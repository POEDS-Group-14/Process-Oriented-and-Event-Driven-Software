"""Start variables of the TO-BE processes, worked out in one place for the bridge (live intake) and for
`rejsegaranti replay`, so a replayed claim starts exactly as it would have live.

The business rule tasks read these variables directly, so each one the tables use must exist (None is
fine: the tables have rules for a missing value).
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from .timeutil import local_date, parse_ts


def delay_start_variables(delay: dict, reprocessed: bool = False) -> dict:
    """Variables for tobe-assess-disruption (message DelayReported)."""
    return {
        "delayId": delay["delayId"],
        "line": delay["line"],
        "delayMinutes": float(delay["delayMinutes"]),
        "reprocessed": reprocessed,
    }


def fare(claim: dict) -> Optional[float]:
    """The receipt's amount in DKK, or None when the claim has no receipt (or no amount on it)."""
    amount = (claim.get("receipt") or {}).get("amountDkk")
    return None if amount in (None, "") else float(amount)


def receipt_id(claim: dict) -> Optional[str]:
    return (claim.get("receipt") or {}).get("receiptId") or None


def replay_order(claim: dict) -> tuple:
    """The order `replay` submits claims in: by trip start, then claim id."""
    return parse_ts(claim["tripStart"]), claim["claimId"]


def claim_start_variables(claim: dict, earlier_claims: Iterable[dict], received_at, reprocessed: bool = False) -> dict:
    """Variables for tobe-decide-claim (message ClaimSubmitted).

    earlier_claims are the claims submitted before this one: receiptUsedBefore is true when one of them has
    the same receipt. received_at is when the claim reached Movia (a datetime or an ISO timestamp);
    daysToClaim counts Copenhagen calendar days from the trip to then.
    """
    got = received_at if isinstance(received_at, datetime) else parse_ts(received_at)
    rid = receipt_id(claim)
    used = rid is not None and any(receipt_id(c) == rid for c in earlier_claims if c["claimId"] != claim["claimId"])
    amount = fare(claim)
    return {
        "claimId": claim["claimId"],
        "receiptAttached": amount is not None,
        "daysToClaim": (local_date(got) - local_date(parse_ts(claim["tripStart"]))).days,
        "receiptUsedBefore": used,
        "fareDkk": amount,
        "reprocessed": reprocessed,
    }
