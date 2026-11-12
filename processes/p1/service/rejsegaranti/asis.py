"""The stored claims judged by the AS-IS rules: the four DMN tables of the Movia travel guarantee
(models/travel-guarantee/travel-guarantee.dmn), deployed on Camunda.

The order follows the AS-IS process (travel-guarantee-process.bpmn): Table 1 "Claim valid" first, then
Tables 2 to 4 through "Travel guarantee applies", then the refund, capped at 300 kr per taxi. A claim is one
leg. The tables decide; this module only reads their inputs from what the bridge has stored:

  receiptAttached  the claim has a receipt with an amount
  daysToClaim      calendar days (Copenhagen) from the trip to Camunda receiving the claim
  service          S-tog lines ("local train") have letters only; Movia's bus lines have a number
  cause            the operator's cause code (fire, person collision: force majeure), or weather if the
                   delay case's weather check found it; else none
  plannedChange    a notice of the change was first seen at least 24 hours before it started
  incident         "delay over 20 min" for the delay the claim was matched to. A claim without one has no
                   incident the tables know, so it is not covered without asking them

Only the outcome is computed here; the stored registry and Camunda's history are only read.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from . import rules
from .camunda import Camunda
from .store import Store
from .timeutil import local_date, parse_ts

CLAIM_KEY = "rejsegaranti-claim"
REFUND_CAP_DKK = 300.0  # the AS-IS task "Calculate refund: taxi fare, max 300 kr per taxi"
NO_DELAY = "No delay over 20 min found"

INCIDENT_INPUTS = ("visibleToDriver", "busFull", "stopRequested", "nextWithin20")

_cache: Dict[Tuple[str, tuple], dict] = {}  # (decision, inputs) -> result; the tables don't change while running


def _decide(camunda: Camunda, key: str, inputs: dict) -> dict:
    k = (key, tuple(sorted(inputs.items())))
    if k not in _cache:
        _cache[k] = camunda.evaluate(key, inputs)
    return _cache[k]


def service(line: str) -> str:
    return "local train" if line.isalpha() else "bus"


def cause(delay: dict, assessment: Optional[dict]) -> str:
    if delay.get("cause") in rules.FORCE_MAJEURE_CAUSES:
        return "force majeure"
    if (assessment or {}).get("forceMajeure"):
        return "weather"  # the delay case's check: a DMI warning or OpenWeatherMap readings
    return "none"


def received_dates(camunda: Camunda) -> Dict[str, str]:
    """claimId -> when Camunda received the claim (start of its first claim instance)."""
    out: Dict[str, str] = {}
    for pi in camunda.process_instances(CLAIM_KEY):
        if pi.get("businessKey") and pi.get("startTime"):
            out.setdefault(pi["businessKey"], pi["startTime"])
    return out


def judge(store: Store, camunda: Camunda) -> List[dict]:
    """One row per stored claim: claimId, line, tripStart, amountDkk, outcome (paid/rejected), refundDkk, reason."""
    delays = {d["delayId"]: d for d in store.all("delays")}
    outcomes = {o["claimId"]: o for o in store.all("outcomes")}
    notices = store.all("notices")
    received = received_dates(camunda)
    rows = []
    for c in store.all("claims"):
        r = c.get("receipt") or {}
        amount = r.get("amountDkk")
        trip = parse_ts(c["tripStart"])
        got = parse_ts(received[c["claimId"]]) if c["claimId"] in received else trip
        row = {"claimId": c["claimId"], "tripStart": c["tripStart"], "amountDkk": amount, "line": None,
               "outcome": "rejected", "refundDkk": 0.0}
        valid = _decide(camunda, "Decision_ClaimValid", {
            "receiptAttached": amount not in (None, ""),
            "daysToClaim": (local_date(got) - local_date(trip)).days,
        })
        if not valid.get("claimValid"):
            rows.append({**row, "reason": valid.get("claimReason")})
            continue
        o = outcomes.get(c["claimId"])
        delay_id = o.get("delayId") if o else (rules.match_delay(c, delays.values()) or {}).get("delayId")
        delay = delays.get(delay_id)
        if not delay or float(delay.get("delayMinutes", 0)) <= 20:
            rows.append({**row, "reason": NO_DELAY})
            continue
        row["line"] = delay["line"]
        decision = _decide(camunda, "Decision_TravelGuarantee", {
            "service": service(delay["line"]),
            "cause": cause(delay, store.get("assessments", delay["delayId"])),
            "plannedChange": rules.notice_excludes(delay, notices, min_notice=timedelta(hours=24)) is not None,
            "incident": "delay over 20 min",
            **{k: None for k in INCIDENT_INPUTS},
        })
        if decision.get("covered"):
            rows.append({**row, "outcome": "paid", "refundDkk": round(min(float(amount), REFUND_CAP_DKK), 2),
                         "reason": None})
        else:
            rows.append({**row, "reason": decision.get("reason")})
    return rows
