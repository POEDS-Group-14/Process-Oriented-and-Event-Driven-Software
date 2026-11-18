"""Situation registry shared by the CEP bridge and the workers.

Holds what the CEP has told us (delays, weather, notices), the claims and
appeals that came in, and the assessments and outcomes. Persisted as one JSON
file so a restart does not lose state. Thread-safe.

The file is read only when the Store is created, and every change rewrites it
whole. So exactly one process may own it: `rejsegaranti run` (bridge and
workers share one Store). Other commands go through the bridge over HTTP
(`rejsegaranti replay` uses its /replay endpoints) instead of writing the file.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from typing import Any, Dict, List, Optional


class Store:
    KINDS = ("delays", "weather", "notices", "claims", "assessments", "outcomes", "appeals")
    # what the processes decided, as opposed to what came in: `replay` clears these and decides again
    DECIDED = ("assessments", "outcomes", "appeals")

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._lock = threading.RLock()
        self.data: Dict[str, Dict[str, Any]] = {k: {} for k in self.KINDS}
        self.paid_receipts: Dict[str, str] = {}  # receiptId -> claimId
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for k in self.KINDS:
                self.data[k] = raw.get(k, {})
            self.paid_receipts = raw.get("paid_receipts", {})

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({**self.data, "paid_receipts": self.paid_receipts}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def put(self, kind: str, key: str, value: dict) -> bool:
        """Store a record; returns True if it was new."""
        with self._lock:
            new = key not in self.data[kind]
            self.data[kind][key] = {**self.data[kind].get(key, {}), **value}
            self._save()
            return new

    def get(self, kind: str, key: str) -> Optional[dict]:
        with self._lock:
            v = self.data[kind].get(key)
            return dict(v) if v else None

    def all(self, kind: str) -> List[dict]:
        with self._lock:
            return [dict(v) for v in self.data[kind].values()]

    def mark_paid(self, receipt_id: str, claim_id: str) -> bool:
        """Atomically record a payout. False if the receipt was already paid."""
        with self._lock:
            owner = self.paid_receipts.get(receipt_id)
            if owner and owner != claim_id:
                return False
            self.paid_receipts[receipt_id] = claim_id
            self._save()
            return True

    def paid_receipt_ids(self, exclude_claim: Optional[str] = None) -> List[str]:
        with self._lock:
            return [r for r, c in self.paid_receipts.items() if c != exclude_claim]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {**{k: len(v) for k, v in self.data.items()}, "paid_receipts": len(self.paid_receipts)}

    def reset_decided(self, backup: Optional[str] = None) -> Dict[str, int]:
        """Copy the file to `backup` (if given), then forget everything the processes decided (DECIDED and
        the paid receipts), keeping what came in. Returns the counts that were cleared."""
        with self._lock:
            if backup and self.path and os.path.exists(self.path):
                shutil.copyfile(self.path, backup)
            cleared = {k: len(self.data[k]) for k in self.DECIDED}
            cleared["paid_receipts"] = len(self.paid_receipts)
            for k in self.DECIDED:
                self.data[k] = {}
            self.paid_receipts = {}
            self._save()
            return cleared
