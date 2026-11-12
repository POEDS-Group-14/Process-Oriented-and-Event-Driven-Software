"""Tests for process 1. Run from service/:  uv run python -m unittest -v"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from unittest import mock

from rejsegaranti import activities as A
from rejsegaranti import dcr, rules, simulate, workers
from rejsegaranti.bridge import Bridge, make_server
from rejsegaranti.cep import CepSink
from rejsegaranti.stations import distance_m, offset, station_point
from rejsegaranti.store import Store
from rejsegaranti.xes import write_xes

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TO_BE = os.path.join(ROOT, "models", "rejsegaranti-to-be.bpmn")
AS_IS = os.path.join(ROOT, "models", "archive", "rejsegaranti-as-is.bpmn")
RULES = os.path.join(ROOT, "models", "rejsegaranti-rules.dcr.xml")
NS = {"bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL"}
CAM = "{http://camunda.org/schema/1.0/bpmn}"

DELAY = {"delayId": "D1", "line": "L", "stops": ["Lyngby", "Gammelmosevej", "Buddinge"],
         "plannedDeparture": "2026-11-04T07:00:00+01:00", "delayMinutes": 40.0,
         "detectedAt": "2026-11-04T07:30:00+01:00"}


def claim_near(start="Lyngby", end="Buddinge", start_m=100, end_m=100, trip="2026-11-04T07:20:00+01:00", **receipt):
    s = offset(*station_point("L", start), start_m, 0)
    e = offset(*station_point("L", end), 0, end_m)
    r = {"receiptId": "R1", "provider": "Dantaxi", "cvr": "12345678", "transportType": "taxi",
         "startAddress": "a", "endAddress": "b", "distanceKm": 3.2, "minutes": 9,
         "time": trip, "amountDkk": 180.0}
    r.update(receipt)
    return {"claimId": "C1", "tripStart": trip, "startLat": s[0], "startLon": s[1],
            "endLat": e[0], "endLon": e[1], "receipt": r}


class Rules(unittest.TestCase):
    def test_threshold(self):
        self.assertTrue(rules.qualifies_as_delay(DELAY))
        self.assertFalse(rules.qualifies_as_delay({**DELAY, "delayMinutes": 29}))
        self.assertFalse(rules.qualifies_as_delay({**DELAY, "line": "A"}))

    def test_payout_cap(self):
        self.assertEqual(rules.payout(180), 180)
        self.assertEqual(rules.payout(450), 300)
        # an int would be sent to Camunda as Long instead of Double
        self.assertIsInstance(rules.payout(450), float)

    def test_notice_example_from_issue_6(self):
        # work planned Wednesday 05:00; notice seen Monday -> excluded
        delay = {**DELAY, "plannedDeparture": "2026-11-04T06:00:00+01:00"}
        early = {"noticeId": "N1", "lines": ["L"], "stops": [], "effectiveFrom": "2026-11-04T05:00:00+01:00",
                 "effectiveTo": "2026-11-04T12:00:00+01:00", "firstSeen": "2026-11-02T10:00:00+01:00"}
        self.assertEqual(rules.notice_excludes(delay, [early])["noticeId"], "N1")
        # first seen Wednesday 04:30 -> late notice, guarantee applies
        late = {**early, "firstSeen": "2026-11-04T04:30:00+01:00"}
        self.assertIsNone(rules.notice_excludes(delay, [late]))
        # seen Tuesday 23:59 counts as the day before
        self.assertIsNotNone(rules.notice_excludes(delay, [{**early, "firstSeen": "2026-11-03T23:59:00+01:00"}]))
        # other stops -> does not apply
        self.assertIsNone(rules.notice_excludes(delay, [{**early, "stops": ["Ishøj"]}]))

    def test_force_majeure(self):
        w = {"validFrom": "2026-11-04T05:00:00+01:00", "validTo": "2026-11-04T09:00:00+01:00"}
        self.assertIsNotNone(rules.force_majeure(DELAY, [{**w, "source": "DMI", "level": 2}]))
        self.assertIsNone(rules.force_majeure(DELAY, [{**w, "source": "DMI", "level": 1}]))
        # an official warning below the bar wins over OWM
        self.assertIsNone(rules.force_majeure(DELAY, [{**w, "source": "DMI", "level": 1},
                                                      {**w, "source": "OWM", "metrics": {"wind_speed": 30}}]))
        self.assertIsNotNone(rules.force_majeure(DELAY, [{**w, "source": "OWM", "metrics": {"rain_1h": 31}}]))
        self.assertIsNone(rules.force_majeure(DELAY, [{**w, "source": "OWM", "metrics": {"rain_1h": 10}}]))
        self.assertEqual(rules.force_majeure({**DELAY, "cause": "fire"}, []), "cause:fire")
        later = {"validFrom": "2026-11-04T10:00:00+01:00", "validTo": "2026-11-04T12:00:00+01:00"}
        self.assertIsNone(rules.force_majeure(DELAY, [{**later, "source": "DMI", "level": 3}]))

    def test_distance(self):
        self.assertIsNone(rules.check_distance(claim_near(), DELAY))
        self.assertIn("starts", rules.check_distance(claim_near(start_m=400), DELAY))
        self.assertIn("ends", rules.check_distance(claim_near(end_m=400), DELAY))
        # start must be near an *affected* station
        self.assertIsNotNone(rules.check_distance(claim_near(start="Ishøj"), DELAY))

    def test_haversine(self):
        a, b = station_point("L", "Lyngby"), offset(*station_point("L", "Lyngby"), 250, 0)
        self.assertAlmostEqual(distance_m(*a, *b), 250, delta=2)

    def test_receipt(self):
        r = claim_near()["receipt"]
        self.assertIsNone(rules.check_receipt(r))
        self.assertIn("CVR", rules.check_receipt({**r, "cvr": "123"}))
        self.assertIn("missing", rules.check_receipt({**r, "minutes": None}))
        self.assertIn("not covered", rules.check_receipt({**r, "transportType": "private_car"}))
        self.assertIn("already", rules.check_receipt(r, already_paid=["R1"]))

    def test_match(self):
        self.assertEqual(rules.match_delay(claim_near(), [DELAY])["delayId"], "D1")
        self.assertIsNone(rules.match_delay(claim_near(trip="2026-11-04T12:00:00+01:00"), [DELAY]))
        self.assertIsNone(rules.match_delay(claim_near(start="Ishøj"), [DELAY]))


class Workers(unittest.TestCase):
    def setUp(self):
        self.store, self.cep = Store(), CepSink(url="")
        self.store.put("delays", "D1", DELAY)

    def run_claim(self, c):
        self.store.put("claims", c["claimId"], c)
        v = {"claimId": c["claimId"]}
        v.update(workers.match_delay(v, self.store, self.cep))
        return v

    def test_claim_waits_for_assessment(self):
        with self.assertRaises(workers.NotReady):
            self.run_claim(claim_near())

    def test_happy_path_and_duplicate(self):
        v = {"delayId": "D1"}
        for fn in (workers.check_notice, workers.check_force_majeure, workers.report_assessment):
            v.update(fn(v, self.store, self.cep))
        self.assertTrue(v["guaranteeApplies"])
        c = claim_near(amountDkk=420.0)
        v = self.run_claim(c)
        for fn in (workers.check_receipt, workers.check_distance, workers.calc_payout, workers.pay_out,
                   workers.report_outcome):
            v.update(fn(v, self.store, self.cep))
        self.assertEqual(v["payoutDkk"], 300)
        self.assertEqual(self.store.get("outcomes", "C1")["outcome"], "paid")
        # same receipt on a second claim is rejected
        c2 = {**c, "claimId": "C2"}
        v2 = self.run_claim(c2)
        v2.update(workers.check_receipt(v2, self.store, self.cep))
        self.assertFalse(v2["receiptValid"])
        with self.assertRaises(RuntimeError):
            workers.pay_out(v2, self.store, self.cep)


class Bpmn(unittest.TestCase):
    def test_names_topics_messages(self):
        root = ET.parse(TO_BE).getroot()
        names = {e.get("name") for e in root.iter() if e.get("name")}
        for k, n in vars(A).items():
            if k.isupper():
                self.assertIn(n, names)
        topics = {e.get(CAM + "topic") for e in root.iter() if e.get(CAM + "topic")}
        self.assertEqual(topics, set(workers.HANDLERS))
        msgs = {m.get("name") for m in root.findall("bpmn:message", NS)}
        self.assertEqual(msgs, {"DelaySituationDetected", "ClaimSubmitted"})
        for p in root.findall("bpmn:process", NS):
            self.assertEqual(p.get("isExecutable"), "true")
            self.assertTrue(p.get(CAM + "historyTimeToLive"))

    def test_author_named(self):
        for f in (TO_BE, AS_IS):
            with open(f, encoding="utf-8") as fh:
                self.assertIn("Liam Palmqvist", fh.read())

    def test_offline_traces_follow_the_model(self):
        """Every directly-follows pair in the offline log must be possible in
        the TO-BE claim process (through gateways, allowing the parallel pair)."""
        root = ET.parse(TO_BE).getroot()
        proc = [p for p in root.findall("bpmn:process", NS) if p.get("id") == "rejsegaranti-claim"][0]
        name, kind, succ = {}, {}, {}
        for e in proc:
            tag = e.tag.split("}")[1]
            if tag == "sequenceFlow":
                succ.setdefault(e.get("sourceRef"), []).append(e.get("targetRef"))
            elif e.get("id"):
                name[e.get("id")], kind[e.get("id")] = e.get("name"), tag

        def next_acts(nid, seen=None):
            out = set()
            for t in succ.get(nid, []):
                if kind[t].endswith("Gateway"):
                    out |= next_acts(t)
                else:
                    out.add(name[t])
            return out

        allowed = set()
        for nid in name:
            if not kind[nid].endswith("Gateway"):
                for b in next_acts(nid):
                    allowed.add((name[nid], b))
        allowed |= {(A.CHECK_DISTANCE, A.CHECK_RECEIPT), (A.CHECK_RECEIPT, A.CHECK_DISTANCE),
                    (A.DELAY_VERIFIED, A.CHECK_DISTANCE)}
        allowed |= {(A.CHECK_RECEIPT, x) for x in (A.RECEIPT_REJECTED, A.RECEIPT_VERIFIED)}
        _, traces, _ = simulate.run_offline(simulate.generate(seed=3, n_delays=30), seed=3)
        for t in traces:
            seq = [e["name"] for e in t["events"]]
            for a, b in zip(seq, seq[1:]):
                self.assertIn((a, b), allowed, f"{t['id']}: {a} -> {b}")


class Feed(unittest.TestCase):
    def test_batches_have_fresh_ids_and_past_delays(self):
        sent = []

        class Stop(Exception):
            pass

        with mock.patch.object(simulate, "run_live", lambda base, sc: sent.append(sc)), \
                mock.patch.object(simulate.time, "sleep", side_effect=[None, Stop()]), \
                mock.patch.object(simulate, "datetime", wraps=simulate.datetime) as dt:
            dt.now.side_effect = [simulate.datetime(2026, 11, 7, 12, 0, tzinfo=simulate.CPH),
                                  simulate.datetime(2026, 11, 7, 13, 0, tzinfo=simulate.CPH)]
            with self.assertRaises(Stop):
                simulate.feed("http://bridge", batch=3, interval=3600, seed=1)
        self.assertEqual(len(sent), 2)
        ids = [[c["claimId"] for c in sc["claims"]] + [c["receipt"]["receiptId"] for c in sc["claims"]]
               + [d["delayId"] for d in sc["delays"]] for sc in sent]
        self.assertFalse(set(ids[0]) & set(ids[1]))
        for d in sent[1]["delays"]:
            self.assertLess(simulate.parse_ts(d["detectedAt"]),
                            simulate.datetime(2026, 11, 7, 13, 0, tzinfo=simulate.CPH))


class RealDelays(unittest.TestCase):
    def test_claims_for_a_delay_are_always_the_same(self):
        d = {**DELAY, "delayId": "L|2026-11-04:J1", "stops": ["Lyngby", "Gammelmosevej", "Not a station"]}
        a, b = simulate.claims_for(d), simulate.claims_for(d)
        self.assertTrue(a)
        self.assertEqual(a, b)
        self.assertNotIn("Not a station", json.dumps(a))
        self.assertEqual(simulate.claims_for({**d, "stops": ["Not a station"]}), [])

    def test_copenhagen_stations(self):
        from rejsegaranti import stations
        network = stations.load("*")
        self.assertIn("150S", network)
        self.assertIn("5C", network)
        self.assertIn("A", network)  # S-tog
        points = stations.load("150S")["150S"]
        campus = [p for p in points if p[0] == "Nørre Campus"]
        self.assertEqual(len(campus), 2)  # one stop point per side of the road
        self.assertIn("Rævehøjvej, DTU", {p[0] for p in points})
        with mock.patch.object(stations, "STATIONS", network):
            # the distance to a station is to its closer stop point
            self.assertLess(stations.station_distance_m(campus[1][1], campus[1][2], "Nørre Campus", "150S"), 1)
            # station names are per line: 150S has no Hillerød St.
            self.assertEqual(stations.station_distance_m(55.926923, 12.310928, "Hillerød St.", "150S"), float("inf"))
        with self.assertRaises(ValueError):
            stations.load("150S,NOPE")

    def test_claim_is_measured_against_the_delayed_line(self):
        from rejsegaranti import stations
        network = stations.load("150S,5C")
        base = claim_near()
        with mock.patch.object(stations, "STATIONS", network), mock.patch.object(stations, "LINES", set(network)), \
                mock.patch.object(rules, "LINES", set(network)):
            names = list(dict.fromkeys(p[0] for p in network["5C"]))
            d = {**DELAY, "line": "5C", "stops": names[3:6]}
            s, e = stations.station_point("5C", names[4]), stations.station_point("5C", names[8])
            c = {**base, "startLat": s[0], "startLon": s[1], "endLat": e[0], "endLon": e[1]}
            self.assertEqual(rules.match_delay(c, [d, {**d, "delayId": "D2", "line": "150S"}])["delayId"], "D1")
            self.assertIsNone(rules.check_distance(c, d))
            self.assertIsNotNone(rules.check_distance(c, {**d, "line": "150S"}))


class Dcr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.xml = os.path.join(cls.tmp, "rules.xml")
        dcr.to_xml(cls.xml)
        cls.g = dcr.Graph.from_xml(cls.xml)

    def test_committed_xml_is_current(self):
        with open(RULES, encoding="utf-8") as a, open(self.xml, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read(), "run: uv run rejsegaranti build-dcr")

    def test_clean_log_conforms(self):
        _, traces, _ = simulate.run_offline(simulate.generate(seed=5), seed=5)
        r = dcr.check_log(self.g, traces)
        self.assertEqual(r["violating"], 0, r["details"])

    def test_violations_found(self):
        paid = [A.CLAIM_SUBMITTED, A.MATCH_DELAY, A.DELAY_VERIFIED, A.CHECK_RECEIPT, A.CHECK_DISTANCE,
                A.RECEIPT_VERIFIED, A.TRIP_WITHIN, A.CALC_PAYOUT, A.PAY_OUT, A.REPORT_OUTCOME, A.CLAIM_CLOSED]
        self.assertEqual(self.g.replay(paid), [])
        # R1: payout without a verified receipt
        no_receipt = [x for x in paid if x != A.RECEIPT_VERIFIED]
        self.assertTrue(self.g.replay(no_receipt))
        # R2: payout after a notice by the day before
        notice = [A.CLAIM_SUBMITTED, A.MATCH_DELAY, A.NOTICE_BY_DAY_BEFORE] + paid[2:]
        self.assertTrue(any("excluded" in p for p in self.g.replay(notice)))
        # R6: two payouts
        twice = paid[:9] + [A.PAY_OUT] + paid[9:]
        self.assertTrue(any("excluded" in p for p in self.g.replay(twice)))
        # R7: no outcome reported
        self.assertTrue(any("pending" in p for p in self.g.replay(paid[:9])))
        # R8: eligible but rejected
        rejected = paid[:7] + [A.REJECT, A.REPORT_OUTCOME, A.CLAIM_CLOSED]
        self.assertTrue(any("excluded" in p for p in self.g.replay(rejected)))

    def test_log_roles_match_the_graph(self):
        # processdoctor replays a log event only on a graph event with the same role
        root = ET.parse(self.xml).getroot()
        label = {m.get("eventId"): m.get("labelId") for m in root.iter("labelMapping")}
        graph_role = {label[e.get("id")]: e.findtext("custom/roles/role")
                      for e in root.find("specification/resources/events")}
        _, traces, _ = simulate.run_offline(simulate.generate(seed=5), seed=5)
        path = os.path.join(self.tmp, "claims.xes")
        write_xes(path, traces, "test", roles=dcr.ROLES)
        events = list(ET.parse(path).getroot().iter("{http://www.xes-standard.org/}event"))
        self.assertTrue(events)
        for ev in events:
            a = {x.get("key"): x.get("value") for x in ev}
            self.assertEqual(a.get("role"), graph_role[a["concept:name"]], a["concept:name"])

    def test_noise_is_detected(self):
        sc = simulate.generate(seed=9)
        _, traces, _ = simulate.run_offline(sc, seed=9, noise=0.2)
        injected = {t["id"] for t in traces if t["attrs"].get("injectedDeviation")}
        r = dcr.check_log(self.g, traces)
        self.assertTrue(injected)
        self.assertEqual(set(r["details"]), injected)


class FakeCamunda:
    def __init__(self):
        self.calls = []

    def correlate(self, message, business_key=None, variables=None, correlation_keys=None):
        self.calls.append((message, business_key, variables))


class BridgeHttp(unittest.TestCase):
    def setUp(self):
        self.cam = FakeCamunda()
        self.store = Store()
        self.srv = make_server(Bridge(self.store, self.cam), host="127.0.0.1", port=0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, path, body):
        return simulate.post(self.base, path, body)

    def test_flow(self):
        self.assertTrue(self.post("/cep/delay", DELAY)["started"])
        self.assertFalse(self.post("/cep/delay", DELAY)["started"])  # re-emitted, one case per delay
        self.assertIn("ignored", self.post("/cep/delay", {**DELAY, "delayId": "D2", "delayMinutes": 10}))
        # Siddhi-style {"event": {...}} wrapper
        self.assertEqual(self.post("/cep/notice", {"event": {"noticeId": "N1", "effectiveFrom": "2026-11-04T05:00:00",
                                                            "effectiveTo": "2026-11-04T09:00:00",
                                                            "firstSeen": "2026-11-01T09:00:00"}})["noticeId"], "N1")
        bad = claim_near()
        del bad["receipt"]["cvr"]
        r = self.post("/claims", bad)
        self.assertEqual(r["status"], 422)
        self.assertIn("receipt.cvr", r["missing"])
        self.assertEqual(self.post("/claims", claim_near())["claimId"], "C1")
        self.assertEqual([c[0] for c in self.cam.calls], ["DelaySituationDetected", "ClaimSubmitted"])
        with urllib.request.urlopen(self.base + "/state") as resp:
            self.assertEqual(len(json.load(resp)["claims"]), 1)

    def test_results_page(self):
        self.post("/claims", {**claim_near(), "passenger": "P0042"})
        self.store.put("outcomes", "C1", {"claimId": "C1", "outcome": "rejected", "payoutDkk": 0.0,
                                          "reason": "<b>no delay</b>"})
        with urllib.request.urlopen(self.base + "/results/?x=1") as resp:
            self.assertIn("text/html", resp.headers["Content-Type"])
            page = resp.read().decode()
        self.assertIn("simulator", page)  # every banner says the claims are simulated
        self.assertIn("&lt;b&gt;no delay&lt;/b&gt;", page)  # escaped
        self.assertNotIn("P0042", page)  # no passenger IDs on the public page
        self.assertIn('href="claims.csv"', page)
        for path in ("/results/claims.csv", "/claims.csv"):
            with urllib.request.urlopen(self.base + path) as resp:
                self.assertIn("attachment", resp.headers["Content-Disposition"])
                body = resp.read().decode()
            self.assertTrue(body.startswith("claimId,delayId,line,"))
            self.assertIn("C1", body)
            self.assertNotIn("P0042", body)  # nor in the downloads
        with urllib.request.urlopen(self.base + "/results/per-line.csv") as resp:
            self.assertTrue(resp.read().decode().startswith("line,journeys,cases"))
        with mock.patch("rejsegaranti.export.traces_from_camunda", return_value=[
                {"id": "C1", "attrs": {}, "events": [{"name": "Claim received", "time": "2026-11-04T07:30:00+01:00"}]}]):
            with urllib.request.urlopen(self.base + "/results/claims.xes") as resp:
                self.assertIn(b"Claim received", resp.read())
        for bad in ("/results/registry.json", "/results/../claims.csv", "/state.csv"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.base + bad)
            self.assertEqual(e.exception.code, 404)

    def test_live_run_waits_before_reusing_a_receipt(self):
        refused = {**claim_near(receiptId="R9"), "claimId": "C2"}
        del refused["receipt"]["cvr"]
        claims = [claim_near(), refused, {**claim_near(receiptId="R9"), "claimId": "C3"},
                  {**claim_near(), "claimId": "C4"}]
        waited = []
        with mock.patch.object(simulate, "wait_for_outcome", lambda base, cid: waited.append(cid)):
            simulate.run_live(self.base, {"notices": [], "weather": [], "delays": [], "claims": claims}, pause=0)
        # C3 reuses a receipt whose only earlier claim was refused at intake; C4 reuses C1's
        self.assertEqual(waited, ["C1"])


if __name__ == "__main__":
    unittest.main()


class WorkerLoop(unittest.TestCase):
    def test_complete_retry_and_incident(self):
        store, stop = Store(), threading.Event()
        store.put("delays", "D1", DELAY)
        store.put("claims", "C1", claim_near())

        class Cam:
            base = "fake"
            calls = []
            batches = [[
                {"id": "t1", "topicName": "rg-check-notice", "variables": {"delayId": {"value": "D1"}}},
                {"id": "t2", "topicName": "rg-match-delay", "variables": {"claimId": {"value": "C1"}}, "retries": None},
                {"id": "t3", "topicName": "rg-check-receipt", "variables": {"claimId": {"value": "nope"}}},
            ]]

            def fetch_and_lock(self, worker_id, topics):
                if not self.batches:
                    stop.set()
                    return []
                return self.batches.pop()

            def complete(self, tid, wid, variables):
                self.calls.append(("complete", tid, variables))

            def failure(self, tid, wid, msg, retries, ms):
                self.calls.append(("failure", tid, retries))

        cam = Cam()
        workers.run_workers(cam, store, CepSink(url=""), stop=stop)
        self.assertEqual(cam.calls[0], ("complete", "t1", {"noticeExcluded": False, "noticeId": None}))
        self.assertEqual(cam.calls[1], ("failure", "t2", 4))  # not assessed yet -> retry
        self.assertEqual(cam.calls[2], ("failure", "t3", 0))  # bug/data error -> incident


class PerLine(unittest.TestCase):
    def test_poisson_interval(self):
        from rejsegaranti.perline import poisson_interval
        low, high = poisson_interval(0)
        self.assertEqual(low, 0)
        self.assertAlmostEqual(high, 3.689, delta=0.01)  # exact: 3.689
        low, high = poisson_interval(10)
        self.assertAlmostEqual(low, 4.795, delta=0.02)  # exact: 4.795 - 18.39
        self.assertAlmostEqual(high, 18.39, delta=0.05)

    def test_coverage_spans_the_gaps_between_weather_posts(self):
        from rejsegaranti import perline
        since = perline.parse_ts("2026-11-11T00:00:00+01:00")
        # posts every 20 minutes for 6 hours, each valid for 10, then one more post after a 2-hour outage
        posts = [since + perline.timedelta(minutes=20 * i) for i in range(18)] + [since + perline.timedelta(hours=8)]
        weather = [{"validFrom": t.isoformat(), "validTo": (t + perline.SLOT).isoformat()} for t in posts]
        (share,) = perline.coverage(weather, since).values()
        self.assertAlmostEqual(share * 24, 5 + 50 / 60 + 10 / 60)  # 00:00-05:50 (last post valid 10 min), lone post

    def test_rates_are_per_scheduled_journey_while_recording(self):
        from datetime import date
        from rejsegaranti import perline
        since = perline.parse_ts("2026-11-11T00:00:00+01:00")
        # the recorder was up for the first 12 hours of 11 November only
        weather = [{"validFrom": f"2026-11-11T{h:02d}:{m:02d}:00+01:00",
                    "validTo": f"2026-11-11T{h:02d}:{m + 10:02d}:00+01:00" if m < 50 else
                    f"2026-11-11T{h + 1:02d}:00:00+01:00"} for h in range(12) for m in range(0, 60, 10)]
        journeys = {("5C", date(2026, 11, 11)): 500, ("142", date(2026, 11, 11)): 60,
                    ("5C", date(2026, 11, 12)): 500}
        delay = {**DELAY, "plannedDeparture": "2026-11-11T08:00:00+01:00"}
        state = {"weather": weather,
                 "delays": [{**delay, "delayId": "a", "line": "5C"}, {**delay, "delayId": "b", "line": "142"},
                            {**delay, "delayId": "old", "line": "142", "plannedDeparture": "2026-11-10T08:00:00+01:00"}],
                 "outcomes": [{"claimId": "C1", "delayId": "a", "outcome": "paid", "payoutDkk": 200.0}]}
        with mock.patch.object(perline, "LINES", {"5C", "142"}), mock.patch.object(rules, "LINES", {"5C", "142"}):
            rows = {r["line"]: r for r in perline.per_line(state, journeys, since)}
        self.assertEqual(rows["5C"]["journeys"], 250)  # half of the day observed
        self.assertAlmostEqual(rows["5C"]["casesPer1000"], 4.0)
        self.assertAlmostEqual(rows["142"]["casesPer1000"], 1000 / 30)  # the delay before `since` is not counted
        self.assertEqual((rows["5C"]["claims"], rows["5C"]["paid"], rows["5C"]["payoutDkk"]), (1, 1, 200.0))
        self.assertEqual((rows["total"]["cases"], rows["total"]["journeys"]), (2, 280))


class Mailing(unittest.TestCase):
    def setUp(self):
        from rejsegaranti.mailing import MailingList, Notifier
        self.sent, self.got = [], threading.Event()

        def send(batch):
            self.sent += batch
            self.got.set()
        self.store = Store()
        self.people = MailingList(os.path.join(tempfile.mkdtemp(), "subscribers.json"))
        self.notifier = Notifier(self.people, self.store, env={"SMTP_HOST": "smtp.example", "SMTP_USER": "me@x.dk",
                                                             "PUBLIC_URL": "https://example.dk/"}, send=send)

    def wait(self, n):
        for _ in range(50):
            if len(self.sent) >= n:
                return
            self.got.wait(0.1)
            self.got.clear()
        self.fail(f"{len(self.sent)} of {n} emails sent")

    def test_double_opt_in_and_rate_limits(self):
        from rejsegaranti import mailing
        t = self.people.request("A@Example.com", t=0)
        self.assertTrue(t)
        self.assertEqual(os.stat(self.people.path).st_mode & 0o777, 0o600)
        self.assertIsNone(self.people.request("a@example.com", t=60))  # asked again within the hour
        self.assertEqual(self.people.confirmed(), {})  # not on the list before confirming
        self.assertTrue(self.people.confirm(t))
        self.assertEqual(list(self.people.confirmed()), ["a@example.com"])
        self.assertIsNone(self.people.request("a@example.com", t=7200))  # already confirmed
        self.assertFalse(self.people.confirm("nope"))
        for i in range(mailing.CONFIRMATIONS_PER_HOUR):
            self.people.request(f"p{i}@example.com", t=8000)
        self.assertIsNone(self.people.request("late@example.com", t=8001))  # hourly cap
        self.assertTrue(self.people.unsubscribe(t))
        self.assertEqual(self.people.confirmed(), {})
        self.assertFalse(mailing.valid_email("not an email"))
        self.assertTrue(mailing.valid_email("liam@example.dk"))

    def test_one_email_per_decided_claim_to_each_person(self):
        for e in ("a@example.com", "b@example.com"):
            self.people.confirm(self.people.request(e))
        self.people.request("pending@example.com")
        self.store.put("claims", "C1", claim_near())
        self.store.put("delays", "D1", {**DELAY, "line": "5C"})
        cep = CepSink(url="", listeners=[self.notifier.on_event])
        cep.emit("DelayAssessed", {"delayId": "D1"})  # not a claim: no email
        cep.emit("ClaimPaid", {"claimId": "C1", "delayId": "D1", "outcome": "paid", "payoutDkk": 180.0})
        self.wait(2)
        self.assertEqual(sorted(m["To"] for m in self.sent), ["a@example.com", "b@example.com"])
        m = self.sent[0]
        self.assertEqual(m["Subject"], "Claim C1 paid (line 5C)")
        body = m.get_content()
        self.assertIn("180.00 DKK", body)
        self.assertIn("https://example.dk/unsubscribe?token=", body)
        self.assertIn("https://example.dk/unsubscribe?token=", m["List-Unsubscribe"])

    def test_sign_up_over_http(self):
        srv = make_server(Bridge(self.store, None, self.notifier), host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/results") as r:
                self.assertIn('action="subscribe"', r.read().decode())
            with urllib.request.urlopen(base + "/results/subscribe", data=b"email=me%40example.com") as r:
                self.assertIn("Check your inbox", r.read().decode())
            self.wait(1)
            link = re.search(r"https://example.dk(/confirm\?token=\S+)", self.sent[0].get_content()).group(1)
            token = link.split("=", 1)[1]
            with urllib.request.urlopen(base + "/results" + link) as r:  # a mail scanner opening the link
                self.assertIn('method="post"', r.read().decode())
            self.assertEqual(self.people.confirmed(), {})
            with urllib.request.urlopen(base + "/confirm", data=f"token={token}".encode()) as r:
                self.assertIn("on the list", r.read().decode())
            self.assertEqual(list(self.people.confirmed()), ["me@example.com"])
            # one-click unsubscribe (RFC 8058): POST to the link itself
            with urllib.request.urlopen(base + f"/results/unsubscribe?token={token}",
                                        data=b"List-Unsubscribe=One-Click") as r:
                self.assertIn("Unsubscribed", r.read().decode())
            self.assertEqual(self.people.confirmed(), {})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/results/subscribe", data=b"email=nope")
            self.assertEqual(e.exception.code, 400)
        finally:
            srv.shutdown()
            srv.server_close()
