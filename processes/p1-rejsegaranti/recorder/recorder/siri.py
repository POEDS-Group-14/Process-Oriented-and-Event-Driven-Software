"""Parse SIRI XML from the Dataudveksleren real-time feed.

SIRI (Service Interface for Real Time Information, CEN/TS 15531) messages:
- SIRI-ET (Estimated Timetable): per vehicle journey, the aimed (planned), expected
  (predicted) and, for calls already made, actual times at each stop.
- SIRI-SX (Situation Exchange): disruption messages, including planned changes.

Element names are SIRI's standard names. Namespaces are ignored, so SIRI 2.0 and 2.1 both work.
Which of these message types Movia's dataset actually carries must be checked against the
raw messages once the feed is connected; the raw XML is always stored first.

Incidents found in SIRI-ET (thresholds from the Movia travel guarantee):
- delay:            departure (or arrival at the last stop) at least 20 min after the aimed time
- early_departure:  ACTUAL departure more than 2 min before the aimed time
- cancelled:        the call or the whole journey is cancelled
Each delay row says whether the time is "expected" (a prediction) or "actual" (recorded).
The guarantee text says a predicted +20 min on Rejseplan is not enough on its own, so the
difference matters for the claim check.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

INCIDENT_FIELDS = ["incidentId", "type", "minutes", "timeKind", "operator", "line", "journeyRef", "stopRef",
                   "stopName", "aimedTime", "observedTime", "detectedAt", "receivedAt"]
SITUATION_FIELDS = ["situationNumber", "version", "creationTime", "firstSeen", "planned", "validFrom", "validTo",
                    "lines", "stops", "reason", "summary", "receivedAt"]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = _local(el.tag)
    return root


def _t(el, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _ts(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 60.0, 1)


def _true(s: str) -> bool:
    return s.lower() == "true"


def message_kind(xml: str) -> str:
    root = _strip(ET.fromstring(xml))
    if root.find(".//EstimatedVehicleJourney") is not None:
        return "ET"
    if root.find(".//PtSituationElement") is not None:
        return "SX"
    return "other"


def parse_et(xml: str, received: datetime, delay_min: float = 20, early_min: float = 2,
             names: dict | None = None) -> list:
    """Incident rows. Movia sends StopPointName only when it sends a journey in full; updates carry just
    StopPointRef. Pass the same `names` dict (stopRef -> name) every time: it learns from calls that
    carry a name and fills in the ones that don't."""
    names = {} if names is None else names
    root = _strip(ET.fromstring(xml))
    stamp = _t(root, ".//ResponseTimestamp")
    out = []
    for vj in root.iter("EstimatedVehicleJourney"):
        operator = _t(vj, "OperatorRef")
        line = _t(vj, "LineRef") or _t(vj, "PublishedLineName")
        frame, dated = _t(vj, "FramedVehicleJourneyRef/DataFrameRef"), _t(vj, "FramedVehicleJourneyRef/DatedVehicleJourneyRef")
        journey = f"{frame}:{dated}" if dated else (_t(vj, "DatedVehicleJourneyRef") or _t(vj, "EstimatedVehicleJourneyCode"))
        detected = _t(vj, "RecordedAtTime") or stamp or received.isoformat()
        journey_cancelled = _true(_t(vj, "Cancellation"))
        calls = [(c, "actual") for c in vj.iter("RecordedCall")] + [(c, "expected") for c in vj.iter("EstimatedCall")]
        for call, kind in calls:
            stop, name = _t(call, "StopPointRef"), _t(call, "StopPointName")
            if name:
                names[stop] = name
            else:
                name = names.get(stop, "")
            base = {"operator": operator, "line": line, "journeyRef": journey, "stopRef": stop, "stopName": name,
                    "detectedAt": detected, "receivedAt": received.isoformat()}
            aimed_dep = _t(call, "AimedDepartureTime")
            if journey_cancelled or _true(_t(call, "Cancellation")):
                out.append({**base, "incidentId": f"{operator}|{journey}|{stop}|cancelled", "type": "cancelled",
                            "minutes": "", "timeKind": "", "aimedTime": aimed_dep or _t(call, "AimedArrivalTime"),
                            "observedTime": ""})
                continue
            # departure time, or arrival time at the last stop (no departure there)
            for aimed_tag, exp_tag, act_tag in (("AimedDepartureTime", "ExpectedDepartureTime", "ActualDepartureTime"),
                                                ("AimedArrivalTime", "ExpectedArrivalTime", "ActualArrivalTime")):
                aimed = _ts(_t(call, aimed_tag))
                if aimed is None:
                    continue
                actual, expected = _ts(_t(call, act_tag)), _ts(_t(call, exp_tag))
                observed, tk = (actual, "actual") if actual else (expected, "expected")
                if observed is None:
                    break
                m = _minutes(aimed, observed)
                rec = {**base, "minutes": m, "timeKind": tk, "aimedTime": aimed.isoformat(),
                       "observedTime": observed.isoformat()}
                if m >= delay_min:
                    out.append({**rec, "incidentId": f"{operator}|{journey}|{stop}|delay", "type": "delay"})
                elif tk == "actual" and aimed_tag == "AimedDepartureTime" and m < -early_min:
                    out.append({**rec, "incidentId": f"{operator}|{journey}|{stop}|early", "type": "early_departure"})
                break
    return out


def parse_sx(xml: str, received: datetime) -> list:
    root = _strip(ET.fromstring(xml))
    out = []
    for s in root.iter("PtSituationElement"):
        periods = s.findall("ValidityPeriod")
        lines = sorted({(e.text or "").strip() for e in s.iter("LineRef") if e.text})
        stops = sorted({(e.text or "").strip() for e in s.iter("StopPointRef") if e.text})
        reason = next((_t(s, tag) for tag in ("ReasonName", "MiscellaneousReason", "EnvironmentReason",
                                               "EquipmentReason", "PersonnelReason") if _t(s, tag)), "")
        for p in periods or [None]:
            out.append({
                "situationNumber": _t(s, "SituationNumber"),
                "version": _t(s, "Version"),
                "creationTime": _t(s, "CreationTime"),
                "firstSeen": "",  # filled in by the recorder from its own first-seen registry
                "planned": _t(s, "Planned"),
                "validFrom": _t(p, "StartTime"),
                "validTo": _t(p, "EndTime"),
                "lines": ";".join(lines),
                "stops": ";".join(stops),
                "reason": reason,
                "summary": _t(s, "Summary"),
                "receivedAt": received.isoformat(),
            })
    return out
