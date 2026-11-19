"""External-task workers for the three TO-BE processes (models/to-be/).

Each handler takes the task's process variables and returns the variables to complete it with. The
decisions are the business rule tasks on Camunda; the handlers only gather the raw facts the tables read,
keep the registry and tell the CEP. Handlers are plain functions so tests can call them without Camunda.
`run_workers` is the polling loop against Camunda. HANDLERS maps each topic in activities.TOPICS to its
handler.
"""
from __future__ import annotations

import logging
import socket
import time
from typing import Callable, Dict

from . import activities as A
from . import rules
from .asis import service
from .camunda import Camunda, from_variables
from .cep import CepSink
from .intake import fare, receipt_id
from .store import Store
from .timeutil import iso, now

log = logging.getLogger("rejsegaranti.workers")


class NotReady(Exception):
    """Retry later (e.g. the delay has not been assessed yet)."""


Handler = Callable[[dict, Store, CepSink], dict]

# The operator's cause codes the ToBe_Cause table knows; anything else is "none"
OPERATOR_CAUSES = {"strike", "fire", "person_collision", "natural_event"}
# What a claim can say about the incident, for the ToBe_Incident table
INCIDENT_FIELDS = ("visibleToDriver", "busFull", "stopRequested", "minutesToNextDeparture")
COLLECTED = ("line", "service", "operatorCause", "dmiLevel", "windSpeed", "windGust", "rain1h", "snow1h",
             "hoursAnnouncedBefore", "delayMinutes")


# ---- assess disruption: tobe-assess-disruption ------------------------------

def collect_disruption(v: dict, store: Store, cep: CepSink) -> dict:
    """The raw facts the ToBe_Exclusion and ToBe_Cause tables read, from the registry."""
    delay = store.get("delays", v["delayId"])
    cause = delay.get("cause")
    notice = rules.covering_notice(delay, store.all("notices"))
    return {
        "service": service(str(delay["line"])),
        "operatorCause": cause if cause in OPERATOR_CAUSES else "none",
        **rules.weather_at(delay, store.all("weather")),
        "hoursAnnouncedBefore": rules.hours_announced_before(notice) if notice else None,
    }


def notify_passengers(v: dict, store: Store, cep: CepSink) -> dict:
    """Simulated: there is no passenger registry, so the CEP is told instead."""
    cep.emit("PassengersNotified", {"delayId": v["delayId"], "line": v.get("line"),
                                    "reprocessed": bool(v.get("reprocessed"))})
    return {}


def publish_assessment(v: dict, store: Store, cep: CepSink) -> dict:
    assessment = {
        "delayId": v["delayId"],
        "exclusion": v["exclusion"],
        "guaranteeApplies": v["exclusion"] == "none",
        **{k: v.get(k) for k in COLLECTED},
        "reprocessed": bool(v.get("reprocessed")),
    }
    store.put("assessments", v["delayId"], assessment)
    cep.emit("DelayAssessed", assessment)
    return {"guaranteeApplies": assessment["guaranteeApplies"]}


# ---- decide claim: tobe-decide-claim ------------------------------------------

def match_claim(v: dict, store: Store, cep: CepSink) -> dict:
    """The delay the claim refers to and its assessment. No delay: delayMinutes None, which the
    ToBe_Incident table answers with "No verified delay"."""
    claim = store.get("claims", v["claimId"])
    incident = {"incident": claim.get("incident") or "delay",
                **{k: claim.get(k) for k in INCIDENT_FIELDS}}
    delay = rules.match_delay(claim, store.all("delays"))
    if delay is None:
        return {"delayId": None, "delayMinutes": None, "exclusion": "none", **incident}
    a = store.get("assessments", delay["delayId"])
    if a is None or "exclusion" not in a:  # none yet, or one the archived Line L process stored
        raise NotReady(f"delay {delay['delayId']} not assessed yet")
    return {"delayId": delay["delayId"], "delayMinutes": float(delay["delayMinutes"]),
            "exclusion": a["exclusion"], **incident}


def pay_out(v: dict, store: Store, cep: CepSink) -> dict:
    claim = store.get("claims", v["claimId"])
    rid = receipt_id(claim)
    if rid and not store.mark_paid(rid, v["claimId"]):
        # the receipt was paid on another claim in the meantime: stop here and
        # leave an incident for a case worker
        raise RuntimeError("receipt was paid out on another claim in the meantime")
    return {"payoutReference": f"PAY-{v['claimId']}"}


def reject(v: dict, store: Store, cep: CepSink) -> dict:
    return {"rejectionReason": v.get("guaranteeReason") if v.get("claimValid") else v.get("claimReason")}


def send_decision(v: dict, store: Store, cep: CepSink) -> dict:
    paid = v.get("payoutReference") is not None
    outcome = {
        "claimId": v["claimId"],
        "delayId": v.get("delayId"),
        "outcome": "paid" if paid else "rejected",
        "payoutDkk": float(v.get("refundDkk") or 0) if paid else 0.0,
        "reason": None if paid else v.get("rejectionReason"),
        "reprocessed": bool(v.get("reprocessed")),
    }
    store.put("outcomes", v["claimId"], outcome)
    cep.emit("ClaimPaid" if paid else "ClaimRejected", outcome)
    return {"outcome": outcome["outcome"]}


# ---- handle appeal: tobe-handle-appeal ------------------------------------------

def retrieve_claim(v: dict, store: Store, cep: CepSink) -> dict:
    """The claim and its first decision, for the case worker. Without a receipt the fare is 0, so an
    upheld appeal pays nothing rather than failing the refund table."""
    claim = store.get("claims", v["claimId"]) or {}
    previous = store.get("outcomes", v["claimId"]) or {}
    amount = fare(claim)
    return {"fareDkk": 0.0 if amount is None else amount,
            "previousOutcome": previous.get("outcome"), "previousReason": previous.get("reason")}


def send_appeal_decision(v: dict, store: Store, cep: CepSink) -> dict:
    upheld = bool(v.get("upheld"))
    refund = float(v.get("refundDkk") or 0) if upheld else 0.0
    appeal = {"claimId": v["claimId"], "reason": v.get("appealReason"), "status": "decided",
              "upheld": upheld, "reviewNote": v.get("reviewNote"), "refundDkk": refund,
              "previousOutcome": v.get("previousOutcome"), "decidedAt": iso(now())}
    store.put("appeals", v["claimId"], appeal)
    if upheld:
        store.put("outcomes", v["claimId"], {"claimId": v["claimId"], "outcome": "paid", "payoutDkk": refund,
                                             "reason": None, "appealUpheld": True,
                                             "reprocessed": False})
    cep.emit("AppealDecided", appeal)
    return {"appealOutcome": "upheld" if upheld else "dismissed"}


BY_ACTIVITY: Dict[str, Handler] = {
    A.COLLECT_DISRUPTION: collect_disruption,
    A.NOTIFY_PASSENGERS: notify_passengers,
    A.PUBLISH_ASSESSMENT: publish_assessment,
    A.MATCH_CLAIM: match_claim,
    A.PAY_OUT: pay_out,
    A.REJECT_INVALID: reject,
    A.REJECT_NOT_COVERED: reject,
    A.SEND_DECISION: send_decision,
    A.RETRIEVE_CLAIM: retrieve_claim,
    A.SEND_APPEAL_DECISION: send_appeal_decision,
}
HANDLERS: Dict[str, Handler] = {A.TOPICS[a]: fn for a, fn in BY_ACTIVITY.items()}


def run_workers(camunda: Camunda, store: Store, cep: CepSink, stop=None) -> None:
    worker_id = f"rejsegaranti-{socket.gethostname()}"
    topics = sorted(HANDLERS)
    log.info("worker %s polling %s", worker_id, ", ".join(topics))
    while not (stop and stop.is_set()):
        try:
            tasks = camunda.fetch_and_lock(worker_id, topics)
        except Exception as e:
            log.warning("fetchAndLock failed (%s); is Camunda running at %s?", e, camunda.base)
            time.sleep(5)
            continue
        for t in tasks:
            v = from_variables(t.get("variables", {}))
            handler = HANDLERS[t["topicName"]]
            retries = t.get("retries")
            retries = 5 if retries is None else retries
            try:
                out = handler(v, store, cep)
                camunda.complete(t["id"], worker_id, out)
                log.info("%-24s %s -> %s", t["topicName"], t.get("businessKey"), out)
            except NotReady as e:
                _report(camunda, t, worker_id, str(e), max(retries - 1, 0), 2000)
            except Exception as e:  # incident after the last retry
                log.exception("task %s failed", t["topicName"])
                _report(camunda, t, worker_id, f"{type(e).__name__}: {e}", 0, 0)


def _report(camunda: Camunda, t: dict, worker_id: str, message: str, retries: int, retry_ms: int) -> None:
    """Report a failed task. If that fails too (the instance was deleted meanwhile, Camunda restarting),
    log it and go on: the loop must keep running."""
    try:
        camunda.failure(t["id"], worker_id, message, retries, retry_ms)
    except Exception as e:  # noqa: BLE001
        log.warning("could not report the failure of %s: %s", t["topicName"], e)
