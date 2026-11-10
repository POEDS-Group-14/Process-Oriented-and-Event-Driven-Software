"""HTTP bridge: CEP situations and claims in, Camunda instances out.

Endpoints (JSON bodies, see docs/cep-interface.md):

  POST /cep/delay     delay situation         -> starts rejsegaranti-delay-case
  POST /cep/weather   DMI warning / OWM observation situation (stored)
  POST /cep/notice    planned service change notice from process 6 (stored)
  POST /claims        passenger claim         -> starts rejsegaranti-claim
  GET  /state         registry contents (debugging)
  GET  /results       read-only HTML summary of the outcomes (shared publicly, see deploy/README.md)
  GET  /health
"""
from __future__ import annotations

import json
import logging
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from . import results, rules, stations
from .camunda import Camunda
from .store import Store

log = logging.getLogger("rejsegaranti.bridge")

DELAY_MESSAGE = "DelaySituationDetected"
CLAIM_MESSAGE = "ClaimSubmitted"

CLAIM_FIELDS = ("tripStart", "startLat", "startLon", "endLat", "endLon", "receipt")


class Bridge:
    def __init__(self, store: Store, camunda: Optional[Camunda]):
        self.store = store
        self.camunda = camunda

    # Each handler returns (http status, response body)
    def delay(self, s: dict) -> Tuple[int, dict]:
        s = dict(s)
        if len(stations.LINES) == 1:  # one line: the CEP may leave it out
            s.setdefault("line", next(iter(stations.LINES)))
        s.setdefault("delayId", f"D-{uuid.uuid4().hex[:10]}")
        if isinstance(s.get("stops"), str):
            s["stops"] = [x.strip() for x in s["stops"].split(",") if x.strip()]
        for f in ("line", "delayMinutes", "detectedAt"):
            if f not in s:
                return 400, {"error": f"missing {f}"}
        if not rules.qualifies_as_delay(s):
            return 202, {"ignored": "not a delay of 30+ minutes on one of the process's lines", "delayId": s["delayId"]}
        new = self.store.put("delays", s["delayId"], s)
        if not new:  # the CEP re-emits while the delay lasts; one case per delay
            return 200, {"delayId": s["delayId"], "started": False}
        if self.camunda:
            self.camunda.correlate(DELAY_MESSAGE, business_key=s["delayId"], variables={
                "delayId": s["delayId"], "line": s["line"],
                "stops": ", ".join(s.get("stops", [])),
                "delayMinutes": float(s["delayMinutes"]),
                "detectedAt": s["detectedAt"],
                "plannedDeparture": s.get("plannedDeparture"),
            })
        return 201, {"delayId": s["delayId"], "started": True}

    def weather(self, w: dict) -> Tuple[int, dict]:
        w = dict(w)
        w.setdefault("situationId", f"W-{uuid.uuid4().hex[:10]}")
        for f in ("source", "validFrom", "validTo"):
            if f not in w:
                return 400, {"error": f"missing {f}"}
        self.store.put("weather", w["situationId"], w)
        return 201, {"situationId": w["situationId"]}

    def notice(self, n: dict) -> Tuple[int, dict]:
        n = dict(n)
        for f in ("noticeId", "effectiveFrom", "effectiveTo", "firstSeen"):
            if f not in n:
                return 400, {"error": f"missing {f}"}
        # keep the earliest first-seen time if the recorder sends it again
        old = self.store.get("notices", n["noticeId"])
        if old:
            n["firstSeen"] = min(old["firstSeen"], n["firstSeen"])
        self.store.put("notices", n["noticeId"], n)
        return 201, {"noticeId": n["noticeId"]}

    def claim(self, c: dict) -> Tuple[int, dict]:
        """Claim intake. Incomplete receipts are refused here, at the form
        (TO-BE change: contact reduction), instead of a later back-and-forth."""
        c = dict(c)
        c.setdefault("claimId", f"C-{uuid.uuid4().hex[:10]}")
        missing = [f for f in CLAIM_FIELDS if f not in c]
        missing += ["receipt." + f for f in rules.missing_receipt_fields(c.get("receipt") or {})]
        if missing:
            return 422, {"error": "incomplete claim", "missing": missing}
        if not self.store.put("claims", c["claimId"], c):
            return 409, {"error": "claim already submitted", "claimId": c["claimId"]}
        if self.camunda:
            r = c["receipt"]
            self.camunda.correlate(CLAIM_MESSAGE, business_key=c["claimId"], variables={
                "claimId": c["claimId"], "tripStart": c["tripStart"],
                "receiptId": r["receiptId"], "transportType": r["transportType"],
                "amountDkk": float(r["amountDkk"]),
            })
        return 201, {"claimId": c["claimId"]}

    def state(self) -> dict:
        return {k: self.store.all(k) for k in Store.KINDS}


def make_server(bridge: Bridge, host: str = "0.0.0.0", port: int = 8090) -> ThreadingHTTPServer:
    routes = {
        "/cep/delay": bridge.delay,
        "/cep/weather": bridge.weather,
        "/cep/notice": bridge.notice,
        "/claims": bridge.claim,
    }

    class H(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/state":
                return self._send(200, bridge.state())
            if self.path.split("?")[0].rstrip("/") == "/results":
                data = results.render(bridge.store).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            fn = routes.get(self.path.rstrip("/"))
            if not fn:
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                # Siddhi's HTTP sink with @map(type='json') wraps payloads in {"event": ...}
                if isinstance(body, dict) and set(body) == {"event"}:
                    body = body["event"]
                status, resp = fn(body)
            except Exception as e:
                log.exception("request to %s failed", self.path)
                status, resp = 500, {"error": str(e)}
            self._send(status, resp)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return ThreadingHTTPServer((host, port), H)
