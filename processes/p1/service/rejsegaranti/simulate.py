"""Simulated input for process 1 (issue #9).

Claims are always simulated (there is no public claims data). On the server the delays and weather are
real (the recorder posts them); `feed --real` makes up claims for those delays. `simulate` and `feed`
without --real make up delays, weather and notices as well. Everything is sent to the running bridge
(--post), and so to Camunda.

The claims are Movia-style: a taxi trip on a receipt, by a passenger whose bus or local train was late. The
scenarios are the cases the TO-BE tables (models/to-be/travel-guarantee-to-be.dmn) tell apart:

  valid                     receipt attached, fare at most 300 kr, the trip during the delay
  no receipt                the claim has no receipt                        -> "No receipt"
  fare over 300             fare above the cap                              -> refund capped at 300 kr
  duplicate receipt         a second claim with an earlier claim's receipt  -> "Receipt already claimed"
  no delay at that time     the trip is hours after the delay               -> "No verified delay"
  long after the delay      the trip is after the delay has played out      -> "No verified delay"

Delays of 20 minutes or less ("Delay 20 min or less"), notices and storms come from the delays themselves.
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

from . import rules
from .stations import LINES, STATIONS, offset, station_point
from .timeutil import CPH, iso, parse_ts

# Movia refunds taxis only. The names are only labels on simulated receipts.
TAXIS = ["Dantaxi 4x48", "Taxa 4x35", "Dantaxi Nord", "Taxi Sjælland"]
FARE_CAP_DKK = 300  # only to pick fares on either side of the cap; the cap itself is in ToBe_Refund


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
    """A scenario: delays (unless given), their notices and weather, and claims. Without `start` the
    delays fall in the 10 days before today, so the claims, received now, are within the 14-day deadline."""
    rng = random.Random(seed)
    out = {"delays": [], "weather": [], "notices": [], "claims": []}

    if delays is None:
        if start is None:
            start = datetime.now(CPH).replace(hour=6, minute=0, second=0, microsecond=0) - timedelta(days=10)
        delays = []
        for k in range(n_delays):
            t = start + timedelta(days=rng.randint(0, 9), minutes=rng.randint(0, 17 * 60))
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
    with_receipt = [c for c in out["claims"] if c.get("receipt")]
    for c in rng.sample(with_receipt, k=min(len(with_receipt), max(1, len(out["claims"]) // 25))):
        n += 1
        dup = json.loads(json.dumps(c))
        dup["claimId"] = f"{id_prefix}C{n:04d}"
        dup["scenario"] = "duplicate receipt"
        out["claims"].append(dup)
    # claims that match no delay: hours later, or just after the delay has played out
    for _ in range(max(1, len(delays) // 10)):
        n += 1
        d = rng.choice(delays)
        c = _claim(rng, n, d, parse_ts(d["plannedDeparture"]) + timedelta(hours=8), id_prefix)
        c["scenario"] = "no delay at that time"
        out["claims"].append(c)
    for _ in range(max(1, len(delays) // 10)):
        n += 1
        d = rng.choice(delays)
        over = timedelta(minutes=float(d["delayMinutes"]) + rules.MATCH_GRACE_MIN + rng.randint(30, 120))
        c = _claim(rng, n, d, parse_ts(d["plannedDeparture"]) + over, id_prefix, offset_min=(0, 0))
        c["scenario"] = "long after the delay"
        out["claims"].append(c)
    return out


def _claim(rng: random.Random, n: int, d: dict, t: datetime, id_prefix: str = "",
           offset_min: Optional[tuple] = None) -> dict:
    """One claim for delay d; the trip starts a few minutes after t (offset_min: (low, high) minutes)."""
    stops = d["stops"]
    i = rng.randrange(len(stops))
    s_name = stops[i]
    s_lat, s_lon = station_point(d["line"], s_name)
    e_name = stops[-1] if i < len(stops) - 1 else stops[0]
    e_lat, e_lon = station_point(d["line"], e_name)
    sn, se = rng.uniform(20, 200), rng.uniform(20, 200)
    start = offset(s_lat, s_lon, sn * rng.choice([-1, 1]) * 0.7, sn * 0.7)
    end = offset(e_lat, e_lon, se * 0.7, se * rng.choice([-1, 1]) * 0.7)
    low, high = offset_min or (5, int(d["delayMinutes"]) + 20)
    trip = t + timedelta(minutes=rng.randint(low, high))
    scenario, amount = "valid", round(rng.uniform(60, FARE_CAP_DKK), 2)
    roll = rng.random()
    if roll < 0.07:
        scenario = "no receipt"
    elif roll < 0.22:
        scenario, amount = "fare over 300", round(rng.uniform(FARE_CAP_DKK + 10, 650), 2)
    claim = {"claimId": f"{id_prefix}C{n:04d}", "passenger": f"P{rng.randint(1, 400):04d}", "tripStart": iso(trip),
             "startLat": round(start[0], 6), "startLon": round(start[1], 6),
             "endLat": round(end[0], 6), "endLon": round(end[1], 6),
             "incident": "delay", "scenario": scenario, "simulated": True}
    if scenario != "no receipt":
        claim["receipt"] = {
            "receiptId": f"{id_prefix}R{n:05d}", "provider": rng.choice(TAXIS), "transportType": "taxi",
            "startAddress": f"near {s_name}", "endAddress": f"near {e_name}",
            "distanceKm": round(rng.uniform(1.5, 12), 1), "minutes": rng.randint(6, 35),
            "time": iso(trip), "amountDkk": amount,
        }
    return claim


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


def run_live(base: str, scenario: dict, pause: float = 3.0) -> None:
    """Post a scenario to the bridge: notices and weather first, then the delays, then (after `pause`, so
    the delays are assessed first) the claims in order. A duplicate receipt needs no wait: the bridge
    stores each claim before the next is posted, so the later claim sees the earlier one."""
    for n in scenario["notices"]:
        post(base, "/cep/notice", n)
    for w in scenario["weather"]:
        post(base, "/cep/weather", w)
    for d in scenario["delays"]:
        print("delay", d["delayId"], post(base, "/cep/delay", d))
    time.sleep(pause)
    for c in scenario["claims"]:
        print("claim", c["claimId"], post(base, "/claims", c))


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
    # generate() adds a duplicate receipt and claims at the wrong time to every batch, sized for 40 delays;
    # for a batch of one, keep them about as rarely
    rare = ("duplicate receipt", "no delay at that time", "long after the delay")
    return [c for c in claims if c["scenario"] not in rare or rng.random() < 0.1]


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
