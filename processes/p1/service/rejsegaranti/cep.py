"""Outbound events from the process engine back to the CEP (issue #38).

If CEP_URL is set (for example the Siddhi HTTP source
http://localhost:8006/rejsegaranti), each event is POSTed there as JSON.
Every event is also appended to <DATA_DIR>/cep-events.jsonl so runs can be
inspected without a CEP.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Optional

from .timeutil import iso, now

log = logging.getLogger("rejsegaranti.cep")


class CepSink:
    def __init__(self, url: Optional[str] = None, journal: Optional[str] = None):
        self.url = url if url is not None else os.environ.get("CEP_URL") or None
        self.journal = journal
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict) -> dict:
        event = {"eventType": event_type, "emittedAt": iso(now()), **payload}
        if self.journal:
            with self._lock:
                os.makedirs(os.path.dirname(os.path.abspath(self.journal)), exist_ok=True)
                with open(self.journal, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.url:
            # Siddhi's HTTP source with @map(type='json') expects {"event": {...}}
            body = json.dumps({"event": event}).encode()
            req = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:  # the CEP being down must not block claims
                log.warning("could not deliver %s to CEP at %s: %s", event_type, self.url, e)
        log.debug("-> CEP %s %s", event_type, payload.get("claimId") or payload.get("delayId"))
        return event
