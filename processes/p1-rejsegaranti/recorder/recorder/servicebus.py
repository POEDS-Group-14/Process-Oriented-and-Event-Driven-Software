"""Receive the Dataudveksleren real-time feed from Azure Service Bus (AMQP 1.0).

What is known (from a third-party write-up, not from Dataudveksleren's own documentation,
which needs a login): SIRI-ET is pushed, not polled, as XML over AMQP 1.0 from Azure Service
Bus to a subscribed service account, authenticated with a Microsoft Entra ID token; Movia's
SIRI-ET dataset is listed as #788. Check the namespace, queue/topic names and authentication
method in your Dataudveksleren subscription and put them in the env file.

Needs: pip install azure-servicebus azure-identity
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("recorder.servicebus")


def _client(cfg):
    from azure.servicebus import ServiceBusClient  # imported here so the rest runs without it
    if cfg.sb_connection_string:
        return ServiceBusClient.from_connection_string(cfg.sb_connection_string)
    from azure.identity import ClientSecretCredential
    cred = ClientSecretCredential(cfg.azure_tenant_id, cfg.azure_client_id, cfg.azure_client_secret)
    return ServiceBusClient(cfg.sb_namespace, credential=cred)


def _body_text(msg) -> str:
    body = msg.body
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif isinstance(body, str):
        return body
    else:  # generator of byte chunks (AMQP data sections)
        data = b"".join(bytes(b) for b in body)
    return data.decode("utf-8", "replace")


def receive_forever(cfg, handle, stop) -> None:
    """Call handle(text, meta) for every message until stop.is_set(). Reconnects on errors."""
    backoff = 5
    while not stop.is_set():
        try:
            with _client(cfg) as client:
                receiver = (client.get_queue_receiver(cfg.sb_queue) if cfg.sb_queue
                            else client.get_subscription_receiver(cfg.sb_topic, cfg.sb_subscription))
                with receiver:
                    log.info("connected to Service Bus")
                    backoff = 5
                    while not stop.is_set():
                        for msg in receiver.receive_messages(max_message_count=50, max_wait_time=30):
                            meta = {"messageId": str(msg.message_id or ""),
                                    "enqueuedTime": msg.enqueued_time_utc.isoformat() if msg.enqueued_time_utc else "",
                                    "subject": str(msg.subject or ""),
                                    "contentType": str(msg.content_type or "")}
                            handle(_body_text(msg), meta)
                            receiver.complete_message(msg)  # only after it has been stored
        except Exception as e:  # network drops, token expiry, throttling
            log.warning("Service Bus error: %s; retrying in %ss", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)
