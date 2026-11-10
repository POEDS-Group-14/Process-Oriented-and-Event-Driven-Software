"""Per-line rates: delay cases and claims per 1,000 scheduled journeys, so lines of very different frequency
can be compared (5C runs about 490 journeys a weekday, 142 about 64).

  rate = cases / exposure * 1000

Exposure is the line's scheduled journeys (data/journeys.csv, from GTFS by tools/build_stations.py) on the
dates observed, times the share of each date the bridge was receiving data. That share comes from the weather
situations: the recorder posts one whenever OpenWeatherMap has a new observation, every 10 to 20 minutes, so the
time from one post to the next is time the recorder and bridge were both up. A gap of more than MAX_GAP counts
as down from the end of the earlier post's validity. Only time from SINCE counts: before it the bridge ran on bus 150S alone, and
other lines' delays were not reported (docs/process-log.md, section 11).

The 95% interval is Byar's approximation to the exact Poisson interval of the case count. A line with few
journeys gets a wide interval, so one unlucky journey can give it the highest rate. Lines are therefore
ranked by the interval's lower end: first come the lines that are late often even in the most favourable
reading of the data.
"""
from __future__ import annotations

import csv
import math
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

from . import rules
from .stations import DATA, LINES
from .timeutil import local_date, parse_ts

SINCE = parse_ts("2026-11-10T15:00:00+01:00")  # the bridge ran on every Copenhagen line from here
SLOT = timedelta(minutes=10)
MAX_GAP = timedelta(minutes=30)
Z = 1.96


def load_journeys(path: str = os.path.join(DATA, "journeys.csv")) -> Dict[Tuple[str, date], int]:
    with open(path, encoding="utf-8") as fh:
        return {(r["line"].upper(), date.fromisoformat(r["date"])): int(r["journeys"]) for r in csv.DictReader(fh)}


def coverage(weather: List[dict], since: datetime = SINCE) -> Dict[date, float]:
    """Local date -> share of it (0..1) covered by weather situations, from `since`."""
    spans = sorted((parse_ts(w["validFrom"]), parse_ts(w["validTo"])) for w in weather)
    slots = set()
    for i, (t, end) in enumerate(spans):
        if i + 1 < len(spans) and spans[i + 1][0] - t <= MAX_GAP:
            end = max(end, spans[i + 1][0])  # up until the next post
        t = datetime.fromtimestamp(t.timestamp() // SLOT.total_seconds() * SLOT.total_seconds(), t.tzinfo)
        while t < end:
            if t >= since:
                slots.add(t.timestamp())
            t += SLOT
    per_day = Counter(local_date(datetime.fromtimestamp(s, since.tzinfo)) for s in slots)
    return {d: n / (timedelta(days=1) / SLOT) for d, n in per_day.items()}


def poisson_interval(k: int) -> Tuple[float, float]:
    """95% interval for a Poisson count k (Byar; exact for k = 0, where Byar is 0.6% low)."""
    if k == 0:
        return 0.0, -math.log(0.025)
    low = k * (1 - 1 / (9 * k) - Z / (3 * math.sqrt(k))) ** 3
    k1 = k + 1
    return low, k1 * (1 - 1 / (9 * k1) + Z / (3 * math.sqrt(k1))) ** 3


def per_line(state: dict, journeys: Dict[Tuple[str, date], int], since: datetime = SINCE) -> List[dict]:
    """One row per line, ranked by the lower end of the rate's interval, then a "total" row.
    `state` is the bridge's /state."""
    cover = coverage(state.get("weather", []), since)
    exposure = defaultdict(float)
    for (line, d), n in journeys.items():
        if line in LINES and d in cover:
            exposure[line] += n * cover[d]
    delays = {d["delayId"]: d for d in state.get("delays", [])
              if rules.qualifies_as_delay(d) and rules.delay_time(d) >= since}
    cases = Counter(d["line"].upper() for d in delays.values())
    claims, paid, payout = Counter(), Counter(), Counter()
    for o in state.get("outcomes", []):
        d = delays.get(o.get("delayId"))
        if d:
            line = d["line"].upper()
            claims[line] += 1
            paid[line] += o["outcome"] == "paid"
            payout[line] += float(o.get("payoutDkk") or 0)

    def row(line, exp, k, c, p, dkk):
        low, high = poisson_interval(k)
        per = 1000 / exp if exp else math.nan
        return {"line": line, "journeys": round(exp), "cases": k, "casesPer1000": k * per,
                "low": low * per, "high": high * per, "claims": c, "paid": p, "payoutDkk": round(dkk, 2),
                "payoutPer1000": dkk * per}

    rows = [row(line, exposure[line], cases[line], claims[line], paid[line], payout[line])
            for line in sorted(set(exposure) | set(cases))]
    rows.sort(key=lambda r: (not r["journeys"], -r["low"] if r["journeys"] else 0, -r["cases"], r["line"]))
    rows.append(row("total", sum(exposure.values()), sum(cases.values()), sum(claims.values()),
                    sum(paid.values()), sum(payout.values())))
    return rows


FIELDS = ["line", "journeys", "cases", "casesPer1000", "low", "high", "claims", "paid", "payoutDkk", "payoutPer1000"]


def write_csv(path: str, rows: List[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: f"{v:.3f}" if isinstance(v, float) else v for k, v in r.items()})
