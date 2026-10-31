"""Business rules of the Line L rejsegaranti (travel guarantee).

Source: https://dinletbane.dk/da/kontakt-og-kundeservice/rejsegaranti/
(read 31 Oct 2026):

* Line L announced 30+ minutes late -> alternative transport is covered,
  up to 300 kr.
* The trip must start within 250 m of a Line L station hit by the stoppage,
  and end within 250 m of the nearest Line L station from which the journey
  can continue.
* The receipt must state start, end, distance, time, number of minutes and
  the amount in DKK.
* The transport must be publicly available and belong to a named,
  VAT-registered company with a CVR number, approved for commercial
  passenger transport.
* Exclusions: planned changes announced in good time, i.e. at the latest the
  day before; force majeure (fire, unusual natural events, person collisions).

All functions here are pure: they take plain dicts (as they travel through
Camunda and the CEP) and return plain values, so they are easy to test.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable, Optional

from .stations import STATION_BY_NAME, distance_m, nearest_station
from .timeutil import local_date, parse_ts

LINE = "L"
DELAY_THRESHOLD_MIN = 30
PAYOUT_CAP_DKK = 300
RADIUS_M = 250

# A claim's trip must start between the planned departure and this long
# after the delay has played out (the passenger gives up and takes a taxi).
MATCH_GRACE_MIN = 60

ALLOWED_TRANSPORT = {"taxi", "bike_share", "scooter_share", "car_share"}
RECEIPT_FIELDS = (
    "receiptId",
    "provider",
    "cvr",
    "transportType",
    "startAddress",
    "endAddress",
    "distanceKm",
    "minutes",
    "time",
    "amountDkk",
)

# ---- force majeure -------------------------------------------------------
# DMI warning levels: 1 "Voldsomt vejr", 2 "Farligt vejr" (may disrupt
# traffic), 3 "Meget farligt vejr". Level 2+ counts as force majeure.
MIN_DMI_LEVEL = 2

# Fallback when there is no DMI warning: OpenWeatherMap observations.
# These are the group's PROPOSED thresholds, to be justified in the report:
#  * wind_speed >= 24.5 m/s : Beaufort 10 ("storm")
#  * wind_gust  >= 32.7 m/s : Beaufort 12 (hurricane-force gusts)
#  * rain_1h    >= 30 mm    : DMI's cloudburst (skybrud) is 15 mm in 30 min;
#                             OWM only reports 1-hour totals, so 30 mm/h is
#                             the same intensity sustained over the hour
#  * snow_1h    >= 5 mm     : heavy snowfall (water equivalent)
OWM_THRESHOLDS = {
    "wind_speed": 24.5,
    "wind_gust": 32.7,
    "rain_1h": 30.0,
    "snow_1h": 5.0,
}

# Cause codes on a delay situation that are force majeure by definition.
FORCE_MAJEURE_CAUSES = {"fire", "person_collision", "natural_event"}


def qualifies_as_delay(situation: dict) -> bool:
    """CEP situation is a Line L delay of 30+ minutes."""
    return (
        str(situation.get("line", "")).upper() == LINE
        and float(situation.get("delayMinutes", 0)) >= DELAY_THRESHOLD_MIN
    )


def delay_time(situation: dict) -> datetime:
    return parse_ts(situation.get("plannedDeparture") or situation["detectedAt"])


def _covers(stops: Iterable[str], affected: Iterable[str]) -> bool:
    stops, affected = list(stops or []), list(affected or [])
    if not stops or not affected:
        return True  # notice/situation for the whole line
    return bool(set(stops) & set(affected))


# ---- exclusion 1: planned change announced by the day before -------------

def notice_excludes(delay: dict, notices: Iterable[dict]) -> Optional[dict]:
    """Return the notice that excludes this delay, or None.

    A notice excludes the delay when it covers Line L, covers the affected
    stops and the delay time, and was first seen on a day before the change
    took effect (local Copenhagen date). We only know when our recorder
    first saw a notice, not when it was published (see issue #6).
    """
    t = delay_time(delay)
    for n in notices:
        lines = [str(x).upper() for x in n.get("lines", [LINE])]
        if LINE not in lines:
            continue
        if not _covers(n.get("stops", []), delay.get("stops", [])):
            continue
        start, end = parse_ts(n["effectiveFrom"]), parse_ts(n["effectiveTo"])
        if not (start <= t <= end):
            continue
        if local_date(parse_ts(n["firstSeen"])) < local_date(start):
            return n
    return None


# ---- exclusion 2: force majeure ------------------------------------------

def owm_exceeds(metrics: dict) -> list:
    """Names of OpenWeatherMap metrics at or above the thresholds."""
    return [k for k, lim in OWM_THRESHOLDS.items() if float(metrics.get(k, 0) or 0) >= lim]


def force_majeure(delay: dict, weather: Iterable[dict]) -> Optional[str]:
    """Return a short reason if force majeure applies, otherwise None.

    DMI warnings are preferred; OWM thresholds are only used when no DMI
    warning covers the delay time.
    """
    cause = delay.get("cause")
    if cause in FORCE_MAJEURE_CAUSES:
        return f"cause:{cause}"
    t = delay_time(delay)
    active = [w for w in weather if parse_ts(w["validFrom"]) <= t <= parse_ts(w["validTo"])]
    dmi = [w for w in active if str(w.get("source", "")).upper() == "DMI"]
    if dmi:
        top = max(dmi, key=lambda w: int(w.get("level", 0)))
        if int(top.get("level", 0)) >= MIN_DMI_LEVEL:
            return f"DMI level {top['level']} ({top.get('type', 'warning')})"
        return None  # an official warning exists but is below the bar
    for w in active:
        if str(w.get("source", "")).upper() == "OWM":
            hit = owm_exceeds(w.get("metrics", {}))
            if hit:
                return "OWM " + ", ".join(hit)
    return None


# ---- claim checks ----------------------------------------------------------

def match_delay(claim: dict, delays: Iterable[dict]) -> Optional[dict]:
    """Find the Line L delay situation the claim refers to."""
    t = parse_ts(claim["tripStart"])
    station, _ = nearest_station(float(claim["startLat"]), float(claim["startLon"]))
    candidates = []
    for d in delays:
        if not qualifies_as_delay(d):
            continue
        start = delay_time(d)
        end = start + timedelta(minutes=float(d["delayMinutes"]) + MATCH_GRACE_MIN)
        if start <= t <= end and _covers([station], d.get("stops", [])):
            candidates.append(d)
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((delay_time(d) - t).total_seconds()))


def missing_receipt_fields(receipt: dict) -> list:
    return [f for f in RECEIPT_FIELDS if receipt.get(f) in (None, "")]


def check_receipt(receipt: dict, already_paid: Iterable[str] = ()) -> Optional[str]:
    """Return None if the receipt is acceptable, else the rejection reason."""
    missing = missing_receipt_fields(receipt)
    if missing:
        return "missing fields: " + ", ".join(missing)
    if not re.fullmatch(r"\d{8}", str(receipt["cvr"])):
        return "provider has no valid CVR number"
    if receipt["transportType"] not in ALLOWED_TRANSPORT:
        return f"transport type {receipt['transportType']!r} not covered"
    if receipt.get("publiclyAvailable") is False:
        return "transport is not publicly available"
    if float(receipt["amountDkk"]) <= 0:
        return "amount must be positive"
    if receipt["receiptId"] in set(already_paid):
        return "receipt already paid out"
    return None


def check_distance(claim: dict, delay: Optional[dict]) -> Optional[str]:
    """Return None if start and end are within 250 m of valid stations."""
    s_name, s_dist = nearest_station(float(claim["startLat"]), float(claim["startLon"]))
    affected = (delay or {}).get("stops") or []
    if affected:
        # distance to the closest *affected* station
        s_dist = min(
            distance_m(float(claim["startLat"]), float(claim["startLon"]), *STATION_BY_NAME[s])
            for s in affected
            if s in STATION_BY_NAME
        )
    if s_dist > RADIUS_M:
        return f"trip starts {s_dist:.0f} m from an affected station"
    e_name, e_dist = nearest_station(float(claim["endLat"]), float(claim["endLon"]))
    if e_dist > RADIUS_M:
        return f"trip ends {e_dist:.0f} m from the nearest station ({e_name})"
    return None


def payout(amount_dkk: float) -> float:
    return round(min(float(amount_dkk), PAYOUT_CAP_DKK), 2)
