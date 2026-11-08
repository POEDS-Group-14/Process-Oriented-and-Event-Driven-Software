"""External-task workers for the two TO-BE processes.

Each handler takes the task's process variables and returns the variables
to complete it with. Handlers are plain functions so tests can call them
without Camunda. `run_workers` is the polling loop against Camunda.
"""
from __future__ import annotations

import logging
import socket
import time
from typing import Callable, Dict

from . import rules
from .camunda import Camunda, from_variables
from .cep import CepSink
from .store import Store

log = logging.getLogger("rejsegaranti.workers")


class NotReady(Exception):
    """Retry later (e.g. the delay case has not been assessed yet)."""


Handler = Callable[[dict, Store, CepSink], dict]


# ---- delay case: rejsegaranti-delay-case ---------------------------------

def check_notice(v: dict, store: Store, cep: CepSink) -> dict:
    delay = store.get("delays", v["delayId"])
    n = rules.notice_excludes(delay, store.all("notices"))
    return {"noticeExcluded": n is not None, "noticeId": n["noticeId"] if n else None}


def check_force_majeure(v: dict, store: Store, cep: CepSink) -> dict:
    delay = store.get("delays", v["delayId"])
    reason = rules.force_majeure(delay, store.all("weather"))
    return {"forceMajeure": reason is not None, "forceMajeureReason": reason}


def report_assessment(v: dict, store: Store, cep: CepSink) -> dict:
    applies = not (v.get("noticeExcluded") or v.get("forceMajeure"))
    reason = None
    if v.get("noticeExcluded"):
        reason = f"planned change announced by the day before (notice {v.get('noticeId')})"
    elif v.get("forceMajeure"):
        reason = f"force majeure: {v.get('forceMajeureReason')}"
    assessment = {
        "delayId": v["delayId"],
        "guaranteeApplies": applies,
        "noticeExcluded": bool(v.get("noticeExcluded")),
        "forceMajeure": bool(v.get("forceMajeure")),
        "exclusionReason": reason,
    }
    store.put("assessments", v["delayId"], assessment)
    cep.emit("DelayAssessed", assessment)
    return {"guaranteeApplies": applies}


# ---- claim: rejsegaranti-claim -------------------------------------------

def match_delay(v: dict, store: Store, cep: CepSink) -> dict:
    claim = store.get("claims", v["claimId"])
    delay = rules.match_delay(claim, store.all("delays"))
    if delay is None:
        return {"delayVerified": False, "delayId": None, "noticeExcluded": False,
                "forceMajeure": False, "exclusionReason": "no verified Line L delay of 30+ minutes"}
    a = store.get("assessments", delay["delayId"])
    if a is None:
        raise NotReady(f"delay {delay['delayId']} not assessed yet")
    return {"delayVerified": True, "delayId": delay["delayId"],
            "noticeExcluded": a["noticeExcluded"], "forceMajeure": a["forceMajeure"],
            "exclusionReason": a["exclusionReason"]}


def check_receipt(v: dict, store: Store, cep: CepSink) -> dict:
    claim = store.get("claims", v["claimId"])
    reason = rules.check_receipt(claim["receipt"], store.paid_receipt_ids(exclude_claim=v["claimId"]))
    return {"receiptValid": reason is None, "receiptReason": reason}


def check_distance(v: dict, store: Store, cep: CepSink) -> dict:
    claim = store.get("claims", v["claimId"])
    delay = store.get("delays", v["delayId"]) if v.get("delayId") else None
    reason = rules.check_distance(claim, delay)
    return {"withinRadius": reason is None, "distanceReason": reason}


def calc_payout(v: dict, store: Store, cep: CepSink) -> dict:
    claim = store.get("claims", v["claimId"])
    return {"payoutDkk": rules.payout(claim["receipt"]["amountDkk"])}


def pay_out(v: dict, store: Store, cep: CepSink) -> dict:
    claim = store.get("claims", v["claimId"])
    if not store.mark_paid(claim["receipt"]["receiptId"], v["claimId"]):
        # two claims with the same receipt passed the check concurrently:
        # stop here and leave an incident for a case worker
        raise RuntimeError("receipt was paid out on another claim in the meantime")
    return {"payoutReference": f"PAY-{v['claimId']}"}


def reject(v: dict, store: Store, cep: CepSink) -> dict:
    if not v.get("delayVerified"):
        reason = v.get("exclusionReason") or "no verified delay"
    elif v.get("noticeExcluded") or v.get("forceMajeure"):
        reason = v.get("exclusionReason")
    elif v.get("receiptValid") is False:
        reason = "receipt: " + str(v.get("receiptReason"))
    elif v.get("withinRadius") is False:
        reason = "distance: " + str(v.get("distanceReason"))
    else:
        reason = "rejected"
    return {"rejectionReason": reason}


def report_outcome(v: dict, store: Store, cep: CepSink) -> dict:
    paid = v.get("payoutReference") is not None
    outcome = {
        "claimId": v["claimId"],
        "delayId": v.get("delayId"),
        "outcome": "paid" if paid else "rejected",
        "payoutDkk": v.get("payoutDkk") if paid else 0.0,
        "reason": None if paid else v.get("rejectionReason"),
    }
    store.put("outcomes", v["claimId"], outcome)
    cep.emit("ClaimPaid" if paid else "ClaimRejected", outcome)
    return {"outcome": outcome["outcome"]}


HANDLERS: Dict[str, Handler] = {
    "rg-check-notice": check_notice,
    "rg-check-force-majeure": check_force_majeure,
    "rg-report-assessment": report_assessment,
    "rg-match-delay": match_delay,
    "rg-check-receipt": check_receipt,
    "rg-check-distance": check_distance,
    "rg-calc-payout": calc_payout,
    "rg-pay-out": pay_out,
    "rg-reject": reject,
    "rg-report-outcome": report_outcome,
}


def run_workers(camunda: Camunda, store: Store, cep: CepSink, stop=None) -> None:
    worker_id = f"rejsegaranti-{socket.gethostname()}"
    topics = list(HANDLERS)
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
                camunda.failure(t["id"], worker_id, str(e), max(retries - 1, 0), 2000)
            except Exception as e:  # incident after the last retry
                log.exception("task %s failed", t["topicName"])
                camunda.failure(t["id"], worker_id, f"{type(e).__name__}: {e}", 0, 0)
