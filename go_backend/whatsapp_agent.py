"""Pure routing and prompt helpers for the WhatsApp Agent Genia channel."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from .connectors import CONNECTOR_CATALOG


LINK_CODE_RE = re.compile(r"\bAG-[A-Z2-9]{4}-[A-Z2-9]{4}\b", re.IGNORECASE)
CREATE_BOT_RE = re.compile(
    r"\b(?:crea|crear|haz|necesito)\s+(?:(?:un|una|otro|otra)\s+)?(?:bot|agente)\b"
    r"(?:\s+(?:(?:llamad[oa]|que\s+se\s+llame)\s+([^,.!?]+)|(?:para|que)\s+(.+)))?",
    re.IGNORECASE,
)

CONNECTOR_ACTION_RE = {
    "connect": re.compile(
        r"\b(?:conecta|conectar|agrega|agregar|anade|anadir|instala|instalar|"
        r"autoriza|autorizar|vincula|vincular|link|connect|add|install|authorize)\b"
    ),
    "disconnect": re.compile(
        r"\b(?:desconecta|desconectar|quita|quitar|elimina|eliminar|"
        r"desvincula|desvincular|disconnect|remove|unlink)\b"
    ),
}
CONNECTOR_LIST_RE = re.compile(
    r"\b(?:mis conexiones|mis conectores|mis plugins|conexiones conectadas|"
    r"conectores conectados|plugins conectados|que conexiones tengo|"
    r"que conectores tengo|que plugins tengo|list connections|my connections)\b"
)
CONNECTOR_REFRESH_RE = re.compile(
    r"^(?:listo|ya conecte|ya lo conecte|ya autorice|termine|done|connected)$"
)
APPROVAL_DECISION_RE = {
    "approve": re.compile(
        r"^(?:autorizar|autorizo|aprobar|apruebo|confirmar|confirmo|"
        r"si,?\s+(?:autoriza|aprueba|confirmo)|authorize|approve|confirm)$"
    ),
    "reject": re.compile(
        r"^(?:cancelar|cancelo|rechazar|rechazo|no,?\s+(?:cancelar|cancela)|"
        r"cancel|reject)$"
    ),
}
LIKELY_CONNECTOR_ACTION_RE = re.compile(
    r"\b(?:haz|hacer|prepara|preparar|genera|generar|crea|crear|organiza|organizar|"
    r"actualiza|actualizar|modifica|modificar|publica|publicar|envia|enviar|manda|mandar|"
    r"agenda|agendar|programa|programar|elimina|eliminar|borra|borrar|sube|subir|"
    r"descarga|descargar|sincroniza|sincronizar|importa|importar|exporta|exportar|"
    r"make|prepare|generate|create|organize|update|edit|publish|post|send|schedule|"
    r"delete|remove|upload|download|sync|import|export)\b"
)

_CONNECTOR_ALIASES: dict[str, tuple[str, ...]] = {
    "google-workspace": (
        "google workspace", "gmail", "google calendar", "google drive",
        "google contacts", "google sheets", "drive", "sheets",
    ),
    "microsoft-365": (
        "microsoft 365", "office 365", "outlook", "onedrive", "microsoft teams",
    ),
    "monday-com": ("monday.com", "monday com", "monday"),
    "quickbooks": ("quickbooks", "quick books"),
}


def extract_link_code(text: str) -> str | None:
    match = LINK_CODE_RE.search(text.upper())
    return match.group(0).upper() if match else None


def wants_bot_list(text: str) -> bool:
    normalized = _normalized(text)
    return bool(
        re.search(
            r"\b(?:lista|muestra|cuales|que)\b.*\b(?:bots|agentes)\b|"
            r"\b(?:mis bots|mis agentes)\b",
            normalized,
        )
    )


def format_bot_list(bots: list[dict[str, Any]], request_text: str) -> str:
    """Render a compact bot inventory without spending an LLM call.

    Repeated display names are valid (and common after creating several test
    agents), but repeating the same line hundreds of times is neither useful
    nor cheap to deliver through WhatsApp. Preserve the real count while
    grouping equal names and honor explicit one-line requests.
    """
    if not bots:
        return "Todavía no tienes agentes. Puedes decir: “Crea un agente para cotizaciones”."

    grouped: dict[str, dict[str, Any]] = {}
    for bot in bots:
        raw_name = bot.get("name") if isinstance(bot, dict) else None
        name = str(raw_name or "Sin nombre").strip()[:80] or "Sin nombre"
        key = name.casefold()
        if key not in grouped:
            grouped[key] = {"name": name, "count": 0}
        grouped[key]["count"] += 1

    groups = list(grouped.values())
    visible = groups[:20]
    labels = [
        f"{item['name']} ({item['count']})" if item["count"] > 1 else item["name"]
        for item in visible
    ]
    hidden_count = sum(int(item["count"]) for item in groups[20:])
    if hidden_count:
        labels.append(f"+{hidden_count} agentes más")

    normalized = _normalized(request_text)
    one_line = bool(re.search(r"\b(?:en|una)\s+(?:sola\s+)?linea\b|\bone line\b", normalized))
    if one_line:
        return "Tus agentes: " + ", ".join(labels) + "."
    return "Tus agentes son:\n• " + "\n• ".join(labels)


def connector_command(text: str) -> tuple[str, str | None] | None:
    """Parsea comandos de conectores sin enviar la intención al LLM.

    El OAuth sigue siendo propiedad del gateway privado del backend. Esta
    función únicamente convierte lenguaje natural en una acción cerrada y un
    id presente en el catálogo.
    """
    normalized = _normalized(text)
    if CONNECTOR_LIST_RE.search(normalized):
        return "list", None
    if CONNECTOR_REFRESH_RE.fullmatch(normalized):
        return "refresh", None
    action = next(
        (name for name, pattern in CONNECTOR_ACTION_RE.items() if pattern.search(normalized)),
        None,
    )
    if not action:
        return None
    candidates: list[tuple[int, str]] = []
    for connector_id, item in CONNECTOR_CATALOG.items():
        aliases = {
            _normalized(connector_id.replace("-", " ")),
            _normalized(str(item["name"])),
            *(_normalized(alias) for alias in _CONNECTOR_ALIASES.get(connector_id, ())),
        }
        for alias in aliases:
            if alias and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized):
                candidates.append((len(alias), connector_id))
    if not candidates:
        return None
    # Prefer the most explicit name ("Google Calendar" over "Google").
    return action, max(candidates)[1]


def approval_decision(text: str) -> str | None:
    """Recognize only explicit, standalone consent or rejection.

    Generic replies such as ``sí`` are deliberately insufficient for a side
    effect. WhatsApp has no typed approval action payload, so this strict text
    boundary is the equivalent of tapping the app's one-shot approval card.
    """
    normalized = _normalized(text).strip(" .!¡?¿")
    return next(
        (
            decision
            for decision, pattern in APPROVAL_DECISION_RE.items()
            if pattern.fullmatch(normalized)
        ),
        None,
    )


def likely_connector_action(text: str) -> bool:
    """Conservatively route imperative work to assigned connector tools."""
    return bool(LIKELY_CONNECTOR_ACTION_RE.search(_normalized(text)))


def requested_bot(state: dict[str, Any], text: str) -> dict[str, Any] | None:
    normalized_text = _normalized(text)
    candidates = sorted(
        (bot for bot in state.get("bots", []) if isinstance(bot, dict)),
        key=lambda bot: len(str(bot.get("name") or "")),
        reverse=True,
    )
    for bot in candidates:
        name = _normalized(str(bot.get("name") or ""))
        if len(name) >= 2 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", normalized_text):
            return bot
    return None


def create_bot_from_request(text: str) -> dict[str, Any] | None:
    match = CREATE_BOT_RE.search(text.strip())
    if not match:
        return None
    explicit_name = (match.group(1) or "").strip()
    purpose = (match.group(2) or "").strip()
    if explicit_name:
        name = _clean_name(explicit_name)
        description = purpose
    elif purpose:
        name = _name_from_purpose(purpose)
        description = purpose
    else:
        name = "Nuevo bot"
        description = ""
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "title": description[:100],
        "description": description[:600],
        "color": "#2f91f5",
        "shape": "circle",
        "avatarDataUrl": "",
        "notificationsEnabled": True,
        "connectorIds": [],
        "messages": [],
        "workflows": [],
        "createdAt": now,
        "updatedAt": now,
    }


def build_bot_prompt(
    bot: dict[str, Any],
    user_prompt: str,
    *,
    history_message_limit: int = 20,
    history_char_limit: int = 60_000,
) -> str:
    messages = bot.get("messages") if isinstance(bot.get("messages"), list) else []
    history_lines: list[str] = []
    history_chars = 0
    # Build backwards so the newest context wins, with a hard total budget
    # below the backend's final prompt limit.
    message_limit = max(0, min(int(history_message_limit), 40))
    char_limit = max(0, min(int(history_char_limit), 80_000))
    for message in reversed(messages[-message_limit:] if message_limit else []):
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            continue
        line = (
            f"{'Usuario' if message.get('role') == 'user' else bot['name']}: "
            f"{message.get('text', '')[:8_000]}"
        )
        if history_chars + len(line) + 1 > char_limit:
            break
        history_lines.append(line)
        history_chars += len(line) + 1
    history = "\n".join(reversed(history_lines))
    connectors = bot.get("connectorIds") if isinstance(bot.get("connectorIds"), list) else []
    profile = "\n".join(
        part
        for part in (
            f"Eres {bot['name']}, un agente de Agent Genia.",
            f"Rol: {bot.get('title')}." if bot.get("title") else "",
            f"Objetivo: {bot.get('description')}." if bot.get("description") else "",
            (
                "Conectores autorizables: " + ", ".join(str(item) for item in connectors) + "."
                if connectors
                else "No hay conectores seleccionados."
            ),
            "Estás conversando mediante el canal oficial de WhatsApp de la cuenta. ",
            "Si la tarea necesita GUI, pantalla, shell o archivos, busca primero 'computadora' con connector_search; úsala solo si la búsqueda la ofrece para esta ejecución.",
            "Responde en el idioma del usuario y sin afirmar que realizaste acciones que no ejecutaste.",
            "Sé directo: normalmente usa entre una y tres frases de texto plano. No repitas la solicitud, no añadas preámbulos, cierres, emojis decorativos ni preguntas genéricas. Tras ejecutar una acción confirma únicamente qué hiciste y los datos esenciales; no muestres URLs ni detalles internos salvo que se pidan. No agregues Meet, invitados, ubicación, duración u otros datos no solicitados.",
            "Devuelve exclusivamente JSON válido con esta forma: {\"text\":\"respuesta visible\",\"widget\":null}.",
            "En WhatsApp no emitas widgets. Si necesitas información o aprobación, haz una pregunta breve en text.",
        )
        if part
    )
    history_section = f"\n\nConversación reciente:\n{history}" if history else ""
    result = f"{profile}{history_section}\n\nUsuario: {user_prompt[:20_000]}"
    return result[:95_000]


def parse_agent_answer(value: str) -> str:
    raw = value.strip()[:20_000]
    if not raw:
        return "La tarea terminó sin una respuesta visible."
    candidate = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            text = parsed["text"].strip()
            if text:
                widget = parsed.get("widget")
                if isinstance(widget, dict) and widget.get("type") == "approval":
                    prompt = widget.get("prompt")
                    if isinstance(prompt, str) and prompt.strip():
                        return (
                            f"{text}\n\n{prompt.strip()}\n\n"
                            "Responde “AUTORIZAR” para ejecutarla o “CANCELAR” para descartarla."
                        )[:20_000]
                return text[:20_000]
    except json.JSONDecodeError:
        pass
    return raw


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(char for char in value if not unicodedata.combining(char)).split())


def _clean_name(value: str) -> str:
    cleaned = " ".join(value.split()).strip(" .,:;!¡?¿\"'")
    return (cleaned or "Nuevo bot")[:60]


def _name_from_purpose(value: str) -> str:
    cleaned = re.split(r"[,.!?]|\s+(?:y|pero|cuando)\s+", value, maxsplit=1)[0]
    words = [word for word in cleaned.strip().split() if word]
    while words and _normalized(words[0]) in {"mis", "mi", "el", "la", "los", "las"}:
        words.pop(0)
    candidate = " ".join(words[:5]) or "Nuevo bot"
    return _clean_name(candidate[:1].upper() + candidate[1:])
