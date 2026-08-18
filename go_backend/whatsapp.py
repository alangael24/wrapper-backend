"""Official WhatsApp Cloud API transport for Agent Genia.

This module intentionally contains no agent or account logic. It verifies and
parses Meta webhooks and sends replies; the backend remains the only authority
for users, bots, credits, connectors, and conversation state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx


class WhatsAppError(RuntimeError):
    """A transport failure with enough detail to make a safe retry decision.

    Meta does not accept a caller-provided idempotency key for message sends.
    A timeout after bytes were written is therefore materially different from
    an explicit 429/5xx response: the former may already have delivered the
    message and must never be retried automatically.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        delivery_uncertain: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.delivery_uncertain = bool(delivery_uncertain)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class WhatsAppConfig:
    enabled: bool
    verify_token: str
    app_secret: str
    access_token: str
    phone_number_id: str
    public_number: str
    graph_version: str
    link_ttl_seconds: int = 600

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.verify_token
            and self.app_secret
            and self.access_token
            and self.phone_number_id
            and self.public_number
            and re.fullmatch(r"v\d+\.\d+", self.graph_version)
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        missing = [
            name
            for name, value in (
                ("WHATSAPP_VERIFY_TOKEN", self.verify_token),
                ("WHATSAPP_APP_SECRET", self.app_secret),
                ("WHATSAPP_ACCESS_TOKEN", self.access_token),
                ("WHATSAPP_PHONE_NUMBER_ID", self.phone_number_id),
                ("WHATSAPP_PUBLIC_NUMBER", self.public_number),
                ("WHATSAPP_GRAPH_VERSION", self.graph_version),
            )
            if not value
        ]
        if missing:
            raise ValueError("Faltan variables de WhatsApp: " + ", ".join(missing))
        if not re.fullmatch(r"v\d+\.\d+", self.graph_version):
            raise ValueError("WHATSAPP_GRAPH_VERSION debe verse como v25.0")
        if not self.public_number.isdigit() or not 7 <= len(self.public_number) <= 15:
            raise ValueError("WHATSAPP_PUBLIC_NUMBER debe usar E.164 sin el signo +")
        if not self.phone_number_id.isdigit():
            raise ValueError("WHATSAPP_PHONE_NUMBER_ID no es válido")
        if len(self.verify_token) < 24 or len(self.app_secret) < 24 or len(self.access_token) < 24:
            raise ValueError("Los secretos de WhatsApp son demasiado cortos")
        if not 120 <= self.link_ttl_seconds <= 3600:
            raise ValueError("WHATSAPP_LINK_TTL_SECONDS debe estar entre 120 y 3600")


def verify_webhook_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    if not signature_header.startswith("sha256=") or not app_secret:
        return False
    supplied = signature_header[7:].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def parse_webhook_messages(payload: Any) -> list[dict[str, Any]]:
    """Extract inbound messages from a Cloud API webhook without trusting shape."""
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return []
    extracted: list[dict[str, Any]] = []
    entries = payload.get("entry")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        for change in changes if isinstance(changes, list) else []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            phone_number_id = (
                metadata.get("phone_number_id", "") if isinstance(metadata, dict) else ""
            )
            if not isinstance(phone_number_id, str) or not phone_number_id:
                continue
            contact_names: dict[str, str] = {}
            contacts = value.get("contacts")
            for contact in contacts if isinstance(contacts, list) else []:
                if not isinstance(contact, dict):
                    continue
                identities = (contact.get("wa_id"), contact.get("user_id"))
                profile = contact.get("profile")
                name = profile.get("name", "") if isinstance(profile, dict) else ""
                if isinstance(name, str):
                    for identity in identities:
                        if isinstance(identity, str) and identity:
                            contact_names[identity] = name
            messages = value.get("messages")
            for message in messages if isinstance(messages, list) else []:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                # Meta can identify a contact by the traditional phone-backed
                # ``from`` value or by its business-scoped user id. Preserve
                # whichever opaque identity the webhook supplies and echo it
                # back unchanged when replying.
                sender = message.get("from") or message.get("user_id")
                message_type = message.get("type")
                if not all(isinstance(item, str) and item for item in (message_id, sender, message_type)):
                    continue
                text = _message_text(message, message_type)
                extracted.append(
                    {
                        "message_id": message_id[:300],
                        "phone_number_id": phone_number_id[:100],
                        "wa_user_id": sender[:100],
                        "display_name": contact_names.get(sender, "")[:120],
                        "message_type": message_type[:40],
                        "text": text[:20_000],
                        "payload": message,
                    }
                )
    return extracted


def _message_text(message: dict[str, Any], message_type: str) -> str:
    if message_type == "text":
        value = message.get("text")
        return value.get("body", "") if isinstance(value, dict) else ""
    if message_type == "button":
        value = message.get("button")
        return value.get("text", "") if isinstance(value, dict) else ""
    if message_type == "interactive":
        value = message.get("interactive")
        if not isinstance(value, dict):
            return ""
        for key in ("button_reply", "list_reply"):
            reply = value.get(key)
            if isinstance(reply, dict):
                title = reply.get("title")
                identifier = reply.get("id")
                if isinstance(title, str) and title:
                    return title
                if isinstance(identifier, str):
                    return identifier
    return ""


class WhatsAppCloudAPI:
    def __init__(
        self,
        config: WhatsAppConfig,
        *,
        timeout_seconds: int = 15,
        api_base_url: str = "https://graph.facebook.com",
        client: httpx.Client | None = None,
    ):
        config.validate()
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.api_base_url = api_base_url.rstrip("/")
        # Reuse TLS connections across replies. Establishing a new connection
        # for every message added avoidable latency, especially when an answer
        # is split into several Cloud API requests.
        self._client = client or httpx.Client(
            http2=True,
            timeout=httpx.Timeout(float(timeout_seconds), connect=min(5.0, float(timeout_seconds))),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "AgentGenia-WhatsApp/1.0"},
        )

    def link_url(self, code: str) -> str:
        query = urllib.parse.urlencode({"text": f"Vincular Agentgenia {code}"})
        return f"https://wa.me/{self.config.public_number}?{query}"

    def send_text(
        self, *, to: str, text: str, reply_to_message_id: str | None = None
    ) -> str | None:
        if not self.config.configured:
            raise WhatsAppError("WhatsApp no está configurado")
        clean = text.strip()
        if not clean:
            return None
        last_message_id: str | None = None
        chunks = _split_text(clean, 3500)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            }
            if index == 0 and reply_to_message_id:
                payload["context"] = {"message_id": reply_to_message_id}
            try:
                response = self._request(payload)
            except WhatsAppError as exc:
                if index:
                    # At least one earlier chunk was accepted. Retrying the
                    # complete answer would duplicate it, regardless of how
                    # definitive the later provider error looks.
                    raise WhatsAppError(
                        str(exc), delivery_uncertain=True
                    ) from exc
                raise
            messages = response.get("messages") if isinstance(response, dict) else None
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                value = messages[0].get("id")
                if isinstance(value, str):
                    last_message_id = value
            if not last_message_id:
                # A 2xx without Meta's accepted message id cannot be proven
                # safe to retry; fail closed to avoid duplicate replies.
                raise WhatsAppError(
                    "Meta aceptó una respuesta sin identificador de mensaje",
                    delivery_uncertain=True,
                )
        return last_message_id

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"{self.api_base_url}/{self.config.graph_version}/"
            f"{self.config.phone_number_id}/messages"
        )
        try:
            response = self._client.post(
                url,
                content=json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.config.access_token}",
                    "Content-Type": "application/json",
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # No connection was established, so Meta could not have accepted
            # the payload. This is the only network error that is safe to retry.
            raise WhatsAppError(
                "No fue posible conectar con WhatsApp Cloud API",
                retryable=True,
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The request may have reached Meta before the socket failed.
            raise WhatsAppError(
                "Se perdió la respuesta de WhatsApp Cloud API",
                delivery_uncertain=True,
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            try:
                parsed_error = response.json()
                detail = parsed_error.get("error", {}) if isinstance(parsed_error, dict) else {}
                code = detail.get("code") if isinstance(detail, dict) else None
            except Exception:
                code = None
            retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            raise WhatsAppError(
                f"WhatsApp Cloud API rechazó el mensaje "
                f"(HTTP {response.status_code}, código {code or 'unknown'})",
                retryable=retryable,
                retry_after_seconds=retry_after,
            )
        try:
            parsed = response.json() if response.content else {}
        except json.JSONDecodeError as exc:
            raise WhatsAppError(
                "Meta devolvió una respuesta inválida",
                delivery_uncertain=True,
            ) from exc
        if not isinstance(parsed, dict):
            raise WhatsAppError(
                "Meta devolvió una respuesta inválida",
                delivery_uncertain=True,
            )
        return parsed


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        return None
    return max(0.0, min(parsed, 300.0))


def _split_text(value: str, maximum: int) -> list[str]:
    chunks: list[str] = []
    remaining = value
    while len(remaining) > maximum:
        boundary = remaining.rfind("\n", 0, maximum + 1)
        if boundary < maximum // 2:
            boundary = remaining.rfind(" ", 0, maximum + 1)
        if boundary < maximum // 2:
            boundary = maximum
        chunks.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks
