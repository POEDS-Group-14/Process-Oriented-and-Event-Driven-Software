"""Simulated input for process 1 (issue #9) and an offline dry run (issue #11).

Claims are always simulated (there is no public claims data). Delays,
weather and notices are simulated too until real data is available; pass
--delays <csv> to use recorded Line L delays instead (format in
data/delays-format.csv). The CSV is what the Rejseplanen recorder (#29/#35)
or the CEP should produce once API access is granted.

Two modes:
  --post URL      send everything to the running bridge (and so to Camunda)
  --offline FILE  run the same rule handlers without Camunda and write an XES
                  log with the same activity names as the BPMN model; use it
                  to test the DCR rules before the engine is up
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import time
import urllib.request
from datetime import datetime, timedelta
from typing import List, Optional

from . import activities as A
from . import rules, workers
from .cep import CepSink
from .stations import LINES, STATIONS, offset, station_point
from .store import Store
from .timeutil import CPH, iso, parse_ts

PROVIDERS = [
    ("Dantaxi 4x48", "taxi", "10153678"),
    ("Taxa 4x35", "taxi", "57188416"),
    ("Donkey Republic", "bike_share", "37315524"),
    ("Voi Technology", "scooter_share", "39543036"),
    ("GreenMobility", "car_share", "37008958"),
]
# NOTE: provider names are only labels for simulated receipts; the CVR
# numbers are random 8-digit strings, not the companies' real numbers.


def _line(rng: random.Random) -> str:
    # no draw for a single line, so Line L scenarios stay the same for a given seed
    return sorted(LINES)[rng.randrange(len(LINES))] if len(LINES) > 1 else next(iter(LINES))


def _segment(rng: random.Random, line: str) -> List[str]:
    points = STATIONS[line]
    i = rng.randrange(0, len(points) - 3)
    j = min(len(points), i + rng.randint(3, 8))
    return list(dict.fromkeys(s[0] for s in points[i:j]))  # two sides of the road are one station


def generate(seed: int = 14, n_delays: int = 40, start: Optional[datetime] = None,
             delays: Optional[List[dict]] = None, id_prefix: str = "") -> dict:
    rng = random.Random(seed)
    start = start or datetime(2026, 11, 2, 6, 0, tzinfo=CPH)
    out = {"delays": [], "weather": [], "notices": [], "claims": []}

    if delays is None:
        delays = []
        for k in range(n_delays):
            t = start + timedelta(days=rng.randint(0, 27), minutes=rng.randint(0, 17 * 60))
            minutes = rng.choice([12, 20, 31, 35, 40, 45, 55, 70, 95]) if rng.random() < 0.2 else rng.randint(30, 90)
            line = _line(rng)
            delays.append({
                "delayId": f"D{k + 1:03d}", "line": line, "stops": _segment(rng, line),
                "plannedDeparture": iso(t), "delayMinutes": float(minutes),
                "detectedAt": iso(t + timedelta(minutes=min(minutes, 30))),
                "cause": rng.choice([None] * 12 + ["fire", "person_collision"]),
            })
    out["delays"] = delays

    for d in delays:
        t = parse_ts(d["plannedDeparture"])
        r = rng.random()
        if r < 0.08:  # notice published two days before -> excluded
            out["notices"].append({"noticeId": f"N-{d['delayId']}", "lines": [d["line"]], "stops": d["stops"],
                                   "effectiveFrom": iso(t - timedelta(hours=1)),
                                   "effectiveTo": iso(t + timedelta(hours=6)),
                                   "firstSeen": iso(t - timedelta(days=2)), "simulated": True})
        elif r < 0.14:  # late notice, same morning -> guarantee applies
            out["notices"].append({"noticeId": f"N-{d['delayId']}", "lines": [d["line"]], "stops": d["stops"],
                                   "effectiveFrom": iso(t - timedelta(minutes=30)),
                                   "effectiveTo": iso(t + timedelta(hours=3)),
                                   "firstSeen": iso(t - timedelta(minutes=60)), "simulated": True})
        w = rng.random()
        if w < 0.06:
            out["weather"].append({"situationId": f"W-{d['delayId']}", "source": "DMI", "level": 2,
                                   "type": "storm", "area": "Hovedstaden",
                                   "validFrom": iso(t - timedelta(hours=3)), "validTo": iso(t + timedelta(hours=6)),
                                   "simulated": True})
        elif w < 0.10:
            out["weather"].append({"situationId": f"W-{d['delayId']}", "source": "OWM",
                                   "metrics": {"wind_speed": 26.1, "wind_gust": 34.0, "rain_1h": 2.0},
                                   "validFrom": iso(t - timedelta(minutes=30)), "validTo": iso(t + timedelta(hours=1)),
                                   "simulated": True})
        elif w < 0.14:  # below the bar: DMI level 1 only
            out["weather"].append({"situationId": f"W-{d['delayId']}", "source": "DMI", "level": 1,
                                   "type": "heavy rain", "area": "Hovedstaden",
                                   "validFrom": iso(t - timedelta(hours=2)), "validTo": iso(t + timedelta(hours=2)),
                                   "simulated": True})

    n = 0
    for d in delays:
        t = parse_ts(d["plannedDeparture"])
        for _ in range(rng.randint(1, 4)):
            n += 1
            out["claims"].append(_claim(rng, n, d, t, id_prefix))
    # duplicates: the same receipt claimed twice
    for c in rng.sample(out["claims"], k=max(1, len(out["claims"]) // 25)):
        n += 1
        dup = json.loads(json.dumps(c))
        dup["claimId"] = f"{id_prefix}C{n:04d}"
        dup["scenario"] = "duplicate receipt"
        out["claims"].append(dup)
    # claims that match no delay (wrong time)
    for _ in range(max(1, len(delays) // 10)):
        n += 1
        d = rng.choice(delays)
        c = _claim(rng, n, d, parse_ts(d["plannedDeparture"]) + timedelta(hours=8), id_prefix)
        c["scenario"] = "no delay at that time"
        out["claims"].append(c)
    return out


def _claim(rng: random.Random, n: int, d: dict, t: datetime, id_prefix: str = "") -> dict:
    stops = d["stops"]
    i = rng.randrange(len(stops))
    s_name = stops[i]
    s_lat, s_lon = station_point(d["line"], s_name)
    e_name = stops[-1] if i < len(stops) - 1 else stops[0]
    e_lat, e_lon = station_point(d["line"], e_name)
    scenario = "valid"
    near = lambda: rng.uniform(20, 200)  # noqa: E731
    sn, se = near(), near()
    roll = rng.random()
    if roll < 0.08:
        sn, scenario = rng.uniform(320, 600), "start too far from station"
    elif roll < 0.13:
        se, scenario = rng.uniform(320, 600), "end too far from station"
    start = offset(s_lat, s_lon, sn * rng.choice([-1, 1]) * 0.7, sn * 0.7)
    end = offset(e_lat, e_lon, se * 0.7, se * rng.choice([-1, 1]) * 0.7)
    prov, ttype, cvr = rng.choice(PROVIDERS)
    amount = round(rng.uniform(60, 450), 2)
    trip = t + timedelta(minutes=rng.randint(5, int(d["delayMinutes"]) + 20))
    receipt = {
        "receiptId": f"{id_prefix}R{n:05d}", "provider": prov, "cvr": cvr, "transportType": ttype,
        "startAddress": f"near {s_name}", "endAddress": f"near {e_name}",
        "distanceKm": round(rng.uniform(1.5, 12), 1), "minutes": rng.randint(6, 35),
        "time": iso(trip), "amountDkk": amount, "publiclyAvailable": True,
    }
    roll = rng.random()
    if roll < 0.06:
        receipt["cvr"], scenario = "", "receipt without CVR"
    elif roll < 0.10:
        receipt.update(provider="Neighbour car pool", transportType="private_car", publiclyAvailable=False)
        scenario = "private transport"
    return {"claimId": f"{id_prefix}C{n:04d}", "passenger": f"P{rng.randint(1, 400):04d}", "tripStart": iso(trip),
            "startLat": round(start[0], 6), "startLon": round(start[1], 6),
            "endLat": round(end[0], 6), "endLon": round(end[1], 6),
            "receipt": receipt, "scenario": scenario, "simulated": True}


def read_delays_csv(path: str) -> List[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r.get("delayId") or r["delayId"].startswith("#"):
                continue
            rows.append({
                "delayId": r["delayId"], "line": r.get("line") or "L",
                "stops": [s.strip() for s in (r.get("stops") or "").split(";") if s.strip()],
                "plannedDeparture": r["plannedDeparture"], "delayMinutes": float(r["delayMinutes"]),
                "detectedAt": r.get("detectedAt") or r["plannedDeparture"],
                "cause": r.get("cause") or None,
            })
    return rows


# ---- live mode: post to the bridge ----------------------------------------

def post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read() or b"{}")}


def wait_for_outcome(base: str, claim_id: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with urllib.request.urlopen(base.rstrip("/") + "/state", timeout=30) as r:
            if any(o["claimId"] == claim_id for o in json.loads(r.read())["outcomes"]):
                return
        time.sleep(1)
    print("no outcome for", claim_id, f"after {timeout:.0f} s; posting the next claim anyway")


def run_live(base: str, scenario: dict, pause: float = 3.0) -> None:
    for n in scenario["notices"]:
        post(base, "/cep/notice", n)
    for w in scenario["weather"]:
        post(base, "/cep/weather", w)
    for d in scenario["delays"]:
        print("delay", d["delayId"], post(base, "/cep/delay", d))
    time.sleep(pause)  # let the delay cases get assessed first
    first_use = {}  # receiptId -> first accepted claim with that receipt
    for c in scenario["claims"]:
        rid = c["receipt"].get("receiptId")
        if rid in first_use:
            # A resubmitted receipt arrives days after the first claim, as in the
            # offline run. Posting it at once would race the first claim to the
            # payout and leave an incident on C_Pay instead of "Receipt rejected".
            wait_for_outcome(base, first_use[rid])
        r = post(base, "/claims", c)
        print("claim", c["claimId"], r)
        if "status" not in r:
            first_use.setdefault(rid, c["claimId"])


def feed(base: str, batch: int = 5, interval: float = 3600.0, seed: Optional[int] = None) -> None:
    """Keep the bridge busy: every `interval` seconds, post `batch` delays that happened
    during the last interval, with their notices, weather and claims. IDs carry the batch
    time, so batches never collide with each other or with a restarted feed."""
    rng = random.Random(seed)
    while True:
        now = datetime.now(CPH)
        tag = now.strftime("%Y%m%d%H%M%S") + "-"
        delays = []
        for k in range(batch):
            minutes = rng.choice([12, 20, 31, 35, 40, 45, 55, 70, 95]) if rng.random() < 0.2 else rng.randint(30, 90)
            # departed long enough ago that the detour trips in the claims are over by now
            t = now - timedelta(minutes=minutes + 40, seconds=rng.uniform(0, interval))
            line = _line(rng)
            delays.append({
                "delayId": f"{tag}D{k + 1:03d}", "line": line, "stops": _segment(rng, line),
                "plannedDeparture": iso(t), "delayMinutes": float(minutes),
                "detectedAt": iso(t + timedelta(minutes=min(minutes, 30))),
                "cause": rng.choice([None] * 12 + ["fire", "person_collision"]),
            })
        sc = generate(seed=rng.randrange(1 << 30), delays=delays, id_prefix=tag)
        # generate() draws weather and notices per delay, assuming delays weeks apart. Here the whole
        # batch falls in one hour, and one storm or notice covers every delay in it, so keep only one
        # delay's worth per batch; otherwise nearly every claim ends up excluded.
        sc["weather"] = [w for w in sc["weather"] if rng.random() < 1 / batch]
        sc["notices"] = [n for n in sc["notices"] if rng.random() < 1 / batch]
        try:
            run_live(base, sc)
            print(f"batch {tag[:-1]}: {len(sc['delays'])} delays, {len(sc['claims'])} claims", flush=True)
        except OSError as e:  # bridge down or restarting: skip this batch, try again next time
            print(f"batch {tag[:-1]} not sent: {e}", flush=True)
        time.sleep(interval)


def claims_for(delay: dict) -> List[dict]:
    """Simulated claims for one real delay. IDs and choices depend only on the delay, so the same delay
    always gets the same claims: posting them again after a restart only gets HTTP 409."""
    key = hashlib.sha1(delay["delayId"].encode()).hexdigest()[:10]
    stops = [s for s in delay.get("stops", []) if station_point(delay["line"], s)]
    if not stops:
        return []
    rng = random.Random(int(key, 16))
    claims = generate(seed=rng.randrange(1 << 30), delays=[{**delay, "stops": stops}], id_prefix=key + "-")["claims"]
    # generate() adds one duplicate receipt and one claim at the wrong time per batch, sized for 40 delays;
    # for a batch of one, keep them about as rarely
    return [c for c in claims if c["scenario"] not in ("duplicate receipt", "no delay at that time") or rng.random() < 0.1]


def feed_real(base: str, interval: float = 300.0, settle_min: float = 40.0) -> None:
    """Simulate claims for the real delays the bridge has received (from the recorder). A delay gets its
    claims once it has played out, `settle_min` minutes after its delayed departure, so the claimed
    taxi trips are over. Weather and notices are not simulated: the recorder sends the real weather."""
    done = set()
    while True:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/state", timeout=30) as r:
                delays = json.loads(r.read())["delays"]
            now = datetime.now(CPH)
            for d in delays:
                if d["delayId"] in done or not rules.qualifies_as_delay(d):
                    continue
                if rules.delay_time(d) + timedelta(minutes=float(d["delayMinutes"]) + settle_min) > now:
                    continue
                claims = claims_for(d)
                run_live(base, {"notices": [], "weather": [], "delays": [], "claims": claims}, pause=0)
                done.add(d["delayId"])
                print(f"delay {d['delayId']}: {len(claims)} claims", flush=True)
        except OSError as e:  # bridge down or restarting: try again next time
            print(f"bridge not reachable: {e}", flush=True)
        time.sleep(interval)


# ---- offline mode: same handlers, no engine ------------------------------

def run_offline(scenario: dict, seed: int = 14, noise: float = 0.0):
    rng = random.Random(seed + 1)
    store, cep = Store(), CepSink(url="")
    for n in scenario["notices"]:
        store.put("notices", n["noticeId"], n)
    for w in scenario["weather"]:
        store.put("weather", w["situationId"], w)

    delay_traces, claim_traces = [], []
    for d in scenario["delays"]:
        if not rules.qualifies_as_delay(d):
            continue
        store.put("delays", d["delayId"], d)
        t = parse_ts(d["detectedAt"])
        tick = lambda: t + timedelta(seconds=rng.uniform(0.2, 2))  # noqa: E731
        v = {"delayId": d["delayId"]}
        ev = [(A.DELAY_DETECTED, t)]
        order = [(A.CHECK_NOTICE, workers.check_notice), (A.CHECK_FORCE_MAJEURE, workers.check_force_majeure)]
        rng.shuffle(order)
        for name, fn in order:
            v.update(fn(v, store, cep))
            t = tick()
            ev.append((name, t))
        t = tick()
        ev.append((A.NOTICE_BY_DAY_BEFORE if v["noticeExcluded"] else
                   A.FORCE_MAJEURE if v["forceMajeure"] else A.GUARANTEE_APPLIES, t))
        v.update(workers.report_assessment(v, store, cep))
        t = tick()
        ev += [(A.REPORT_ASSESSMENT, t), (A.DELAY_CASE_DONE, t)]
        delay_traces.append({"id": d["delayId"], "attrs": {"simulated": True},
                             "events": [{"name": n_, "time": iso(x)} for n_, x in ev]})

    refused = []
    for c in scenario["claims"]:
        if rules.missing_receipt_fields(c["receipt"]):
            refused.append(c["claimId"])  # refused by the claim form (bridge 422), no instance
            continue
        store.put("claims", c["claimId"], c)
        t = parse_ts(c["tripStart"]) + timedelta(hours=rng.uniform(1, 72))
        clock = [t]

        def step(name):
            clock[0] = clock[0] + timedelta(seconds=rng.uniform(0.2, 3))
            ev.append((name, clock[0]))

        v = {"claimId": c["claimId"]}
        ev = [(A.CLAIM_SUBMITTED, t)]
        v.update(workers.match_delay(v, store, cep))
        step(A.MATCH_DELAY)
        rejected = True
        if not v["delayVerified"]:
            step(A.NO_VERIFIED_DELAY)
        elif v["noticeExcluded"]:
            step(A.NOTICE_BY_DAY_BEFORE)
        elif v["forceMajeure"]:
            step(A.FORCE_MAJEURE)
        else:
            step(A.DELAY_VERIFIED)
            par = [(A.CHECK_RECEIPT, workers.check_receipt), (A.CHECK_DISTANCE, workers.check_distance)]
            rng.shuffle(par)
            for name, fn in par:
                v.update(fn(v, store, cep))
                step(name)
            if not v["receiptValid"]:
                step(A.RECEIPT_REJECTED)
            else:
                step(A.RECEIPT_VERIFIED)
                if not v["withinRadius"]:
                    step(A.TRIP_TOO_FAR)
                else:
                    step(A.TRIP_WITHIN)
                    v.update(workers.calc_payout(v, store, cep))
                    step(A.CALC_PAYOUT)
                    v.update(workers.pay_out(v, store, cep))
                    step(A.PAY_OUT)
                    rejected = False
        if rejected:
            v.update(workers.reject(v, store, cep))
            step(A.REJECT)
        v.update(workers.report_outcome(v, store, cep))
        step(A.REPORT_OUTCOME)
        ev.append((A.CLAIM_CLOSED, clock[0]))

        attrs = {"delayId": v.get("delayId"), "outcome": v["outcome"], "scenario": c.get("scenario"),
                 "payoutDkk": v.get("payoutDkk"), "simulated": True}
        names = [e[0] for e in ev]
        if noise and rng.random() < noise:
            names, label = _inject(rng, names)
            attrs["injectedDeviation"] = label
        times = [e[1] for e in ev]
        while len(times) < len(names):
            times.append(times[-1] + timedelta(seconds=1))
        claim_traces.append({"id": c["claimId"], "attrs": attrs,
                             "events": [{"name": n_, "time": iso(x)} for n_, x in zip(names, times)]})
    store.refused_at_intake = refused
    return delay_traces, claim_traces, store


def _inject(rng: random.Random, names: List[str]):
    """Deliberate rule violations, to show that the conformance check finds them."""
    kinds = []
    if A.RECEIPT_REJECTED in names:
        kinds.append("payout despite rejected receipt")
    if A.NOTICE_BY_DAY_BEFORE in names:
        kinds.append("payout despite notice by the day before")
    if A.PAY_OUT in names:
        kinds += ["double payout", "no outcome reported"]
    if not kinds:
        kinds.append("no outcome reported")
    label = rng.choice(kinds)
    n = list(names)
    if label in ("payout despite rejected receipt", "payout despite notice by the day before"):
        n[n.index(A.REJECT)] = A.PAY_OUT
    elif label == "double payout":
        n.insert(n.index(A.PAY_OUT) + 1, A.PAY_OUT)
    elif label == "no outcome reported":
        n.remove(A.REPORT_OUTCOME)
    return n, label
