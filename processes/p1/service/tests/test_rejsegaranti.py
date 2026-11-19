"""Tests for process 1. Run from service/:  uv run python -m unittest -v"""
from __future__ import annotations

import glob
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
from rejsegaranti import intake, rules, simulate, workers
from rejsegaranti.bridge import Bridge, make_server
from rejsegaranti.cep import CepSink
from rejsegaranti.stations import distance_m, offset, station_point
from rejsegaranti.store import Store

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TO_BE = os.path.join(ROOT, "models", "to-be")
EXECUTABLE = ("assess-disruption", "decide-claim", "handle-appeal")
BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
CAM = "{http://camunda.org/schema/1.0/bpmn}"

DELAY = {"delayId": "D1", "line": "L", "stops": ["Lyngby", "Gammelmosevej", "Buddinge"],
         "plannedDeparture": "2026-11-04T07:00:00+01:00", "delayMinutes": 40.0,
         "detectedAt": "2026-11-04T07:30:00+01:00"}


def claim_near(start="Lyngby", end="Buddinge", start_m=100, end_m=100, trip="2026-11-04T07:20:00+01:00", **receipt):
    s = offset(*station_point("L", start), start_m, 0)
    e = offset(*station_point("L", end), 0, end_m)
    r = {"receiptId": "R1", "provider": "Dantaxi", "transportType": "taxi", "time": trip, "amountDkk": 180.0}
    r.update(receipt)
    return {"claimId": "C1", "tripStart": trip, "startLat": s[0], "startLon": s[1],
            "endLat": e[0], "endLon": e[1], "receipt": r}


class Rules(unittest.TestCase):
    def test_only_the_line_counts(self):
        self.assertTrue(rules.qualifies_as_delay(DELAY))
        self.assertTrue(rules.qualifies_as_delay({**DELAY, "delayMinutes": 12}))  # the table decides the minutes
        self.assertFalse(rules.qualifies_as_delay({**DELAY, "line": "A"}))

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

    def test_covering_notice_without_deciding(self):
        delay = {**DELAY, "plannedDeparture": "2026-11-04T06:00:00+01:00"}
        early = {"noticeId": "N1", "lines": ["L"], "stops": [], "effectiveFrom": "2026-11-04T05:00:00+01:00",
                 "effectiveTo": "2026-11-04T12:00:00+01:00", "firstSeen": "2026-11-02T10:00:00+01:00"}
        late = {**early, "noticeId": "N2", "firstSeen": "2026-11-04T04:30:00+01:00"}
        self.assertEqual(rules.covering_notice(delay, [late, early])["noticeId"], "N1")  # announced longest before
        self.assertEqual(rules.hours_announced_before(early), 43.0)
        self.assertEqual(rules.hours_announced_before(late), 0.5)  # too late, but that is the table's call
        self.assertEqual(rules.covering_notice(delay, [late])["noticeId"], "N2")
        self.assertIsNone(rules.covering_notice(delay, [{**early, "lines": ["A"]}]))
        self.assertIsNone(rules.covering_notice({**delay, "plannedDeparture": "2026-11-04T13:00:00+01:00"}, [early]))

    def test_weather_at_the_delay(self):
        w = {"validFrom": "2026-11-04T05:00:00+01:00", "validTo": "2026-11-04T09:00:00+01:00"}
        got = rules.weather_at(DELAY, [{**w, "source": "DMI", "level": 1}, {**w, "source": "DMI", "level": 2},
                                       {**w, "source": "OWM", "metrics": {"wind_speed": 26.1, "rain_1h": 0}}])
        self.assertEqual(got, {"dmiLevel": 2, "windSpeed": 26.1, "windGust": None, "rain1h": 0.0, "snow1h": None})
        later = {"validFrom": "2026-11-04T10:00:00+01:00", "validTo": "2026-11-04T12:00:00+01:00"}
        self.assertEqual(rules.weather_at(DELAY, [{**later, "source": "DMI", "level": 3}]),
                         {"dmiLevel": None, "windSpeed": None, "windGust": None, "rain1h": None, "snow1h": None})

    def test_force_majeure(self):  # the AS-IS page's weather check
        w = {"validFrom": "2026-11-04T05:00:00+01:00", "validTo": "2026-11-04T09:00:00+01:00"}
        self.assertIsNotNone(rules.force_majeure(DELAY, [{**w, "source": "DMI", "level": 2}]))
        self.assertIsNone(rules.force_majeure(DELAY, [{**w, "source": "DMI", "level": 1}]))
        self.assertIsNone(rules.force_majeure(DELAY, [{**w, "source": "DMI", "level": 1},
                                                      {**w, "source": "OWM", "metrics": {"wind_speed": 30}}]))
        self.assertIsNotNone(rules.force_majeure(DELAY, [{**w, "source": "OWM", "metrics": {"rain_1h": 31}}]))
        self.assertEqual(rules.force_majeure({**DELAY, "cause": "fire"}, []), "cause:fire")

    def test_haversine(self):
        a, b = station_point("L", "Lyngby"), offset(*station_point("L", "Lyngby"), 250, 0)
        self.assertAlmostEqual(distance_m(*a, *b), 250, delta=2)

    def test_match(self):
        self.assertEqual(rules.match_delay(claim_near(), [DELAY])["delayId"], "D1")
        self.assertIsNone(rules.match_delay(claim_near(trip="2026-11-04T12:00:00+01:00"), [DELAY]))
        self.assertIsNone(rules.match_delay(claim_near(start="Ishøj"), [DELAY]))


class StartVariables(unittest.TestCase):
    def test_claim(self):
        c = claim_near()
        v = intake.claim_start_variables(c, [], "2026-11-18T23:30:00+01:00")
        self.assertEqual(v, {"claimId": "C1", "receiptAttached": True, "daysToClaim": 14, "receiptUsedBefore": False,
                             "fareDkk": 180.0, "reprocessed": False})
        # Copenhagen calendar days: 00:30 local on the 19th is day 15
        self.assertEqual(intake.claim_start_variables(c, [], "2026-11-18T23:30:00+00:00")["daysToClaim"], 15)
        earlier = {**c, "claimId": "C0"}
        self.assertTrue(intake.claim_start_variables(c, [earlier, c], c["tripStart"])["receiptUsedBefore"])
        self.assertFalse(intake.claim_start_variables(c, [c], c["tripStart"])["receiptUsedBefore"])  # itself
        bare = {k: v for k, v in c.items() if k != "receipt"}
        v = intake.claim_start_variables(bare, [bare, {**bare, "claimId": "C0"}], c["tripStart"], reprocessed=True)
        self.assertEqual((v["receiptAttached"], v["fareDkk"], v["receiptUsedBefore"], v["reprocessed"]),
                         (False, None, False, True))
        self.assertIsInstance(intake.claim_start_variables({**c, "receipt": {"amountDkk": 200}}, [], c["tripStart"])
                              ["fareDkk"], float)  # a Double in Camunda

    def test_delay_and_order(self):
        self.assertEqual(intake.delay_start_variables({**DELAY, "delayMinutes": 40}),
                         {"delayId": "D1", "line": "L", "delayMinutes": 40.0, "reprocessed": False})
        a = {"claimId": "B", "tripStart": "2026-11-04T07:00:00+01:00"}
        b = {"claimId": "A", "tripStart": "2026-11-04T06:30:00Z"}  # later, though the string sorts first
        self.assertEqual(sorted([b, a], key=intake.replay_order), [a, b])


class Workers(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.store, self.cep = Store(), CepSink(url="", listeners=[self.events.append])
        self.store.put("delays", "D1", {**DELAY, "cause": "strike"})

    def assess(self, delay_id="D1", exclusion="none"):
        v = {"delayId": delay_id, "line": "L", "delayMinutes": 40.0, "reprocessed": False}
        v.update(workers.collect_disruption(v, self.store, self.cep))
        v["exclusion"] = exclusion  # what the Decide exclusion table would set
        v.update(workers.publish_assessment(v, self.store, self.cep))
        return v

    def test_collect_disruption(self):
        self.store.put("weather", "W1", {"source": "DMI", "level": 2, "validFrom": "2026-11-04T05:00:00+01:00",
                                         "validTo": "2026-11-04T09:00:00+01:00"})
        self.store.put("notices", "N1", {"noticeId": "N1", "lines": ["L"], "effectiveFrom": "2026-11-04T06:00:00+01:00",
                                         "effectiveTo": "2026-11-04T09:00:00+01:00",
                                         "firstSeen": "2026-11-03T06:00:00+01:00"})
        v = workers.collect_disruption({"delayId": "D1"}, self.store, self.cep)
        self.assertEqual(v, {"service": "local train", "operatorCause": "strike", "dmiLevel": 2, "windSpeed": None,
                             "windGust": None, "rain1h": None, "snow1h": None, "hoursAnnouncedBefore": 24.0})
        self.store.put("delays", "D2", {**DELAY, "delayId": "D2", "line": "5C", "cause": "signal fault"})
        v = workers.collect_disruption({"delayId": "D2"}, self.store, self.cep)
        self.assertEqual((v["service"], v["operatorCause"]), ("bus", "none"))

    def test_assessment_published(self):
        v = self.assess(exclusion="Strike, weather or force majeure")
        self.assertFalse(v["guaranteeApplies"])
        a = self.store.get("assessments", "D1")
        self.assertEqual((a["exclusion"], a["operatorCause"], a["service"]),
                         ("Strike, weather or force majeure", "strike", "local train"))
        self.assertEqual(self.events[-1]["eventType"], "DelayAssessed")
        workers.notify_passengers({"delayId": "D1", "line": "L"}, self.store, self.cep)
        self.assertEqual((self.events[-1]["eventType"], self.events[-1]["line"]), ("PassengersNotified", "L"))

    def test_match_claim(self):
        self.store.put("claims", "C1", claim_near())
        with self.assertRaises(workers.NotReady):
            workers.match_claim({"claimId": "C1"}, self.store, self.cep)
        # an assessment the archived Line L process stored has no exclusion: wait for the TO-BE's own
        self.store.put("assessments", "D1", {"delayId": "D1", "guaranteeApplies": True, "forceMajeure": False})
        with self.assertRaises(workers.NotReady):
            workers.match_claim({"claimId": "C1"}, self.store, self.cep)
        self.assess(exclusion="none")
        v = workers.match_claim({"claimId": "C1"}, self.store, self.cep)
        self.assertEqual(v, {"delayId": "D1", "delayMinutes": 40.0, "exclusion": "none", "incident": "delay",
                             "visibleToDriver": None, "busFull": None, "stopRequested": None,
                             "minutesToNextDeparture": None})
        # no delay at that time: every table input still exists, delayMinutes is null
        self.store.put("claims", "C2", {**claim_near(trip="2026-11-04T15:00:00+01:00"), "claimId": "C2",
                                        "incident": "bus passed stop", "visibleToDriver": True})
        v = workers.match_claim({"claimId": "C2"}, self.store, self.cep)
        self.assertEqual((v["delayId"], v["delayMinutes"], v["exclusion"], v["incident"], v["visibleToDriver"]),
                         (None, None, "none", "bus passed stop", True))
        self.assertIn("busFull", v)

    def test_paid_and_rejected(self):
        self.store.put("claims", "C1", claim_near())
        v = {"claimId": "C1", "claimValid": True, "covered": True, "delayId": "D1", "refundDkk": 180.0,
             "reprocessed": True}
        v.update(workers.pay_out(v, self.store, self.cep))
        self.assertEqual(v["payoutReference"], "PAY-C1")
        workers.send_decision(v, self.store, self.cep)
        self.assertEqual(self.store.get("outcomes", "C1"), {"claimId": "C1", "delayId": "D1", "outcome": "paid",
                                                            "payoutDkk": 180.0, "reason": None, "reprocessed": True})
        self.assertEqual(self.events[-1]["eventType"], "ClaimPaid")
        # the same receipt cannot be paid twice
        self.store.put("claims", "C2", {**claim_near(), "claimId": "C2"})
        with self.assertRaises(RuntimeError):
            workers.pay_out({"claimId": "C2"}, self.store, self.cep)
        for valid, expected in ((True, "Delay 20 min or less"), (False, "No receipt")):
            v = {"claimId": "C3", "claimValid": valid, "claimReason": "No receipt",
                 "guaranteeReason": "Delay 20 min or less" if valid else None}
            v.update(workers.reject(v, self.store, self.cep))
            self.assertEqual(v["rejectionReason"], expected)
        workers.send_decision(v, self.store, self.cep)
        o = self.store.get("outcomes", "C3")
        self.assertEqual((o["outcome"], o["payoutDkk"], o["reason"], o["reprocessed"]),
                         ("rejected", 0.0, "No receipt", False))
        self.assertEqual(self.events[-1]["eventType"], "ClaimRejected")

    def test_appeal(self):
        self.store.put("claims", "C1", claim_near(amountDkk=420.0))
        self.store.put("outcomes", "C1", {"claimId": "C1", "outcome": "rejected", "payoutDkk": 0.0,
                                          "reason": "Planned change"})
        v = {"claimId": "C1", "appealReason": "The notice was wrong"}
        v.update(workers.retrieve_claim(v, self.store, self.cep))
        self.assertEqual(v, {**v, "fareDkk": 420.0, "previousOutcome": "rejected", "previousReason": "Planned change"})
        v.update(upheld=True, reviewNote="ok", refundDkk=300.0)
        v.update(workers.pay_out(v, self.store, self.cep))
        workers.send_appeal_decision(v, self.store, self.cep)
        self.assertEqual(self.store.get("outcomes", "C1")["outcome"], "paid")
        self.assertEqual(self.store.get("outcomes", "C1")["payoutDkk"], 300.0)
        a = self.store.get("appeals", "C1")
        self.assertEqual((a["upheld"], a["refundDkk"], a["status"]), (True, 300.0, "decided"))
        self.assertEqual(self.events[-1]["eventType"], "AppealDecided")
        # dismissed: the outcome stays; no receipt: the fare is 0, so the refund table has a number
        self.store.put("claims", "C2", {k: x for k, x in claim_near().items() if k != "receipt"} | {"claimId": "C2"})
        self.assertEqual(workers.retrieve_claim({"claimId": "C2"}, self.store, self.cep)["fareDkk"], 0.0)
        workers.send_appeal_decision({"claimId": "C2", "upheld": False}, self.store, self.cep)
        self.assertIsNone(self.store.get("outcomes", "C2"))

    def test_every_topic_has_a_handler(self):
        self.assertEqual(set(workers.HANDLERS), set(A.TOPICS.values()))


def _process(path):
    """The first bpmn:process of a file (processdoctor reads only that one)."""
    root = ET.parse(path).getroot()
    return root, next(root.iter(BPMN + "process"))


def _tag(e):
    return e.tag.split("}")[1]


class Models(unittest.TestCase):
    def test_names_and_topics_match_activities(self):
        known = {v for k, v in vars(A).items() if k.isupper() and isinstance(v, str)}
        seen = set()
        for name in EXECUTABLE:
            for path in (os.path.join(TO_BE, name + ".bpmn"), os.path.join(TO_BE, name + ".processdoctor.bpmn")):
                _, proc = _process(path)
                for e in proc:
                    t = _tag(e)
                    if t == "task" or t.endswith("Task") or t.endswith("Event"):
                        self.assertIn(e.get("name"), known, f"{os.path.basename(path)}: {e.get('name')}")
                        seen.add(e.get("name"))
                    if t == "serviceTask":
                        self.assertEqual(e.get(CAM + "topic"), A.TOPICS.get(e.get("name")), e.get("name"))
        root, proc = _process(os.path.join(TO_BE, "assess-disruption.bpmn"))
        self.assertEqual(proc.get("id"), A.ASSESS_KEY)
        names = {v for k, v in vars(A).items() if k.isupper() and isinstance(v, str) and not k.endswith(("KEY", "MESSAGE"))}
        self.assertEqual(names - seen, set())  # every activity name is in a model
        keys, messages = set(), set()
        for name in EXECUTABLE:
            root, proc = _process(os.path.join(TO_BE, name + ".bpmn"))
            keys.add(proc.get("id"))
            messages |= {m.get("name") for m in root.findall(BPMN + "message")}
            self.assertEqual(proc.get("isExecutable"), "true")
            self.assertTrue(proc.get(CAM + "historyTimeToLive"))
        self.assertEqual(keys, {A.ASSESS_KEY, A.CLAIM_KEY, A.APPEAL_KEY})
        self.assertEqual(messages, {A.DELAY_MESSAGE, A.CLAIM_MESSAGE, A.APPEAL_MESSAGE})

    def test_processdoctor_rules(self):
        files = glob.glob(os.path.join(TO_BE, "*.processdoctor.bpmn")) + [os.path.join(TO_BE, "passenger-journey.bpmn")]
        self.assertEqual(len(files), 4)
        for path in files:
            f = os.path.basename(path)
            _, proc = _process(path)
            nodes = {e.get("id"): _tag(e) for e in proc if _tag(e) != "sequenceFlow"}
            inc, out = {}, {}
            for e in proc:
                if _tag(e) == "sequenceFlow":
                    out[e.get("sourceRef")] = out.get(e.get("sourceRef"), 0) + 1
                    inc[e.get("targetRef")] = inc.get(e.get("targetRef"), 0) + 1
            self.assertEqual(sum(t == "startEvent" for t in nodes.values()), 1, f)
            for nid, t in nodes.items():
                if t == "task":
                    self.assertEqual((inc.get(nid, 0), out.get(nid, 0)), (1, 1), f"{f}: {nid}")
                elif t.endswith("Gateway"):
                    split = inc.get(nid, 0) == 1 and out.get(nid, 0) > 1
                    join = inc.get(nid, 0) > 1 and out.get(nid, 0) == 1
                    self.assertTrue(split or join, f"{f}: gateway {nid} is neither a split nor a join")
                elif f.endswith(".processdoctor.bpmn"):
                    self.assertFalse(t.endswith("Task"), f"{f}: typed task {nid} ({t})")

    def test_tables_read_only_variables_the_processes_set(self):
        """Every input of a ToBe_ table is a start variable, a worker output or a table output."""
        dmn = ET.parse(os.path.join(TO_BE, "travel-guarantee-to-be.dmn")).getroot()
        dns = dmn.tag.split("}")[0] + "}"
        inputs = {e.findtext(dns + "text").strip() for e in dmn.iter(dns + "inputExpression")}
        decided = {o.get("name") for o in dmn.iter(dns + "output")}
        s = Store()
        s.put("delays", "D1", DELAY)
        s.put("claims", "C1", claim_near())
        s.put("assessments", "D1", {"delayId": "D1", "exclusion": "none"})
        cep = CepSink(url="")
        have = set(intake.delay_start_variables(DELAY)) | set(workers.collect_disruption({"delayId": "D1"}, s, cep))
        have |= set(intake.claim_start_variables(claim_near(), [], DELAY["detectedAt"]))
        have |= set(workers.match_claim({"claimId": "C1"}, s, cep))
        self.assertTrue(inputs)
        self.assertEqual(inputs - have - decided, set())


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
        ids = [[c["claimId"] for c in sc["claims"]] + [d["delayId"] for d in sc["delays"]] for sc in sent]
        self.assertFalse(set(ids[0]) & set(ids[1]))
        for d in sent[1]["delays"]:
            self.assertLess(simulate.parse_ts(d["detectedAt"]),
                            simulate.datetime(2026, 11, 7, 13, 0, tzinfo=simulate.CPH))

    def test_movia_scenarios(self):
        sc = simulate.generate(seed=4, n_delays=40, start=simulate.datetime(2026, 11, 2, 6, 0, tzinfo=simulate.CPH))
        kinds = {c["scenario"] for c in sc["claims"]}
        self.assertEqual(kinds, {"valid", "no receipt", "fare over 300", "duplicate receipt", "no delay at that time",
                                 "long after the delay"})
        delays = sc["delays"]
        for c in sc["claims"]:
            r = c.get("receipt")
            self.assertEqual(r is None, c["scenario"] == "no receipt")
            if c["scenario"] == "fare over 300":
                self.assertGreater(r["amountDkk"], 300)
            elif c["scenario"] == "valid":
                self.assertLessEqual(r["amountDkk"], 300)
                self.assertIsNotNone(rules.match_delay(c, delays), c["claimId"])
            if c["scenario"] in ("no delay at that time", "long after the delay"):
                self.assertIsNone(rules.match_delay(c, delays), c["claimId"])
            if r:
                self.assertEqual(r["transportType"], "taxi")
        receipts = [c["receipt"]["receiptId"] for c in sc["claims"] if c["scenario"] == "duplicate receipt"]
        firsts = {c["receipt"]["receiptId"] for c in sc["claims"] if c.get("receipt") and c["scenario"] != "duplicate receipt"}
        self.assertTrue(receipts)
        self.assertLessEqual(set(receipts), firsts)


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

    def test_claim_is_matched_on_the_delayed_line(self):
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


class FakeCamunda:
    def __init__(self):
        self.calls = []

    def correlate(self, message, business_key=None, variables=None, correlation_keys=None):
        self.calls.append((message, business_key, variables))

    # the AS-IS tables, enough of them for the results pages: the real ones are checked in Camunda by
    # models/travel-guarantee/check_camunda.py
    def evaluate(self, key, variables):
        self.calls.append((key, None, variables))
        if key == "Decision_ClaimValid":
            if not variables["receiptAttached"]:
                return {"claimValid": False, "claimReason": "No receipt"}
            ok = variables["daysToClaim"] <= 14
            return {"claimValid": ok, "claimReason": "Valid" if ok else "Claim too late"}
        if variables["cause"] != "none":
            return {"covered": False, "reason": "Strike, weather or force majeure"}
        return {"covered": True, "reason": "Covered"}

    def process_instances(self, key):
        return [{"businessKey": "C1", "startTime": "2026-11-20T09:00:00.000+0100"}]


class BridgeHttp(unittest.TestCase):
    def setUp(self):
        from rejsegaranti import asis
        asis._cache.clear()
        self.cam = FakeCamunda()
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "registry.json"))
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
        self.assertTrue(self.post("/cep/delay", {**DELAY, "delayId": "D2", "delayMinutes": 10})["started"])
        self.assertIn("ignored", self.post("/cep/delay", {**DELAY, "delayId": "D3", "line": "A"}))
        # Siddhi-style {"event": {...}} wrapper
        self.assertEqual(self.post("/cep/notice", {"event": {"noticeId": "N1", "effectiveFrom": "2026-11-04T05:00:00",
                                                            "effectiveTo": "2026-11-04T09:00:00",
                                                            "firstSeen": "2026-11-01T09:00:00"}})["noticeId"], "N1")
        bad = claim_near()
        del bad["startLat"]
        r = self.post("/claims", bad)
        self.assertEqual((r["status"], r["missing"]), (422, ["startLat"]))
        self.assertEqual(self.post("/claims", {**claim_near(), "tripStart": "yesterday"})["status"], 422)
        self.assertEqual(self.post("/claims", claim_near())["claimId"], "C1")
        self.assertEqual(self.post("/claims", claim_near())["status"], 409)
        no_receipt = {k: v for k, v in claim_near().items() if k != "receipt"}
        self.assertEqual(self.post("/claims", {**no_receipt, "claimId": "C2"})["claimId"], "C2")  # the table rejects it
        self.assertEqual(self.post("/claims", {**claim_near(), "claimId": "C3"})["claimId"], "C3")
        msgs = [c for c in self.cam.calls if c[0] in (A.DELAY_MESSAGE, A.CLAIM_MESSAGE)]
        self.assertEqual([m[0] for m in msgs], [A.DELAY_MESSAGE] * 2 + [A.CLAIM_MESSAGE] * 3)
        self.assertEqual(msgs[0][2], {"delayId": "D1", "line": "L", "delayMinutes": 40.0, "reprocessed": False})
        c1, c2, c3 = (m[2] for m in msgs[2:])
        self.assertEqual((c1["receiptAttached"], c1["receiptUsedBefore"], c1["daysToClaim"]), (True, False,
                         (intake.local_date(intake.parse_ts(self.store.get("claims", "C1")["receivedAt"]))
                          - intake.local_date(intake.parse_ts(claim_near()["tripStart"]))).days))
        self.assertEqual((c2["receiptAttached"], c2["fareDkk"]), (False, None))
        self.assertTrue(c3["receiptUsedBefore"])  # C1 had the same receipt
        self.assertIn("receivedAt", self.store.get("claims", "C1"))
        with urllib.request.urlopen(self.base + "/state") as resp:
            state = json.load(resp)
        self.assertEqual((len(state["claims"]), state["appeals"]), (3, []))

    def test_appeals(self):
        self.post("/claims", claim_near())
        self.assertEqual(self.post("/appeals", {"claimId": "nope", "reason": "x"})["status"], 404)
        self.assertEqual(self.post("/appeals", {"claimId": "C1"})["status"], 422)
        self.assertTrue(self.post("/appeals", {"claimId": "C1", "reason": "I was there"})["appealed"])
        self.assertEqual(self.post("/appeals", {"claimId": "C1", "reason": "again"})["status"], 409)
        self.assertEqual(self.cam.calls[-1], (A.APPEAL_MESSAGE, "C1", {"claimId": "C1", "appealReason": "I was there"}))
        self.assertEqual(self.store.get("appeals", "C1")["status"], "open")

    def test_replay_endpoints(self):
        self.store.put("delays", "D1", DELAY)
        self.store.put("claims", "C2", {**claim_near(trip="2026-11-04T07:25:00+01:00"), "claimId": "C2"})
        self.store.put("claims", "C1", claim_near())
        self.store.put("assessments", "D1", {"delayId": "D1", "exclusion": "none"})
        self.store.put("outcomes", "C1", {"claimId": "C1", "outcome": "paid"})
        self.store.mark_paid("R1", "C1")
        r = self.post("/replay/reset", {})
        self.assertEqual(r["cleared"], {"assessments": 1, "outcomes": 1, "appeals": 0, "paid_receipts": 1})
        with open(r["backup"], encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["outcomes"]), 1)  # the backup has what was there
        self.assertEqual((self.store.all("outcomes"), self.store.paid_receipts), ([], {}))
        self.assertEqual(len(self.store.all("claims")), 2)  # what came in stays
        self.post("/replay/delay", {"delayId": "D1"})
        self.post("/replay/claim", {"claimId": "C2", "receivedAt": "2026-11-20T09:00:00+01:00"})
        self.post("/replay/claim", {"claimId": "C1"})
        self.assertEqual(self.post("/replay/claim", {"claimId": "nope"})["status"], 404)
        d, c2, c1 = (c[2] for c in self.cam.calls if c[0] in (A.DELAY_MESSAGE, A.CLAIM_MESSAGE))
        self.assertTrue(d["reprocessed"] and c1["reprocessed"] and c2["reprocessed"])
        self.assertEqual((c2["daysToClaim"], c2["receiptUsedBefore"]), (16, True))  # C1's trip was earlier
        self.assertEqual((c1["daysToClaim"], c1["receiptUsedBefore"]), (0, False))  # falls back to the trip start
        with mock.patch("rejsegaranti.bridge.LOOPBACK", ()):
            self.assertEqual(self.post("/replay/reset", {})["status"], 403)

    def test_to_be_page_compares_with_as_is(self):
        self.store.put("delays", "D1", DELAY)
        for cid, amount, trip in (("C1", 180.0, "2026-11-04T07:20:00+01:00"),  # AS-IS: too late (16 days)
                                  ("C2", 120.0, "2026-11-04T07:25:00+01:00")):
            self.store.put("claims", cid, {**claim_near(amountDkk=amount, trip=trip), "claimId": cid, "passenger": "P0042"})
        self.store.put("outcomes", "C1", {"claimId": "C1", "delayId": "D1", "outcome": "paid", "payoutDkk": 180.0,
                                          "reason": None})
        self.store.put("outcomes", "C2", {"claimId": "C2", "delayId": "D1", "outcome": "rejected", "payoutDkk": 0.0,
                                          "reason": "<b>Planned change</b>"})
        with urllib.request.urlopen(self.base + "/results/to-be?x=1") as resp:
            self.assertIn("text/html", resp.headers["Content-Type"])
            page = resp.read().decode()
        self.assertIn(": TO-BE</h1>", page)
        self.assertIn("simulator", page)  # every banner says the claims are simulated
        self.assertIn("The delays are real; every claim is simulated", page)
        self.assertIn("&lt;b&gt;Planned change&lt;/b&gt;", page)  # escaped
        self.assertNotIn('action="subscribe"', page)  # the sign-up form is hidden
        self.assertNotIn("P0042", page)  # no passenger IDs on the public page
        self.assertIn("AS-IS against TO-BE", page)
        # C1: AS-IS rejected (too late), TO-BE paid; C2: AS-IS paid, TO-BE rejected
        self.assertRegex(page, r"AS-IS paid</th><td class=num>0</td><td class=num>1</td>")
        self.assertRegex(page, r"AS-IS rejected</th><td class=num>1</td><td class=num>0</td>")
        self.assertRegex(page, r"C1</td>.*?<td class=rejected>rejected</td>.*?Claim too late.*?<td class=paid>paid</td>")
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
                {"id": "C1", "attrs": {}, "events": [{"name": A.CLAIM_SUBMITTED, "time": "2026-11-04T07:30:00+01:00"}]}]):
            with urllib.request.urlopen(self.base + "/results/claims.xes") as resp:
                self.assertIn(b"Claim submitted", resp.read())
        for bad in ("/results/registry.json", "/results/../claims.csv", "/state.csv"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.base + bad)
            self.assertEqual(e.exception.code, 404)

    def test_as_is_page_is_the_main_page(self):
        self.store.put("delays", "D1", DELAY)
        for cid, amount, trip in (("C1", 450.0, "2026-11-04T07:20:00+01:00"),
                                  ("C2", 120.0, "2026-11-04T12:25:00+01:00"),
                                  ("C3", 200.0, "2026-11-04T07:25:00+01:00")):
            self.store.put("claims", cid, {**claim_near(amountDkk=amount, trip=trip), "claimId": cid})
        # what the TO-BE decided does not change the AS-IS judgement
        self.store.put("outcomes", "C3", {"claimId": "C3", "delayId": None, "outcome": "rejected"})
        self.store.put("assessments", "D1", {"delayId": "D1", "exclusion": "Planned change"})
        for path in ("/", "/results", "/results/"):
            with urllib.request.urlopen(self.base + path) as resp:
                page = resp.read().decode()
            self.assertIn(": AS-IS</h1>", page)
            self.assertIn('href="to-be"', page)
        # C1: received 16 days after the trip (FakeCamunda.process_instances) -> too late
        self.assertIn("Claim too late", page)
        # C2: no delay at that time; C3: covered, refund = fare under the cap
        self.assertIn("No delay over 20 min found", page)
        self.assertRegex(page, r"C3</td>.*?<td class=paid>paid</td><td class=num>200.00</td>")
        decided = [c for c in self.cam.calls if c[0] == "Decision_TravelGuarantee"]
        self.assertEqual(decided[0][2]["incident"], "delay over 20 min")
        self.assertEqual(decided[0][2]["service"], "local train")  # line L: letters only
        self.assertEqual(decided[0][2]["cause"], "none")
        with urllib.request.urlopen(self.base + "/to-be") as resp:
            self.assertIn(": TO-BE</h1>", resp.read().decode())

    def test_as_is_refund_capped_and_weather(self):
        from rejsegaranti import asis
        self.store.put("delays", "D1", DELAY)
        self.store.put("claims", "C9", {**claim_near(amountDkk=450.0), "claimId": "C9"})
        row = asis.judge(self.store, self.cam)[0]
        self.assertEqual((row["outcome"], row["refundDkk"], row["delayId"]), ("paid", 300.0, "D1"))
        self.store.put("weather", "W1", {"source": "DMI", "level": 2, "validFrom": "2026-11-04T05:00:00+01:00",
                                         "validTo": "2026-11-04T09:00:00+01:00"})
        self.assertEqual(asis.judge(self.store, self.cam)[0]["outcome"], "rejected")


class WorkerLoop(unittest.TestCase):
    def test_complete_retry_and_incident(self):
        store, stop = Store(), threading.Event()
        store.put("delays", "D1", DELAY)
        store.put("claims", "C1", claim_near())

        class Cam:
            base = "fake"
            calls = []
            batches = [[
                {"id": "t1", "topicName": "tb-collect-disruption", "variables": {"delayId": {"value": "D1"}}},
                {"id": "t2", "topicName": "tb-match-claim", "variables": {"claimId": {"value": "C1"}}, "retries": None},
                {"id": "t3", "topicName": "tb-retrieve-claim", "variables": {}},
                {"id": "t4", "topicName": "tb-pay-out", "variables": {"claimId": {"value": "C1"}}},
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
                if tid == "t3":
                    raise OSError("instance deleted")  # the loop must survive this

        cam = Cam()
        workers.run_workers(cam, store, CepSink(url=""), stop=stop)
        self.assertEqual(cam.calls[0][:2], ("complete", "t1"))
        self.assertEqual(cam.calls[0][2]["service"], "local train")
        self.assertEqual(cam.calls[1], ("failure", "t2", 4))  # not assessed yet -> retry
        self.assertEqual(cam.calls[2], ("failure", "t3", 0))  # bug/data error -> incident
        self.assertEqual(cam.calls[3], ("complete", "t4", {"payoutReference": "PAY-C1"}))


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

    def test_no_email_for_replayed_claims(self):
        self.people.confirm(self.people.request("a@example.com"))
        self.store.put("claims", "C1", claim_near())
        cep = CepSink(url="", listeners=[self.notifier.on_event])
        cep.emit("ClaimRejected", {"claimId": "C1", "outcome": "rejected", "reason": "No receipt", "reprocessed": True})
        cep.emit("ClaimRejected", {"claimId": "C1", "outcome": "rejected", "reason": "No receipt", "reprocessed": False})
        self.wait(1)
        self.got.wait(0.3)
        self.assertEqual(len(self.sent), 1)

    def test_sign_up_over_http(self):
        srv = make_server(Bridge(self.store, None, self.notifier), host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/results/to-be") as r:
                self.assertNotIn('action="subscribe"', r.read().decode())  # form hidden, the list still works
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


if __name__ == "__main__":
    unittest.main()
