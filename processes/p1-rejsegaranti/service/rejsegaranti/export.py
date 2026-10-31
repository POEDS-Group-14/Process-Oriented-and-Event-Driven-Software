"""Build XES event logs from Camunda history (issue #11).

One trace per process instance (case id = business key: claimId or
delayId). Events are the completed tasks, milestone (intermediate throw)
events and start/end events; gateways are left out.
"""
from __future__ import annotations

from typing import List

from .camunda import Camunda

KEEP_TYPES = {
    "serviceTask", "sendTask", "userTask", "task", "intermediateNoneThrowEvent",
    "startEvent", "messageStartEvent", "noneEndEvent", "endEvent",
    "intermediateMessageCatch", "intermediateTimer",
}

CLAIM_KEY = "rejsegaranti-claim"
DELAY_KEY = "rejsegaranti-delay-case"


def traces_from_camunda(cam: Camunda, key: str, finished_only: bool = True) -> List[dict]:
    out = []
    for pi in cam.process_instances(key):
        if finished_only and not pi.get("endTime"):
            continue
        acts = [a for a in cam.activity_instances(pi["id"])
                if a.get("activityType") in KEEP_TYPES and a.get("activityName")
                and a.get("endTime") and not a.get("canceled")]
        acts.sort(key=lambda a: (a["endTime"], a["startTime"]))
        v = cam.variables(pi["id"])
        attrs = {k: v.get(k) for k in ("delayId", "outcome", "payoutDkk", "rejectionReason",
                                        "exclusionReason", "guaranteeApplies") if v.get(k) is not None}
        attrs["processInstanceId"] = pi["id"]
        out.append({
            "id": pi.get("businessKey") or pi["id"],
            "attrs": attrs,
            "events": [{"name": a["activityName"], "time": a["endTime"],
                        "attrs": {"activityType": a["activityType"]}} for a in acts],
        })
    return out
