"""HTTP bridge: CEP situations, claims and appeals in, Camunda instances out.

Endpoints (JSON bodies, see docs/cep-interface.md):

  POST /cep/delay     delay situation         -> starts tobe-assess-disruption (message DelayReported)
  POST /cep/weather   DMI warning / OWM observation situation (stored)
  POST /cep/notice    planned service change notice from process 6 (stored)
  POST /claims        passenger claim         -> starts tobe-decide-claim (message ClaimSubmitted)
  POST /appeals       {claimId, reason}       -> starts tobe-handle-appeal (message AppealSubmitted);
                      404 if the claim is unknown, 409 if it was appealed already
  POST /replay/reset  back up the registry and clear what the processes decided   (from this machine only)
  POST /replay/delay  {delayId}: assess a stored delay again (reprocessed)          (from this machine only)
  POST /replay/claim  {claimId, receivedAt}: decide a stored claim again             (from this machine only)
  GET  /state         registry contents (debugging)
  GET  /results       read-only HTML page: the claims judged by the AS-IS rules, the travel guarantee's DMN
                      tables on Camunda (shared publicly as "/", see deploy/README.md; "/" serves it locally too)
  GET  /results/to-be the outcomes the TO-BE processes decided, next to the AS-IS judgement (also "/to-be")
  GET  /results/<f>   its data as downloads: claims.csv, per-line.csv, claims.xes, delay-cases.xes
  POST /results/subscribe              join the claim mailing list (form field "email"; double opt-in)
  GET|POST /results/confirm?token=     confirm it (GET shows a button, the POST confirms)
  GET|POST /results/unsubscribe?token= leave it (likewise)
  GET  /health

The bridge's Store is the only writer of the registry file (see store.py), which is why the replay
command works through the /replay endpoints. They answer only requests from the loopback address, and
Tailscale Funnel only forwards paths under /results, so they are not reachable from outside.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from . import activities as A
from . import results, rules, stations
from .camunda import Camunda
from .intake import claim_start_variables, delay_start_variables, replay_order
from .mailing import Notifier, valid_email
from .store import Store
from .timeutil import iso, now, parse_ts

log = logging.getLogger("rejsegaranti.bridge")

# The receipt is optional: a claim without one is rejected by the ToBe_ClaimValid table ("No receipt")
CLAIM_FIELDS = ("tripStart", "startLat", "startLon", "endLat", "endLon")
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


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
            return 202, {"ignored": "not on one of the process's lines", "delayId": s["delayId"]}
        new = self.store.put("delays", s["delayId"], s)
        if not new:  # the CEP re-emits while the delay lasts; one case per delay
            return 200, {"delayId": s["delayId"], "started": False}
        self._start_delay(s)
        return 201, {"delayId": s["delayId"], "started": True}

    def _start_delay(self, d: dict, reprocessed: bool = False) -> None:
        if self.camunda:
            self.camunda.correlate(A.DELAY_MESSAGE, business_key=d["delayId"],
                                   variables=delay_start_variables(d, reprocessed))

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
        """Claim intake. Only the trip itself is required; everything the travel guarantee rules decide
        (receipt, deadline, duplicate receipt) is left to the tables in the process."""
        c = dict(c)
        c.setdefault("claimId", f"C-{uuid.uuid4().hex[:10]}")
        missing = [f for f in CLAIM_FIELDS if c.get(f) in (None, "")]
        if missing:
            return 422, {"error": "incomplete claim", "missing": missing}
        try:
            parse_ts(c["tripStart"])
            for f in CLAIM_FIELDS[1:]:
                float(c[f])
        except (TypeError, ValueError):
            return 422, {"error": "tripStart must be a timestamp and the coordinates numbers"}
        c["receivedAt"] = iso(now())
        earlier = self.store.all("claims")  # every claim already stored was submitted before this one
        if not self.store.put("claims", c["claimId"], c):
            return 409, {"error": "claim already submitted", "claimId": c["claimId"]}
        self._start_claim(c, earlier, c["receivedAt"])
        return 201, {"claimId": c["claimId"]}

    def _start_claim(self, c: dict, earlier: list, received_at, reprocessed: bool = False) -> None:
        if self.camunda:
            self.camunda.correlate(A.CLAIM_MESSAGE, business_key=c["claimId"],
                                   variables=claim_start_variables(c, earlier, received_at, reprocessed))

    def appeal(self, a: dict) -> Tuple[int, dict]:
        """An appeal against a decided claim: a case worker reviews it in Camunda Tasklist."""
        claim_id, reason = a.get("claimId"), a.get("reason")
        if not claim_id or not reason:
            return 422, {"error": "claimId and reason are required"}
        if self.store.get("claims", claim_id) is None:
            return 404, {"error": "unknown claim", "claimId": claim_id}
        if not self.store.put("appeals", claim_id, {"claimId": claim_id, "reason": str(reason),
                                                    "status": "open", "submittedAt": iso(now())}):
            return 409, {"error": "claim already appealed", "claimId": claim_id}
        if self.camunda:
            self.camunda.correlate(A.APPEAL_MESSAGE, business_key=claim_id,
                                   variables={"claimId": claim_id, "appealReason": str(reason)})
        return 201, {"claimId": claim_id, "appealed": True}

    # ---- replay (see __main__.cmd_replay) ---------------------------------------
    def replay(self, action: str, body: dict) -> Tuple[int, dict]:
        if action == "reset":
            backup = None
            if self.store.path:
                stamp = now().strftime("%Y%m%dT%H%M%S")
                backup = os.path.join(os.path.dirname(os.path.abspath(self.store.path)),
                                      f"registry.before-replay-{stamp}.json")
            return 200, {"backup": backup, "cleared": self.store.reset_decided(backup)}
        if action == "delay":
            d = self.store.get("delays", body.get("delayId") or "")
            if d is None:
                return 404, {"error": "unknown delay"}
            self._start_delay(d, reprocessed=True)
            return 201, {"delayId": d["delayId"]}
        if action == "claim":
            c = self.store.get("claims", body.get("claimId") or "")
            if c is None:
                return 404, {"error": "unknown claim"}
            key = replay_order(c)
            earlier = [x for x in self.store.all("claims") if replay_order(x) < key]
            self._start_claim(c, earlier, body.get("receivedAt") or c.get("receivedAt") or c["tripStart"],
                              reprocessed=True)
            return 201, {"claimId": c["claimId"]}
        return 404, {"error": "not found"}

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
        "/appeals": bridge.appeal,
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
                    html = results.render(bridge.store, signup=False, camunda=bridge.camunda)  # sign-up form hidden; the list itself still works
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
            path = self.path.rstrip("/")
            fn = routes.get(path)
            if path.startswith("/replay/"):
                if self.client_address[0] not in LOOPBACK:
                    return self._send(403, {"error": "replay is only allowed from this machine"})
                fn = lambda body: bridge.replay(path.rsplit("/", 1)[-1], body)  # noqa: E731
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
