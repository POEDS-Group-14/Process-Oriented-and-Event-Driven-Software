"""HTTP bridge: CEP situations and claims in, Camunda instances out.

Endpoints (JSON bodies, see docs/cep-interface.md):

  POST /cep/delay     delay situation         -> starts rejsegaranti-delay-case
  POST /cep/weather   DMI warning / OWM observation situation (stored)
  POST /cep/notice    planned service change notice from process 6 (stored)
  POST /claims        passenger claim         -> starts rejsegaranti-claim
  GET  /state         registry contents (debugging)
  GET  /results       read-only HTML page: the claims judged by the AS-IS rules, the travel guarantee's DMN
                      tables on Camunda (shared publicly as "/", see deploy/README.md; "/" serves it locally too)
  GET  /results/to-be the outcomes the running TO-BE process decided (also "/to-be")
  GET  /results/<f>   its data as downloads: claims.csv, per-line.csv, claims.xes, delay-cases.xes
  POST /results/subscribe              join the claim mailing list (form field "email"; double opt-in)
  GET|POST /results/confirm?token=     confirm it (GET shows a button, the POST confirms)
  GET|POST /results/unsubscribe?token= leave it (likewise)
  GET  /health
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from . import results, rules, stations
from .camunda import Camunda
from .mailing import Notifier, valid_email
from .store import Store

log = logging.getLogger("rejsegaranti.bridge")

DELAY_MESSAGE = "DelaySituationDetected"
CLAIM_MESSAGE = "ClaimSubmitted"

CLAIM_FIELDS = ("tripStart", "startLat", "startLon", "endLat", "endLon", "receipt")


class Bridge:
    def __init__(self, store: Store, camunda: Optional[Camunda], notifier: Optional[Notifier] = None):
        self.store = store
        self.camunda = camunda
        self.notifier = notifier  # None: no mailing list

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

    def mailing(self, action: str, method: str, fields: dict) -> Tuple[int, str]:
        """The mailing-list pages: (http status, HTML)."""
        people = self.notifier.people
        token = fields.get("token", "")
        if action == "subscribe":
            email = fields.get("email", "").strip()
            if method != "POST" or not valid_email(email):
                return 400, results.message("Not an email address", "Go back and check the address.")
            t = people.request(email)
            if t:
                self.notifier.send_confirmation(email.lower(), t)
            # the same answer whatever happened, so the page does not reveal who is on the list
            return 200, results.message("Check your inbox", (
                "If that address is not on the list yet, a confirmation email is on its way. You'll get claim "
                "emails once you have clicked the link in it."))
        verb = {"confirm": "Confirm", "unsubscribe": "Unsubscribe"}[action]
        if method == "GET":
            return 200, results.message(f"{verb}?", "", form=(action, token, verb))
        done = people.confirm(token) if action == "confirm" else people.unsubscribe(token)
        if not done:
            return 404, results.message("Link not valid", "It has expired or was used to unsubscribe already.")
        if action == "confirm":
            return 200, results.message("You're on the list", "You'll get an email for every decided claim.")
        return 200, results.message("Unsubscribed", "You won't get any more claim emails.")

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
        def _mailing(self, method: str) -> bool:
            """Serve /[results/](subscribe|confirm|unsubscribe) if the mailing list is on; True if handled."""
            url = urllib.parse.urlsplit(self.path)
            action = url.path.rsplit("/", 1)[-1]
            if (not bridge.notifier or action not in ("subscribe", "confirm", "unsubscribe")
                    or url.path not in ("/" + action, "/results/" + action)):
                return False
            fields = dict(urllib.parse.parse_qsl(url.query))
            if method == "POST":
                n = min(int(self.headers.get("Content-Length", 0)), 4096)
                fields.update(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace")))
            status, html = bridge.mailing(action, method, fields)
            data = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self._mailing("GET"):
                return
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/state":
                return self._send(200, bridge.state())
            page = self.path.split("?")[0].rstrip("/")
            if page in ("", "/results", "/to-be", "/results/to-be"):
                if page.endswith("/to-be"):
                    html = results.render(bridge.store, signup=False)  # sign-up form hidden; the list itself still works
                else:
                    html = results.render_as_is(bridge.store, bridge.camunda)
                data = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            # downloads: /results/<file> publicly (Funnel maps "/" to /results), /<file> locally next to /results
            name = self.path.split("?")[0].rsplit("/", 1)[-1]
            if name in results.DOWNLOADS and self.path.split("?")[0] in ("/" + name, "/results/" + name):
                try:
                    file = results.download(name, bridge.store, bridge.camunda)
                except Exception as e:  # Camunda down or restarting
                    log.warning("download %s failed: %s", name, e)
                    return self._send(503, {"error": f"{name} is not available right now"})
                if file:
                    data, media = file
                    self.send_response(200)
                    self.send_header("Content-Type", media)
                    self.send_header("Content-Disposition", f'attachment; filename="rejsegaranti-{name}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self._mailing("POST"):
                return
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
