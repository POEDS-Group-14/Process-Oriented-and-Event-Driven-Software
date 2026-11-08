"""Receive the Dataudveksleren real-time feed from Azure Service Bus (AMQP 1.0).

Each subscribed dataset is a topic subscription on Dataudveksleren's Service Bus namespace. The
portal's "Subscribe to events" popup gives its AMQP url (amqps://<namespace>/<topic>/<subscription>)
and the tenant ID; the service account's client ID and password sign in to Microsoft Entra ID
(client credentials), as in Dataudveksleren's "AMQP - Receive messages" documentation.

Needs: apt install python3-azure (Debian), or the package's "siri" extra
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("recorder.servicebus")


def _client(cfg, namespace: str):
    from azure.identity import ClientSecretCredential  # imported here so the rest runs without it
    from azure.servicebus import ServiceBusClient
    cred = ClientSecretCredential(cfg.azure_tenant_id, cfg.azure_client_id, cfg.azure_client_secret)
    return ServiceBusClient(namespace, credential=cred)


def _body_text(msg) -> str:
    body = msg.body
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif isinstance(body, str):
        return body
    else:  # generator of byte chunks (AMQP data sections)
        data = b"".join(bytes(b) for b in body)
    return data.decode("utf-8", "replace")


def receive_forever(cfg, namespace: str, topic: str, subscription: str, handle, stop) -> None:
    """Call handle(text, meta) for every message on one topic subscription until stop.is_set().
    Reconnects on errors."""
    backoff = 5
    while not stop.is_set():
        try:
            with _client(cfg, namespace) as client:
                with client.get_subscription_receiver(topic, subscription) as receiver:
                    log.info("%s: connected to Service Bus", topic)
                    backoff = 5
                    while not stop.is_set():
                        for msg in receiver.receive_messages(max_message_count=50, max_wait_time=30):
                            meta = {"topic": topic, "messageId": str(msg.message_id or ""),
                                    "enqueuedTime": msg.enqueued_time_utc.isoformat() if msg.enqueued_time_utc else "",
                                    "subject": str(msg.subject or ""),
                                    "contentType": str(msg.content_type or "")}
                            handle(_body_text(msg), meta)
                            receiver.complete_message(msg)  # only after it has been stored
        except Exception as e:  # network drops, token expiry, throttling
            log.warning("%s: Service Bus error: %s; retrying in %ss", topic, e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)
