"""Validation and canonicalization for cross-device Agent Genia state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .connectors import CONNECTOR_CATALOG


MAX_ACCOUNT_STATE_BYTES = 900 * 1024
BOT_COLORS = frozenset(
    {
        "#a66d35", "#ff2f43", "#ff6a00", "#ff9300", "#08be70",
        "#11b9a9", "#2f91f5", "#8654ed", "#f35ca7", "#808080",
    }
)
BOT_SHAPES = frozenset(
    {"circle", "bean", "square", "capsule", "triangle", "hexagon", "cloud", "drop"}
)


class AccountStateError(ValueError):
    pass


def empty_account_state() -> dict[str, Any]:
    return {
        "version": 2,
        "onboardingCompleted": False,
        "selectedConnectorIds": [],
        "bots": [],
        "deletedBotIds": [],
        "activeBotId": None,
    }


def normalize_account_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AccountStateError("state debe ser un objeto JSON")
    selected = _connector_ids(value.get("selectedConnectorIds"))
    deleted_bot_ids = _bounded_ids(value.get("deletedBotIds"), 200, "state.deletedBotIds")
    deleted = set(deleted_bot_ids)
    bots = []
    seen: set[str] = set()
    raw_bots = value.get("bots")
    if raw_bots is not None and not isinstance(raw_bots, list):
        raise AccountStateError("state.bots debe ser una lista")
    if len(raw_bots or []) > 100:
        raise AccountStateError("state.bots admite como máximo 100 elementos")
    for raw in (raw_bots or []):
        bot = _bot(raw)
        if bot and bot["id"] not in seen and bot["id"] not in deleted:
            seen.add(bot["id"])
            bots.append(bot)
    active = _text(value.get("activeBotId"), 100)
    if active not in seen:
        active = bots[0]["id"] if bots else None
    state = {
        "version": 2,
        "onboardingCompleted": value.get("onboardingCompleted") is True,
        "selectedConnectorIds": selected,
        "bots": bots,
        "deletedBotIds": deleted_bot_ids,
        "activeBotId": active,
    }
    encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_ACCOUNT_STATE_BYTES:
        raise AccountStateError("El estado de la cuenta excede 900 KB")
    return state


def account_state_json(value: Any) -> str:
    return json.dumps(
        normalize_account_state(value), separators=(",", ":"), ensure_ascii=False
    )


def _bot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    bot_id = _text(value.get("id"), 100)
    name = " ".join(_text(value.get("name"), 60).split())
    if not bot_id or not name:
        return None
    _require_text_limit(value.get("title"), 100, "bot.title")
    _require_text_limit(value.get("description"), 600, "bot.description")
    color = _text(value.get("color"), 7).lower()
    shape = _text(value.get("shape"), 20).lower()
    return {
        "id": bot_id,
        "name": name,
        "title": _text(value.get("title"), 100, multiline=True),
        "description": _text(value.get("description"), 600, multiline=True),
        "color": color if color in BOT_COLORS else "#2f91f5",
        "shape": shape if shape in BOT_SHAPES else "circle",
        "avatarDataUrl": _avatar(value.get("avatarDataUrl")),
        "notificationsEnabled": value.get("notificationsEnabled") is not False,
        "connectorIds": _connector_ids(value.get("connectorIds")),
        "messages": _messages(value.get("messages")),
        "workflows": _workflows(value.get("workflows")),
        "createdAt": _date(value.get("createdAt")),
    }


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    if len(value) > 200:
        raise AccountStateError("bot.messages admite como máximo 200 elementos")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        text = _text(item.get("text"), 20_000, multiline=True)
        message_id = _text(item.get("id"), 100)
        if not text or not message_id:
            continue
        message = {
            "id": message_id,
            "role": item["role"],
            "text": text,
            "createdAt": _date(item.get("createdAt")),
        }
        widget = _widget(item.get("widget")) if item["role"] == "assistant" else None
        if widget:
            message["widget"] = widget
        result.append(message)
    return result


def _widget(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    prompt = _text(value.get("prompt"), 300, multiline=True)
    raw_options = value.get("options")
    if not prompt or not isinstance(raw_options, list):
        return None
    options = []
    for item in raw_options[:6]:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), 120, multiline=True)
        if not label:
            continue
        options.append(
            {
                "label": label,
                "value": _text(item.get("value"), 300, multiline=True) or label,
                "description": _text(item.get("description"), 240, multiline=True),
            }
        )
    if not options:
        return None
    return {
        "prompt": prompt,
        "helpText": _text(value.get("helpText"), 500, multiline=True),
        "options": options,
        "allowCustom": value.get("allowCustom") is True,
        "dismissOnMoveOn": value.get("dismissOnMoveOn") is not False,
    }


def _workflows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    if len(value) > 50:
        raise AccountStateError("bot.workflows admite como máximo 50 elementos")
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        workflow_id = _text(item.get("id"), 100)
        title = " ".join(_text(item.get("title"), 120).split())
        raw_steps = item.get("steps")
        steps = [
            step for raw in (raw_steps[:30] if isinstance(raw_steps, list) else [])
            if (step := _text(raw, 600, multiline=True))
        ]
        if not workflow_id or not title or not steps:
            continue
        mime = item.get("recordingMimeType")
        result.append(
            {
                "id": workflow_id,
                "title": title,
                "summary": _text(item.get("summary"), 500, multiline=True),
                "steps": steps,
                "recordingId": _text(item.get("recordingId"), 100),
                "recordingMimeType": mime if mime in {"video/webm", "video/mp4"} else "",
                "createdAt": _date(item.get("createdAt")),
                "updatedAt": _date(item.get("updatedAt")),
                "lastRunAt": _date(item.get("lastRunAt"), allow_empty=True),
            }
        )
    return result


def _connector_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for raw in value:
        item = raw if isinstance(raw, str) else ""
        if item in CONNECTOR_CATALOG and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _bounded_ids(value: Any, limit: int, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise AccountStateError(f"{field} admite como máximo {limit} elementos")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _text(raw, 100)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _avatar(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 700_000:
        return ""
    lowered = value[:40].lower()
    return value if lowered.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")) else ""


def _text(value: Any, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not multiline:
        normalized = normalized.replace("\n", " ")
    return normalized[:limit]


def _require_text_limit(value: Any, limit: int, field: str) -> None:
    if isinstance(value, str) and len(value) > limit:
        raise AccountStateError(f"{field} admite como máximo {limit} caracteres")


def _date(value: Any, *, allow_empty: bool = False) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    parsed: datetime | None = None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp < 10_000_000_000:
            # Android legacy used Unix milliseconds; Swift's default Date
            # encoding used seconds since 2001. Both are accepted here.
            if timestamp > 10_000_000:
                timestamp += 978_307_200
        else:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, OSError, OverflowError):
            parsed = None
    if parsed is None:
        raise AccountStateError("Fecha inválida en el estado de cuenta")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
