"""Helpers for the travel guarantee: which delays the process takes, which delay a claim refers to, and
the notice or weather facts around a delay.

The TO-BE decisions themselves are DMN tables on Camunda (models/to-be/travel-guarantee-to-be.dmn): every
limit (20 min, 24 h, 14 days, 300 kr, the weather limits) is a rule there. This module only finds raw
facts for them. The AS-IS results page (asis.py) still uses the force majeure check below to fill the
AS-IS tables' inputs.

The process runs on whatever lines REJSEGARANTI_LINES selects (see stations.py); on the server those are
every Movia bus and S-tog line serving Copenhagen. A delay, a notice and the stations a claim is measured
against all belong to one line. All functions here are pure: they take plain dicts (as they travel through
Camunda and the CEP) and return plain values, so they are easy to test.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Iterator, Optional

from .stations import LINES, nearest_station
from .timeutil import local_date, parse_ts

# A claim's trip must start between the planned departure and this long
# after the delay has played out (the passenger gives up and takes a taxi).
MATCH_GRACE_MIN = 60

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
    """CEP situation is on one of the process's lines. How long the delay must be is a rule in the
    ToBe_Incident table (more than 20 minutes), not here; the recorder only reports 20+ minute delays."""
    return str(situation.get("line", "")).upper() in LINES


def delay_time(situation: dict) -> datetime:
    return parse_ts(situation.get("plannedDeparture") or situation["detectedAt"])


def _covers(stops: Iterable[str], affected: Iterable[str]) -> bool:
    stops, affected = list(stops or []), list(affected or [])
    if not stops or not affected:
        return True  # notice/situation for the whole line
    return bool(set(stops) & set(affected))


# ---- notices of planned changes ----------------------------------------------

def covering_notices(delay: dict, notices: Iterable[dict]) -> Iterator[dict]:
    """The notices of a planned change that cover this delay: its line (a notice without lines covers
    every line), its affected stops and the delay time. Nothing is decided here."""
    t = delay_time(delay)
    for n in notices:
        lines = [str(x).upper() for x in n.get("lines") or []]
        if lines and str(delay.get("line", "")).upper() not in lines:
            continue
        if not _covers(n.get("stops", []), delay.get("stops", [])):
            continue
        if parse_ts(n["effectiveFrom"]) <= t <= parse_ts(n["effectiveTo"]):
            yield n


def hours_announced_before(notice: dict) -> float:
    """Hours from when the recorder first saw the notice to when the change took effect (negative if it
    was first seen after). We only know when our recorder first saw a notice, not when it was published
    (see issue #6)."""
    return round((parse_ts(notice["effectiveFrom"]) - parse_ts(notice["firstSeen"])).total_seconds() / 3600, 2)


def covering_notice(delay: dict, notices: Iterable[dict]) -> Optional[dict]:
    """The covering notice announced longest before the change, or None. The ToBe_Exclusion table decides
    whether it was announced in time."""
    found = list(covering_notices(delay, notices))
    return max(found, key=hours_announced_before) if found else None


def notice_excludes(delay: dict, notices: Iterable[dict], min_notice: Optional[timedelta] = None) -> Optional[dict]:
    """Return the notice that excludes this delay, or None (the AS-IS rule).

    A covering notice excludes the delay when it was first seen on a day before the change took effect
    (local Copenhagen date), or with min_notice, at least that long before.
    """
    for n in covering_notices(delay, notices):
        start, seen = parse_ts(n["effectiveFrom"]), parse_ts(n["firstSeen"])
        if (seen <= start - min_notice) if min_notice else (local_date(seen) < local_date(start)):
            return n
    return None


# ---- exclusion 2: force majeure ------------------------------------------

def owm_exceeds(metrics: dict) -> list:
    """Names of OpenWeatherMap metrics at or above the thresholds."""
    return [k for k, lim in OWM_THRESHOLDS.items() if float(metrics.get(k, 0) or 0) >= lim]


def weather_at(delay: dict, weather: Iterable[dict]) -> dict:
    """The weather facts at the delay time, for the ToBe_Cause table: the highest level of the DMI warnings
    in force (dmiLevel) and the readings of the OpenWeatherMap observation valid then (windSpeed, windGust,
    rain1h, snow1h). None where nothing covers the delay time; the table decides what counts."""
    t = delay_time(delay)
    active = [w for w in weather if parse_ts(w["validFrom"]) <= t <= parse_ts(w["validTo"])]
    dmi = [int(w.get("level", 0)) for w in active if str(w.get("source", "")).upper() == "DMI"]
    owm = [w for w in active if str(w.get("source", "")).upper() == "OWM"]
    metrics = max(owm, key=lambda w: parse_ts(w["validFrom"])).get("metrics", {}) if owm else {}

    def reading(k):
        v = metrics.get(k)
        return None if v is None else float(v)
    return {"dmiLevel": max(dmi) if dmi else None, "windSpeed": reading("wind_speed"),
            "windGust": reading("wind_gust"), "rain1h": reading("rain_1h"), "snow1h": reading("snow_1h")}


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
    """Find the delay situation the claim refers to: the claim's trip starts nearest a stop of the delay."""
    t = parse_ts(claim["tripStart"])
    candidates = []
    for d in delays:
        if not qualifies_as_delay(d):
            continue
        start = delay_time(d)
        end = start + timedelta(minutes=float(d["delayMinutes"]) + MATCH_GRACE_MIN)
        if not start <= t <= end:
            continue
        station, _ = nearest_station(float(claim["startLat"]), float(claim["startLon"]), d["line"])
        if _covers([station], d.get("stops", [])):
            candidates.append(d)
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((delay_time(d) - t).total_seconds()))
