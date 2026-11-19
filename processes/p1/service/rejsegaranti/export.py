"""Build XES event logs from Camunda history (issue #11).

One trace per process instance (case id = business key: delayId or
claimId). Events are the completed tasks (business rule tasks included),
milestone (intermediate throw) events and start/end events; gateways are
left out. The roles for the org:resource attribute are activities.ROLES.
"""
from __future__ import annotations

from typing import List

from .activities import APPEAL_KEY, ASSESS_KEY, CLAIM_KEY
from .camunda import Camunda

KEEP_TYPES = {
    "serviceTask", "sendTask", "userTask", "task", "businessRuleTask", "intermediateNoneThrowEvent",
    "startEvent", "messageStartEvent", "noneEndEvent", "endEvent",
    "intermediateMessageCatch", "intermediateTimer",
}

# process key -> file name of its log
KEYS = {ASSESS_KEY: "delay-cases-camunda", CLAIM_KEY: "claims-camunda", APPEAL_KEY: "appeals-camunda"}
ATTRS = ("delayId", "exclusion", "guaranteeApplies", "claimValid", "claimReason", "covered", "guaranteeReason",
         "outcome", "refundDkk", "rejectionReason", "upheld", "reprocessed")


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
        attrs = {k: v.get(k) for k in ATTRS if v.get(k) is not None}
        attrs["processInstanceId"] = pi["id"]
        out.append({
            "id": pi.get("businessKey") or pi["id"],
            "attrs": attrs,
            "events": [{"name": a["activityName"], "time": a["endTime"],
                        "attrs": {"activityType": a["activityType"]}} for a in acts],
        })
    return out
