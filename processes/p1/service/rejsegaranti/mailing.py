"""Mailing list: people sign up on the results page and get one email per decided claim.

The page is public, so sign-up is double opt-in: an address only joins the list once its owner clicks the
confirmation link sent to it. Every email carries an unsubscribe link. The confirm and unsubscribe links open a
page with a button that POSTs, so mail scanners that open links do not confirm or unsubscribe anyone.
Confirmation emails are rate-limited (one per address per hour, CONFIRMATIONS_PER_HOUR in all), so the form
cannot be used to flood someone's inbox. The list is kept in <DATA_DIR>/subscribers.json and is never shown.

Mail goes out over SMTP with STARTTLS, set in the environment (on the server: /etc/rejsegaranti/mail.env):
SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, MAIL_FROM (default SMTP_USER) and PUBLIC_URL, the
results page's public address used in the links. Without SMTP_HOST the list is off and the page has no form.
Sending runs on its own thread, so a slow or failing mail server never holds up the claim workers.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import smtplib
import threading
import time
from email.message import EmailMessage
from typing import Dict, List, Optional

log = logging.getLogger("rejsegaranti.mailing")

EMAIL = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}")
CONFIRMATIONS_PER_HOUR = 20


class MailingList:
    """email -> {"token", "confirmed", "requestedAt"}; thread-safe, persisted to one JSON file."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._lock = threading.Lock()
        self.people: Dict[str, dict] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.people = json.load(fh)
        self._sent: List[float] = []  # times of recent confirmation emails

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        # email addresses: readable by the service user only
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            json.dump(self.people, fh, indent=1)
        os.chmod(tmp, 0o600)  # also when the .tmp file was left over from before
        os.replace(tmp, self.path)

    def request(self, email: str, t: Optional[float] = None) -> Optional[str]:
        """Start (or repeat) a sign-up. Returns the token to email, or None when nothing should be sent:
        already confirmed, asked within the hour, or too many confirmations sent this hour."""
        t = time.time() if t is None else t
        email = email.strip().lower()
        with self._lock:
            p = self.people.get(email)
            if p and (p["confirmed"] or t - p["requestedAt"] < 3600):
                return None
            self._sent = [s for s in self._sent if t - s < 3600]
            if len(self._sent) >= CONFIRMATIONS_PER_HOUR:
                return None
            self._sent.append(t)
            token = p["token"] if p else secrets.token_urlsafe(24)
            self.people[email] = {"token": token, "confirmed": False, "requestedAt": t}
            self._save()
            return token

    def _by_token(self, token: str) -> Optional[str]:
        return next((e for e, p in self.people.items() if secrets.compare_digest(p["token"], token)), None)

    def confirm(self, token: str) -> bool:
        with self._lock:
            email = self._by_token(token)
            if email:
                self.people[email]["confirmed"] = True
                self._save()
            return email is not None

    def unsubscribe(self, token: str) -> bool:
        with self._lock:
            email = self._by_token(token)
            if email:
                del self.people[email]
                self._save()
            return email is not None

    def confirmed(self) -> Dict[str, str]:
        """email -> token of everyone on the list."""
        with self._lock:
            return {e: p["token"] for e, p in self.people.items() if p["confirmed"]}


def valid_email(email: str) -> bool:
    return len(email) <= 254 and EMAIL.fullmatch(email.strip()) is not None


class Notifier:
    """Sends the mail. `on_event` is a CepSink listener: each ClaimPaid / ClaimRejected event becomes one email
    to everyone on the list."""

    def __init__(self, people: MailingList, store, env=os.environ, send=None):
        self.people, self.store = people, store
        self.host = env.get("SMTP_HOST", "")
        self.port = int(env.get("SMTP_PORT", "587"))
        self.user, self.password = env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", "")
        self.sender = env.get("MAIL_FROM") or self.user
        self.public_url = env.get("PUBLIC_URL", "http://localhost:8090/results").rstrip("/")
        self._send = send or self._smtp
        self._queue: "queue.Queue[EmailMessage]" = queue.Queue()
        if self.enabled:
            threading.Thread(target=self._run, daemon=True, name="mailer").start()

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def _message(self, to: str, subject: str, body: str, token: Optional[str] = None) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.sender, to, subject
        if token:
            link = f"{self.public_url}/unsubscribe?token={token}"
            body += f"\n\nUnsubscribe: {link}\n"
            m["List-Unsubscribe"] = f"<{link}>"
            m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        m.set_content(body)
        return m

    def send_confirmation(self, email: str, token: str) -> None:
        self._queue.put(self._message(email, "Confirm your Rejsegaranti claim emails", (
            "Someone (hopefully you) asked to get an email for every claim decided by the Rejsegaranti process "
            "of the DTU Process-Oriented and Event-Driven Software project.\n\n"
            f"To confirm, open this link and press the button:\n{self.public_url}/confirm?token={token}\n\n"
            "If it wasn't you, ignore this email and you won't hear from us again.")))

    def on_event(self, event: dict) -> None:
        if event.get("eventType") not in ("ClaimPaid", "ClaimRejected"):
            return
        people = self.people.confirmed()
        if not people:
            return
        claim = self.store.get("claims", event["claimId"]) or {}
        delay = self.store.get("delays", event.get("delayId") or "") or {}
        receipt = claim.get("receipt") or {}
        paid = event["eventType"] == "ClaimPaid"
        subject = f"Claim {event['claimId']} {'paid' if paid else 'rejected'}"
        if delay.get("line"):
            subject += f" (line {delay['line']})"
        lines = [
            f"Claim:      {event['claimId']}",
            f"Outcome:    {'paid ' + format(float(event.get('payoutDkk') or 0), '.2f') + ' DKK' if paid else 'rejected'}",
        ]
        if not paid:
            lines.append(f"Reason:     {event.get('reason') or '-'}")
        lines += [
            f"Trip start: {claim.get('tripStart', '-')}",
            f"Transport:  {receipt.get('transportType', '-')}, receipt {receipt.get('amountDkk', '-')} DKK",
            f"Delay:      {event.get('delayId') or 'none matched'}"
            + (f", {delay.get('delayMinutes'):.0f} min late" if delay.get("delayMinutes") else ""),
            "",
            "The delay is real (Movia / DSB real-time data); the claim is generated by the project's simulator.",
            f"All results: {self.public_url}/",
        ]
        for email, token in people.items():
            self._queue.put(self._message(email, subject, "\n".join(lines), token))

    def _smtp(self, messages: List[EmailMessage]) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password)
            for m in messages:
                s.send_message(m)

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            while not self._queue.empty():  # one connection for everything queued
                batch.append(self._queue.get_nowait())
            try:
                self._send(batch)
                log.info("sent %d email(s)", len(batch))
            except Exception as e:  # the mail server being down must not stop the bridge
                log.warning("could not send %d email(s): %s", len(batch), e)
                time.sleep(60)
