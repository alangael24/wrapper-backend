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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class WhatsAppError(RuntimeError):
    pass


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
            raise ValueError("WHATSAPP_GRAPH_VERSION debe verse como v23.0")
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
    def __init__(self, config: WhatsAppConfig, *, timeout_seconds: int = 15):
        config.validate()
        self.config = config
        self.timeout_seconds = timeout_seconds

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
            response = self._request(payload)
            messages = response.get("messages") if isinstance(response, dict) else None
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                value = messages[0].get("id")
                if isinstance(value, str):
                    last_message_id = value
        return last_message_id

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"https://graph.facebook.com/{self.config.graph_version}/"
            f"{self.config.phone_number_id}/messages"
        )
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.access_token}",
                "Content-Type": "application/json",
                "User-Agent": "AgentGenia-WhatsApp/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(1024 * 1024)
                parsed = json.loads(body) if body else {}
                if not isinstance(parsed, dict):
                    raise WhatsAppError("Meta devolvió una respuesta inválida")
                return parsed
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(64 * 1024)).get("error", {})
                code = detail.get("code") if isinstance(detail, dict) else None
            except Exception:
                code = None
            raise WhatsAppError(
                f"WhatsApp Cloud API rechazó el mensaje (HTTP {exc.code}, código {code or 'unknown'})"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise WhatsAppError("No fue posible contactar WhatsApp Cloud API") from exc


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
