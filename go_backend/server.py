"""Servidor HTTP del wrapper backend.

Endpoints publicos (Bearer = api key del usuario del wrapper):
  POST /v1/account-auth/start    Iniciar login con Google
  POST /v1/account-auth/status  Consumir login desde el dispositivo original
  GET  /v1/account-auth/status/<attempt_id>  Consultar login desde Electron
  GET  /v1/account-auth/google/callback
  POST /v1/account-auth/refresh  Rotar la sesión del dispositivo
  POST /v1/account-auth/logout   Revocar la sesión actual
  POST /v1/account-auth/apple    Verificar Sign in with Apple nativo
  POST /v1/account/delete        Eliminar definitivamente la cuenta autenticada
  GET  /v1/account-state         Leer bots y preferencias sincronizados
  POST /v1/account-state         Guardar estado con control de revisión
  POST /v1/whatsapp/link         Generar un enlace de vinculación de un solo uso
  GET  /v1/whatsapp/status       Consultar la identidad de WhatsApp vinculada
  POST /v1/whatsapp/unlink       Desvincular WhatsApp de la cuenta
  GET|POST /v1/whatsapp/webhook  Verificación y eventos firmados de Meta
  GET  /v1/connectors            Catalogo y conexiones del usuario
  GET  /v1/connectors/<id>       Estado de una cuenta conectada
  POST /v1/connectors/start      Crear un Connect Link de Composio
  POST /v1/connectors/status     Consultar/consumir el consentimiento
  POST /v1/connectors/disconnect Revocar la cuenta del usuario
  POST /v1/signup          Crear usuario free (los tiers pagados requieren admin/pago verificado)
  GET  /v1/billing         Consultar plan y suscripción Stripe
  POST /v1/billing/checkout Crear Checkout para Starter/Pro/Business
  POST /v1/billing/portal  Abrir Customer Portal
  POST /v1/billing/webhook Procesar eventos Stripe firmados
  GET  /v1/models          Catalogo de modelos de DeepSeek
  POST /v1/chat/completions
  GET  /v1/agent/status    Estado del harness de Pi
  POST /v1/agent/warm      Precalentar la sesión aislada de un bot
  POST /v1/agent/run       Ejecutar una tarea con Pi
  GET  /v1/computers/<bot_id> Estado de la computadora persistente del bot
  POST /v1/computers/<bot_id>/ensure Crear/despertar y obtener un viewer firmado
  POST /v1/computers/<bot_id>/hand-back Hibernar conservando el filesystem
  POST /v1/computers/<bot_id>/delete Eliminar la computadora del bot
  GET  /v1/usage           Uso por ventanas con limites ajustados al tier
  GET  /v1/me

Endpoints admin (Bearer ADMIN_TOKEN):
  GET  /admin/users
  POST /admin/users/<id>/revoke
  POST /admin/users/<id>/tier  Cambiar tier
  GET  /admin/usage

CLI:
  python3 -m go_backend.server init-db
  python3 -m go_backend.server serve [--port N]
  python3 -m go_backend.server users | usage
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import uuid
import httpx
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .apple_auth import AppleAccountAuth, AppleAuthError
from .account_state import AccountStateError, empty_account_state, normalize_account_state
from .billing import BillingConfig, BillingError, BillingService
from .connector_adapters import (
    ComposioConnectorAdapter,
    ComposioConnectorGateway,
    parse_config_mapping,
)
from .native_connectors import NativeConnectorGateway
from .crypto_utils import (
    CryptoError,
    decrypt_api_key,
    encrypt_api_key,
    parse_secret_versions,
)
from .connectors import (
    CONNECTOR_CATALOG,
    ConnectorBroker,
    ConnectorBrokerError,
    canonical_arguments_hash,
)
from .computers import ComputerConfig, ComputerError, ComputerManager
from .credits import CreditConfig, CreditService, credits_float
from .deepseek_prices import estimate_cost_microusd
from .google_auth import GoogleAccountAuth, GoogleAuthError, completion_html
from .opencode_prices import estimate_cost_microusd as estimate_opencode_cost_microusd
from .pi_harness import (
    CHROME_ISOLATION_PER_RUN,
    PiHarness,
    PiHarnessBusy,
    PiHarnessCancelled,
    PiHarnessError,
    PiHarnessTimeout,
    PiHarnessUsageError,
    PiRunResult,
)
from .postgres_store import create_store
from .store import AccountStateConflict, hash_agent_run_token
from .tiers import (
    DEFAULT_TIER,
    effective_limits,
    has_model_access,
    is_valid,
    plan_for,
    plan_payload,
    tier_label,
)
from .upstream import DEFAULT_UA, proxy_request
from .whatsapp import (
    WhatsAppCloudAPI,
    WhatsAppConfig,
    WhatsAppError,
    parse_webhook_messages,
    verify_webhook_signature,
)
from .whatsapp_agent import (
    build_bot_prompt as build_whatsapp_bot_prompt,
    connector_command as whatsapp_connector_command,
    create_bot_from_request,
    extract_link_code,
    parse_agent_answer as parse_whatsapp_agent_answer,
    requested_bot,
    wants_bot_list,
)

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "wrapper.sqlite"
DEFAULT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "secret.key"
DEFAULT_PI_RUNS = Path(__file__).resolve().parent.parent / "data" / "pi-runs"
DEFAULT_PI_BIN = Path(__file__).resolve().parent.parent / "scripts" / "pi-sandbox"
DEFAULT_PI_CHROME_EXTENSION = (
    Path(__file__).resolve().parent.parent
    / "node_modules"
    / "pi-chrome"
    / "extensions"
    / "chrome-profile-bridge"
    / "index.ts"
)
DEFAULT_PI_CONNECTOR_EXTENSION = (
    Path(__file__).resolve().parent.parent / "extensions" / "connectors" / "index.ts"
)
DEFAULT_DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_OPENCODE_BASE = "https://opencode.ai/zen/go/v1"
MAX_BODY = 16 * 1024 * 1024
MAX_JSON_BODY = 1024 * 1024
MAX_STRIPE_WEBHOOK_BODY = 1024 * 1024  # Los eventos Stripe normales son mucho menores.
MAX_WHATSAPP_WEBHOOK_BODY = 1024 * 1024
UNSAFE_ADMIN_TOKENS = frozenset({"cambia-este-token"})
JSON_TEXT_FIELD_RE = re.compile(r'"text"\s*:\s*"')
READ_ONLY_CONNECTOR_OPERATIONS = frozenset({"search", "query_database", "select_query"})
READ_ONLY_CONNECTOR_PREFIXES = ("search_", "read_", "list_", "get_", "query_", "describe_", "enrich_")
READ_ONLY_COMPUTER_OPERATIONS = frozenset({"status", "screenshot", "list_files", "read_file"})
MAX_EAGER_CONNECTORS = 8
AGENT_RESPONSE_STYLE_INSTRUCTION = (
    "Responde directamente en el idioma del usuario, normalmente en una a tres "
    "frases. Usa texto plano: no Markdown ni emojis decorativos. No repitas la "
    "solicitud, no añadas preámbulos, cierres o preguntas genéricas. Tras una "
    "acción confirma solo qué hiciste y los datos esenciales. No muestres URLs "
    "crudas ni detalles internos salvo que se pidan. No agregues Meet, invitados, "
    "ubicación, duración u otros datos que el usuario no haya solicitado. Nunca "
    "muestres razonamiento interno, deliberación, planes de herramientas, intentos "
    "intermedios ni frases como 'let me think/reconsider'; entrega solo la conclusión "
    "visible y honesta. En el campo text, comienza la conclusión exactamente con "
    "'FINAL: '; ese marcador es de transporte y no será visible para el usuario."
)


def _connector_operation_is_read_only(operation: Any) -> bool:
    return bool(
        isinstance(operation, str)
        and (
            operation in READ_ONLY_CONNECTOR_OPERATIONS
            or operation.startswith(READ_ONLY_CONNECTOR_PREFIXES)
        )
    )


def _action_summary(
    *, target_type: str, connector_id: str, operation: str, arguments: dict[str, Any]
) -> str:
    """Produce a bounded, human-auditable description of the exact action."""
    if connector_id == "google-workspace" and operation in {"send_email", "draft_email"}:
        recipient = str(
            arguments.get("recipient_email")
            or arguments.get("to")
            or arguments.get("recipient")
            or "destinatario no indicado"
        )[:320]
        subject = str(arguments.get("subject") or arguments.get("title") or "sin asunto")[:200]
        body = str(
            arguments.get("body")
            or arguments.get("body_text")
            or arguments.get("message")
            or ""
        ).strip()
        if len(body) > 240:
            body = body[:237].rstrip() + "..."
        verb = "Enviar" if operation == "send_email" else "Crear borrador de"
        return (
            f"{verb} correo a {recipient} con asunto «{subject}»"
            f" y contenido «{body}»" if body else
            f"{verb} correo a {recipient} con asunto «{subject}» y contenido vacío"
        )
    if connector_id == "google-workspace" and operation in {
        "create_calendar_event", "delete_calendar_event",
    }:
        if operation == "create_calendar_event":
            title = str(arguments.get("summary") or arguments.get("title") or "evento")[:200]
            start = str(arguments.get("start_datetime") or arguments.get("start") or "")[:100]
            return f"Crear el evento «{title}»{f' para {start}' if start else ''}"
        event_id = str(arguments.get("event_id") or arguments.get("id") or "")[:200]
        return f"Eliminar el evento con identificador {event_id}"
    label = "Computadora" if target_type == "computer" else CONNECTOR_CATALOG.get(
        connector_id, {"name": connector_id}
    )["name"]
    encoded = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"{label}: {operation} con {encoded[:700]}"


def _approved_connector_confirmation(action: dict[str, Any]) -> str:
    """Return a concise deterministic confirmation after an exact write."""
    connector_id = str(action.get("connector_id") or "")
    operation = str(action.get("operation") or "")
    arguments = action.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if connector_id == "google-workspace":
        if operation == "send_email":
            recipient = str(arguments.get("recipient_email") or "el destinatario")[:320]
            subject = str(arguments.get("subject") or "sin asunto")[:200]
            return f"Listo. Envié el correo a {recipient} con asunto «{subject}»."
        if operation == "draft_email":
            recipient = str(arguments.get("recipient_email") or "el destinatario")[:320]
            subject = str(arguments.get("subject") or "sin asunto")[:200]
            return f"Listo. Guardé el borrador para {recipient} con asunto «{subject}»."
        if operation == "create_calendar_event":
            title = str(arguments.get("summary") or "evento")[:200]
            start = str(arguments.get("start_datetime") or "")[:100]
            return f"Listo. Creé «{title}»{f' para {start}' if start else ''}."
        if operation == "delete_calendar_event":
            return "Listo. Eliminé el evento solicitado."
        if operation == "update_sheet":
            cell_range = str(arguments.get("range") or "el rango solicitado")[:500]
            return f"Listo. Actualicé {cell_range} en la hoja indicada."
    connector_name = str(
        CONNECTOR_CATALOG.get(connector_id, {"name": connector_id or "el conector"})["name"]
    )
    summary = str(action.get("human_summary") or operation or "la acción solicitada")
    return f"Listo. Completé la acción en {connector_name}: {summary}."


def _approval_envelope(approval: dict[str, Any]) -> str:
    approval_id = str(approval["id"])
    summary = str(approval["human_summary"])
    return json.dumps(
        {
            "text": "Confirma esta acción antes de que la ejecute.",
            "widget": {
                "type": "approval",
                "approvalId": approval_id,
                "prompt": summary,
                "helpText": "La autorización sirve una sola vez y solo para estos datos exactos.",
                "options": [
                    {
                        "label": "Autorizar",
                        "value": "Autorizar esta acción",
                        "description": "Ejecutar exactamente la acción mostrada",
                        "action": {
                            "type": "approval",
                            "approvalId": approval_id,
                            "decision": "approve",
                        },
                    },
                    {
                        "label": "Cancelar",
                        "value": "Cancelar esta acción",
                        "description": "No realizar ningún cambio",
                        "action": {
                            "type": "approval",
                            "approvalId": approval_id,
                            "decision": "reject",
                        },
                    },
                ],
                "allowCustom": False,
                "dismissOnMoveOn": False,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _plain_intent_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(
        "".join(char for char in folded if not unicodedata.combining(char)).split()
    )


# OpenAI-compatible routes exposed by the wrapper.
UPSTREAM_PATHS = {
    "/v1/chat/completions": "/chat/completions",
    "/v1/models": "/models",
}


class RequestBodyError(ValueError):
    pass


class RequestBodyTooLarge(RequestBodyError):
    pass


class UnsafeConfigurationError(RuntimeError):
    pass


class ModelProviderUnavailable(RuntimeError):
    pass


def build_commit() -> str:
    """Return the immutable deployment revision without exposing other env data."""
    value = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
        or ""
    ).strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else ""


def runtime_memory_limit_mb() -> int | None:
    """Return the container memory ceiling without exposing host telemetry."""
    limits: list[int] = []
    for candidate in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            raw = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if raw.lower() == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # Some cgroup v1 hosts expose an enormous sentinel instead of `max`.
        if 0 < value < (1 << 60):
            limits.append(value // (1024 * 1024))
    return min(limits) if limits else None


class DirectChatError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = "upstream_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def validate_admin_token(admin_token: str | None) -> None:
    """Impide arrancar con secretos publicados en ejemplos o documentación."""
    if admin_token and admin_token.strip().lower() in UNSAFE_ADMIN_TOKENS:
        raise UnsafeConfigurationError(
            "ADMIN_TOKEN usa el valor inseguro de ejemplo 'cambia-este-token'. "
            "Genera un secreto aleatorio antes de arrancar."
        )


def validate_pi_chrome_security(chrome_isolation: str) -> None:
    """Prohibe perfiles compartidos en el backend multiusuario."""
    if chrome_isolation != CHROME_ISOLATION_PER_RUN:
        raise UnsafeConfigurationError(
            "PI_CHROME_ISOLATION debe ser 'per_run'. Los perfiles Chrome compartidos "
            "estan prohibidos en este backend multiusuario."
        )


def _validate_service_url(value: str, label: str, *, allowed_hosts: set[str] | None = None) -> None:
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise UnsafeConfigurationError(f"{label} no es una URL válida") from exc
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        or (allowed_hosts is not None and parsed.hostname not in allowed_hosts)
    ):
        raise UnsafeConfigurationError(f"{label} no apunta a un destino permitido")


class Config:
    def __init__(self):
        self.environment = (os.environ.get("ENVIRONMENT") or "development").strip().lower()
        if self.environment not in {"development", "production"}:
            raise UnsafeConfigurationError(
                "ENVIRONMENT debe ser 'development' o 'production'"
            )
        self.db_path = Path(os.environ.get("DB_PATH", str(DEFAULT_DB)))
        self.database_url = (os.environ.get("DATABASE_URL") or "").strip() or None
        self.secret_file = Path(os.environ.get("SECRET_FILE", str(DEFAULT_SECRET_FILE)))
        self.deepseek_base_url = os.environ.get(
            "DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE
        ).rstrip("/")
        self.deepseek_api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        self.opencode_base_url = os.environ.get(
            "OPENCODE_BASE_URL", DEFAULT_OPENCODE_BASE
        ).rstrip("/")
        self.host = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
        self.port = int(os.environ.get("PORT", "8787"))
        self.enforce_limits = os.environ.get("ENFORCE_LIMITS", "1") != "0"
        try:
            self.credits = CreditConfig(
                mode=(os.environ.get("CREDITS_MODE") or "shadow").strip().lower(),
                llm_multiplier_bps=int(os.environ.get("CREDIT_LLM_MULTIPLIER_BPS", "12500")),
                display_increment_milli=int(
                    os.environ.get("CREDIT_DISPLAY_INCREMENT_MILLI", "100")
                ),
                trial_credit_milli=int(Decimal(os.environ.get("TRIAL_CREDITS", "30")) * 1000),
                trial_ttl_days=int(os.environ.get("TRIAL_CREDITS_TTL_DAYS", "30")),
                default_run_max_milli=int(
                    Decimal(os.environ.get("DEFAULT_RUN_MAX_CREDITS", "25")) * 1000
                ),
                deep_run_max_milli=int(
                    Decimal(os.environ.get("DEEP_RUN_MAX_CREDITS", "50")) * 1000
                ),
                reservation_ttl_seconds=int(
                    os.environ.get("CREDIT_RESERVATION_TTL_SECONDS", "3900")
                ),
            )
            self.credits.validate()
        except (ValueError, InvalidOperation) as exc:
            raise UnsafeConfigurationError(f"Configuración de créditos inválida: {exc}") from exc
        self.wrapper_secret = os.environ.get("WRAPPER_SECRET") or None
        self.wrapper_secret_version = int(os.environ.get("WRAPPER_SECRET_VERSION", "1"))
        try:
            self.wrapper_secret_versions = parse_secret_versions(
                os.environ.get("WRAPPER_SECRET_PREVIOUS_JSON", ""),
                current_version=self.wrapper_secret_version,
                current_secret=self.wrapper_secret,
            )
        except CryptoError as exc:
            raise UnsafeConfigurationError(str(exc)) from exc
        self.admin_token = (os.environ.get("ADMIN_TOKEN") or "").strip() or None
        self.google_oauth_client_id = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip() or None
        self.google_oauth_client_secret = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip() or None
        self.google_oauth_redirect_uri = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip() or None
        self.apple_client_id = (os.environ.get("APPLE_CLIENT_ID") or "").strip() or None
        self.apple_team_id = (os.environ.get("APPLE_TEAM_ID") or "").strip() or None
        self.apple_key_id = (os.environ.get("APPLE_KEY_ID") or "").strip() or None
        self.apple_private_key_base64 = (
            (os.environ.get("APPLE_PRIVATE_KEY_BASE64") or "").strip() or None
        )
        self.account_access_ttl_seconds = int(os.environ.get("ACCOUNT_ACCESS_TTL_SECONDS", "900"))
        self.account_refresh_ttl_seconds = int(os.environ.get("ACCOUNT_REFRESH_TTL_SECONDS", str(30 * 86400)))
        self.account_auth_attempt_ttl_seconds = int(os.environ.get("ACCOUNT_AUTH_ATTEMPT_TTL_SECONDS", "600"))
        self.composio_api_key = (os.environ.get("COMPOSIO_API_KEY") or "").strip()
        self.composio_public_url = (os.environ.get("COMPOSIO_PUBLIC_URL") or "").strip()
        self.connector_public_url = (
            os.environ.get("CONNECTOR_PUBLIC_URL") or self.composio_public_url
        ).strip()
        self.composio_auth_configs_json = os.environ.get("COMPOSIO_AUTH_CONFIGS_JSON", "")
        self.composio_direct_auth_configs_json = os.environ.get(
            "COMPOSIO_DIRECT_AUTH_CONFIGS_JSON", ""
        )
        self.composio_toolkit_overrides_json = os.environ.get(
            "COMPOSIO_TOOLKIT_OVERRIDES_JSON", ""
        )
        self.composio_auth_attempt_ttl_seconds = int(
            os.environ.get("COMPOSIO_AUTH_ATTEMPT_TTL_SECONDS", "600")
        )
        self.stripe_enabled = os.environ.get("STRIPE_ENABLED", "0") == "1"
        self.stripe_live_mode = os.environ.get("STRIPE_LIVE_MODE", "1") != "0"
        self.stripe_secret_key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip() or None
        self.stripe_webhook_secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip() or None
        self.stripe_starter_price_id = (
            os.environ.get("STRIPE_STARTER_PRICE_ID")
            or os.environ.get("STRIPE_PLUS_PRICE_ID")
            or ""
        ).strip() or None
        self.stripe_pro_price_id = (os.environ.get("STRIPE_PRO_PRICE_ID") or "").strip() or None
        self.stripe_business_price_id = (
            os.environ.get("STRIPE_BUSINESS_PRICE_ID") or ""
        ).strip() or None
        self.stripe_success_url = (os.environ.get("STRIPE_SUCCESS_URL") or "").strip() or None
        self.stripe_cancel_url = (os.environ.get("STRIPE_CANCEL_URL") or "").strip() or None
        self.stripe_portal_return_url = (
            (os.environ.get("STRIPE_PORTAL_RETURN_URL") or "").strip() or None
        )
        self.stripe_webhook_tolerance_seconds = int(
            os.environ.get("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300")
        )
        self.pi_enabled = os.environ.get("PI_ENABLED", "0") == "1"
        self.pi_bin = os.environ.get("PI_BIN", str(DEFAULT_PI_BIN))
        warm_sessions_default = "1" if Path(self.pi_bin).name == "pi-render-safe" else "0"
        self.pi_warm_sessions = os.environ.get(
            "PI_WARM_SESSIONS", warm_sessions_default
        ) == "1"
        self.pi_backend_url = os.environ.get(
            "PI_BACKEND_URL", f"http://127.0.0.1:{self.port}"
        ).rstrip("/")
        self.pi_runs_dir = Path(os.environ.get("PI_RUNS_DIR", str(DEFAULT_PI_RUNS)))
        self.pi_model = os.environ.get("PI_MODEL", "deepseek-v4-flash")
        # DeepSeek currently maps both `low` and `medium` to `high`. Keep the
        # agent/tool path honest and deliberate; the latency-sensitive direct
        # chat path explicitly disables thinking in `_run_direct_chat`.
        self.pi_thinking = os.environ.get("PI_THINKING", "high")
        # One exact connector is a bounded operation-selection task. Running
        # DeepSeek's hidden high-effort reasoning there dominated latency while
        # adding no authorization or schema safety; complex/multi-tool work
        # continues to use PI_THINKING.
        self.pi_connector_thinking = os.environ.get(
            "PI_CONNECTOR_THINKING", "off"
        )
        self.pi_timeout_seconds = int(os.environ.get("PI_TIMEOUT_SECONDS", "1800"))
        self.pi_max_concurrent = int(os.environ.get("PI_MAX_CONCURRENT", "4"))
        self.pi_browser_max_concurrent = int(
            os.environ.get("PI_BROWSER_MAX_CONCURRENT", "1")
        )
        self.pi_browser_min_memory_mb = int(
            os.environ.get("PI_BROWSER_MIN_MEMORY_MB", "1024")
        )
        self.pi_max_prompt_chars = int(os.environ.get("PI_MAX_PROMPT_CHARS", "100000"))
        self.pi_session_idle_seconds = int(
            os.environ.get("PI_SESSION_IDLE_SECONDS", "900")
        )
        self.pi_max_warm_sessions = int(
            os.environ.get("PI_MAX_WARM_SESSIONS", str(self.pi_max_concurrent))
        )
        self.pi_node_bin_dir = os.environ.get("PI_NODE_BIN_DIR") or None
        if "PI_CONNECTOR_EXTENSION" in os.environ:
            self.pi_connector_extension = os.environ.get("PI_CONNECTOR_EXTENSION") or None
        else:
            self.pi_connector_extension = str(DEFAULT_PI_CONNECTOR_EXTENSION)
        default_connector_ttl = (
            min(3600, self.pi_timeout_seconds + 60)
            if self.pi_timeout_seconds > 0
            else 3600
        )
        self.pi_connector_token_ttl_seconds = int(
            os.environ.get("PI_CONNECTOR_TOKEN_TTL_SECONDS", str(default_connector_ttl))
        )
        if "PI_CHROME_EXTENSION" in os.environ:
            self.pi_chrome_extension = os.environ.get("PI_CHROME_EXTENSION") or None
        else:
            self.pi_chrome_extension = str(DEFAULT_PI_CHROME_EXTENSION)
        self.pi_chrome_auto_authorize = os.environ.get("PI_CHROME_AUTO_AUTHORIZE", "0") == "1"
        self.pi_chrome_authorize_minutes = int(os.environ.get("PI_CHROME_AUTHORIZE_MINUTES", "30"))
        self.pi_chrome_bin = (os.environ.get("PI_CHROME_BIN") or "").strip() or None
        self.pi_chrome_isolation = os.environ.get(
            "PI_CHROME_ISOLATION", CHROME_ISOLATION_PER_RUN
        ).strip().lower()
        self.computers_enabled = os.environ.get("COMPUTERS_ENABLED", "0") == "1"
        self.external_writes_enabled = os.environ.get("EXTERNAL_WRITES_ENABLED", "0") == "1"
        self.daytona_api_key = (os.environ.get("DAYTONA_API_KEY") or "").strip()
        self.daytona_api_url = (os.environ.get("DAYTONA_API_URL") or "").strip()
        self.daytona_target = (os.environ.get("DAYTONA_TARGET") or "").strip()
        self.daytona_snapshot = (os.environ.get("DAYTONA_SNAPSHOT") or "").strip()
        self.computer_auto_stop_minutes = int(os.environ.get("COMPUTER_AUTO_STOP_MINUTES", "15"))
        self.computer_auto_archive_minutes = int(os.environ.get("COMPUTER_AUTO_ARCHIVE_MINUTES", "1440"))
        self.computer_preview_ttl_seconds = int(os.environ.get("COMPUTER_PREVIEW_TTL_SECONDS", "3600"))
        self.computer_vnc_port = int(os.environ.get("COMPUTER_VNC_PORT", "6080"))
        self.computer_vnc_resolution = os.environ.get("COMPUTER_VNC_RESOLUTION", "1440x900").strip()
        self.computer_basic_limit = int(os.environ.get("COMPUTER_BASIC_LIMIT", "1"))
        self.computer_pro_limit = int(os.environ.get("COMPUTER_PRO_LIMIT", "3"))
        signup_default = "0" if self.environment == "production" else "1"
        self.public_legacy_signup_enabled = (
            os.environ.get("PUBLIC_LEGACY_SIGNUP_ENABLED", signup_default) == "1"
        )
        self.whatsapp_enabled = os.environ.get("WHATSAPP_ENABLED", "0") == "1"
        self.whatsapp_verify_token = (os.environ.get("WHATSAPP_VERIFY_TOKEN") or "").strip()
        self.whatsapp_app_secret = (os.environ.get("WHATSAPP_APP_SECRET") or "").strip()
        self.whatsapp_access_token = (os.environ.get("WHATSAPP_ACCESS_TOKEN") or "").strip()
        self.whatsapp_phone_number_id = (
            os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or ""
        ).strip()
        self.whatsapp_public_number = re.sub(
            r"\D", "", os.environ.get("WHATSAPP_PUBLIC_NUMBER") or ""
        )
        self.whatsapp_graph_version = (
            os.environ.get("WHATSAPP_GRAPH_VERSION") or "v25.0"
        ).strip()
        self.whatsapp_link_ttl_seconds = int(
            os.environ.get("WHATSAPP_LINK_TTL_SECONDS", "600")
        )


def validate_runtime_security(cfg: Config) -> None:
    validate_admin_token(cfg.admin_token)
    if cfg.pi_warm_sessions and Path(cfg.pi_bin).name != "pi-render-safe":
        raise UnsafeConfigurationError(
            "PI_WARM_SESSIONS solo puede usarse con PI_BIN=pi-render-safe"
        )
    if cfg.pi_session_idle_seconds < 0 or cfg.pi_max_warm_sessions < 1:
        raise UnsafeConfigurationError(
            "PI_SESSION_IDLE_SECONDS y PI_MAX_WARM_SESSIONS no son válidos"
        )
    if not 1 <= cfg.pi_browser_max_concurrent <= cfg.pi_max_concurrent:
        raise UnsafeConfigurationError(
            "PI_BROWSER_MAX_CONCURRENT debe estar entre 1 y PI_MAX_CONCURRENT"
        )
    if cfg.pi_browser_min_memory_mb < 0:
        raise UnsafeConfigurationError(
            "PI_BROWSER_MIN_MEMORY_MB no puede ser negativo"
        )
    thinking_levels = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
    if cfg.pi_thinking not in thinking_levels or cfg.pi_connector_thinking not in thinking_levels:
        raise UnsafeConfigurationError(
            "PI_THINKING y PI_CONNECTOR_THINKING no son válidos"
        )
    try:
        WhatsAppConfig(
            enabled=cfg.whatsapp_enabled,
            verify_token=cfg.whatsapp_verify_token,
            app_secret=cfg.whatsapp_app_secret,
            access_token=cfg.whatsapp_access_token,
            phone_number_id=cfg.whatsapp_phone_number_id,
            public_number=cfg.whatsapp_public_number,
            graph_version=cfg.whatsapp_graph_version,
            link_ttl_seconds=cfg.whatsapp_link_ttl_seconds,
        ).validate()
    except ValueError as exc:
        raise UnsafeConfigurationError(f"Configuración de WhatsApp inválida: {exc}") from exc
    if cfg.environment == "production":
        missing = [
            name
            for name, value in (
                ("DATABASE_URL", cfg.database_url),
                ("WRAPPER_SECRET", cfg.wrapper_secret),
                ("ADMIN_TOKEN", cfg.admin_token),
                ("DEEPSEEK_API_KEY", cfg.deepseek_api_key),
                ("GOOGLE_OAUTH_CLIENT_ID", cfg.google_oauth_client_id),
                ("GOOGLE_OAUTH_CLIENT_SECRET", cfg.google_oauth_client_secret),
                ("GOOGLE_OAUTH_REDIRECT_URI", cfg.google_oauth_redirect_uri),
                ("APPLE_CLIENT_ID", cfg.apple_client_id),
                ("APPLE_TEAM_ID", cfg.apple_team_id),
                ("APPLE_KEY_ID", cfg.apple_key_id),
                ("APPLE_PRIVATE_KEY_BASE64", cfg.apple_private_key_base64),
            )
            if not value
        ]
        if missing:
            raise UnsafeConfigurationError(
                "Faltan variables obligatorias en producción: " + ", ".join(missing)
            )
        if cfg.admin_token and len(cfg.admin_token) < 32:
            raise UnsafeConfigurationError("ADMIN_TOKEN debe tener al menos 32 caracteres en producción")
        if cfg.stripe_enabled and cfg.stripe_live_mode and cfg.credits.mode != "enforce":
            raise UnsafeConfigurationError(
                "CREDITS_MODE=enforce es obligatorio con Stripe live"
            )
        if cfg.external_writes_enabled:
            raise UnsafeConfigurationError(
                "EXTERNAL_WRITES_ENABLED no puede activarse en producción hasta que exista aprobación humana por operación"
            )
        _validate_service_url(
            cfg.deepseek_base_url,
            "DEEPSEEK_BASE_URL",
            allowed_hosts={"api.deepseek.com"},
        )
        _validate_service_url(
            cfg.opencode_base_url,
            "OPENCODE_BASE_URL",
            allowed_hosts={"opencode.ai"},
        )
        _validate_service_url(
            cfg.pi_backend_url,
            "PI_BACKEND_URL",
            allowed_hosts={"localhost", "127.0.0.1", "::1"},
        )
        if cfg.daytona_api_url:
            _validate_service_url(cfg.daytona_api_url, "DAYTONA_API_URL")


def json_response(handler: BaseHTTPRequestHandler, status: int, obj) -> None:
    body = json.dumps(obj, allow_nan=False, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    if getattr(handler, "close_connection", False):
        handler.send_header("Connection", "close")
    request_id = getattr(handler, "request_id", "")
    if request_id:
        handler.send_header("X-Request-Id", request_id)
    handler.end_headers()
    handler.wfile.write(body)


def _completion_message_text(body: bytes | None) -> str:
    """Read a non-stream OpenAI-compatible assistant message safely."""
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _expects_agent_envelope(prompt: str) -> bool:
    """Identify our internal text/widget response contract, not arbitrary JSON."""
    return (
        "Devuelve exclusivamente JSON válido" in prompt
        and '"text":"respuesta visible"' in prompt
        and '"widget"' in prompt
    )


def _valid_agent_envelope(value: str) -> bool:
    """Reject partial/model-invented envelopes before clients persist them."""
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        return False
    widget = payload.get("widget")
    if widget is None:
        return True
    if not isinstance(widget, dict):
        return False
    prompt = widget.get("prompt")
    options = widget.get("options")
    valid_question = (
        isinstance(prompt, str)
        and bool(prompt.strip())
        and isinstance(options, list)
        and 1 <= len(options) <= 6
        and all(
            isinstance(option, dict)
            and isinstance(option.get("label"), str)
            and bool(option["label"].strip())
            for option in options
        )
    )
    if not valid_question:
        return False
    if widget.get("type") != "approval":
        return True
    approval_id = widget.get("approvalId")
    return bool(
        isinstance(approval_id, str)
        and approval_id.startswith("apr_")
        and all(
            isinstance(option.get("action"), dict)
            and option["action"].get("type") == "approval"
            and option["action"].get("approvalId") == approval_id
            and option["action"].get("decision") in {"approve", "reject"}
            for option in options
        )
    )


def _agent_envelope_text(value: str) -> str:
    if not _valid_agent_envelope(value):
        return ""
    payload = json.loads(value)
    return str(payload.get("text") or "").strip()


_VISIBLE_DELIBERATION_BOUNDARY_RE = re.compile(
    r"(?:^|\n\s*\n)\s*(?:"
    r"let me report(?: honestly)?(?: what I found| (?:the )?results?)?"
    r"(?: (?:that |this )?accordingly)?[.!]?|"
    r"here (?:is|are) (?:the )?final (?:answer|result)s?[.:]?|"
    r"the final answer is[.:]?|"
    r"final answer[.:]?"
    r")\s*(?:\n\s*\n|$)",
    flags=re.IGNORECASE,
)
_VISIBLE_FINAL_SENTINEL_RE = re.compile(
    r"(?:^|\n\s*\n)\s*FINAL\s*:\s*",
    flags=re.IGNORECASE,
)


def _sentinel_visible_agent_text(value: str) -> str | None:
    boundaries = list(_VISIBLE_FINAL_SENTINEL_RE.finditer(value))
    if not boundaries:
        return None
    return value[boundaries[-1].end():].strip()


def _sanitize_visible_agent_text(value: str) -> str:
    """Remove unmistakable model deliberation accidentally emitted as content.

    Some OpenAI-compatible model gateways occasionally place a short analysis
    preamble in ``content`` instead of ``reasoning_content``. Prompting alone
    cannot make that transport bug fail closed. We only cut at explicit final
    answer boundaries, then collapse a repeated first section heading. This is
    intentionally narrower than a general prose rewriter so legitimate user-
    requested explanations remain untouched.
    """
    candidate = value.strip()
    sentinel_text = _sentinel_visible_agent_text(candidate)
    if sentinel_text is not None:
        return sentinel_text
    boundaries = list(_VISIBLE_DELIBERATION_BOUNDARY_RE.finditer(candidate))
    if not boundaries:
        return candidate
    candidate = candidate[boundaries[-1].end():].strip()
    first_heading = re.match(r"^([^\n:]{1,48}:)", candidate)
    if first_heading:
        heading = first_heading.group(1)
        last_heading = candidate.rfind(heading)
        if last_heading > 0:
            candidate = candidate[last_heading:].strip()
    return candidate


def _sanitize_agent_envelope(value: str) -> str:
    payload = json.loads(value)
    payload["text"] = _sanitize_visible_agent_text(str(payload.get("text") or ""))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _normalized_agent_envelope(value: str) -> str | None:
    """Repair bounded presentation mistakes without accepting raw JSON."""
    candidate = value.strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate).strip()
    if _valid_agent_envelope(candidate):
        return _sanitize_agent_envelope(candidate)
    start = candidate.find("{")
    if start >= 0:
        try:
            payload, _end = json.JSONDecoder().raw_decode(candidate[start:])
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if _valid_agent_envelope(encoded):
                return _sanitize_agent_envelope(encoded)
        except (TypeError, json.JSONDecodeError):
            pass
    if candidate and not candidate.startswith(("{", "[")):
        return json.dumps(
            {"text": _sanitize_visible_agent_text(candidate)[:20_000], "widget": None},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return None


def _bounded_agent_envelope(value: str, limit: int = 20_000) -> str | None:
    """Return a valid envelope within the synchronization character limit.

    Truncating the serialized JSON would corrupt the final response after it
    had already passed validation. Shrink only the human-readable text and
    re-encode the complete envelope instead.
    """
    normalized = _normalized_agent_envelope(value)
    if normalized is None:
        return None
    if len(normalized) <= limit:
        return normalized
    payload = json.loads(normalized)
    text = str(payload.get("text") or "")
    marker = "\n\n[Respuesta truncada por límite de sincronización]"
    low, high = 0, len(text)
    bounded: str | None = None
    while low <= high:
        midpoint = (low + high) // 2
        candidate = dict(payload)
        candidate["text"] = text[:midpoint].rstrip() + marker
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= limit:
            bounded = encoded
            low = midpoint + 1
        else:
            high = midpoint - 1
    return bounded


def _partial_json_text(value: str) -> str | None:
    """Decode the completed prefix of a top-level JSON ``text`` string.

    Agent replies use a JSON envelope so widgets can travel beside visible text.
    This deliberately small decoder lets the UI stream only the human-readable
    field without briefly rendering JSON syntax while the model is still typing.
    """
    match = JSON_TEXT_FIELD_RE.search(value)
    if not match:
        return None
    index = match.end()
    output: list[str] = []
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while index < len(value):
        char = value[index]
        if char == '"':
            break
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            break
        escaped = value[index + 1]
        if escaped == "u":
            digits = value[index + 2:index + 6]
            if len(digits) != 4 or any(item not in "0123456789abcdefABCDEF" for item in digits):
                break
            codepoint = int(digits, 16)
            if 0xD800 <= codepoint <= 0xDBFF:
                low_prefix = value[index + 6:index + 8]
                low_digits = value[index + 8:index + 12]
                if (
                    low_prefix != "\\u"
                    or len(low_digits) != 4
                    or any(item not in "0123456789abcdefABCDEF" for item in low_digits)
                ):
                    # Do not publish half of an emoji while its low surrogate is
                    # still in flight in a later model delta.
                    break
                low = int(low_digits, 16)
                if not 0xDC00 <= low <= 0xDFFF:
                    break
                output.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00))
                index += 12
                continue
            if 0xDC00 <= codepoint <= 0xDFFF:
                break
            output.append(chr(codepoint))
            index += 6
            continue
        replacement = escapes.get(escaped)
        if replacement is None:
            break
        output.append(replacement)
        index += 2
    return "".join(output)


class _AgentEventStream:
    """Minimal SSE writer for a single synchronous Pi execution."""

    def __init__(self, handler: BaseHTTPRequestHandler):
        self.handler = handler
        self.raw_answer = ""
        self.visible_sent = ""
        self.started = False
        self.disconnected = False
        self.finished = False
        self._write_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()

    def start(self, run_id: str) -> None:
        self.handler.close_connection = False
        self.handler.send_response(200)
        self.handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.handler.send_header("Cache-Control", "no-store, no-cache, max-age=0")
        self.handler.send_header("Pragma", "no-cache")
        self.handler.send_header("X-Accel-Buffering", "no")
        # Give mobile clients a recovery handle before reading the first SSE
        # frame. A cellular/proxy transition can discard that first frame even
        # though the response headers arrived and the durable run continues.
        self.handler.send_header("X-Agent-Run-Id", run_id)
        # HTTP/1.1 requires an explicit body delimiter for a persistent stream.
        # Relying on connection-close made Render/Cloudflare surface a normal
        # SSE completion as URLError.networkConnectionLost (-1005) on iOS.
        self.handler.send_header("Transfer-Encoding", "chunked")
        request_id = getattr(self.handler, "request_id", "")
        if request_id:
            self.handler.send_header("X-Request-Id", request_id)
        self.handler.end_headers()
        self.started = True
        self.send("start", {"run_id": run_id})
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _heartbeat(self) -> None:
        while not self._heartbeat_stop.wait(10):
            if self.disconnected:
                return
            self._write(b": keep-alive\n\n")

    def _write(self, frame: bytes) -> None:
        if self.disconnected or self.finished:
            return
        try:
            with self._write_lock:
                self.handler.wfile.write(f"{len(frame):X}\r\n".encode("ascii"))
                self.handler.wfile.write(frame)
                self.handler.wfile.write(b"\r\n")
                self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.disconnected = True
            self._heartbeat_stop.set()

    def send(self, event: str, payload: dict) -> None:
        if self.disconnected:
            return
        # Keep the wire representation ASCII-only. Foundation's incremental
        # line decoder can otherwise surface NSCocoaErrorDomain 4864 when an
        # intermediary splits a multi-byte scalar across streamed buffers.
        # JSONDecoder restores the original Unicode text from the escapes.
        data = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        frame = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        self._write(frame)

    def model_delta(self, delta: str) -> None:
        self.raw_answer += delta
        visible = _partial_json_text(self.raw_answer)
        visible = _sentinel_visible_agent_text(visible or "")
        if visible is None or len(visible) <= len(self.visible_sent):
            return
        next_delta = visible[len(self.visible_sent):]
        self.visible_sent = visible
        self.send("delta", {"text": next_delta})

    def text_delta(self, delta: str) -> None:
        """Publish direct-chat text only after its explicit final boundary."""
        if not delta:
            return
        self.raw_answer += delta
        visible = _sentinel_visible_agent_text(self.raw_answer)
        if visible is None or len(visible) <= len(self.visible_sent):
            return
        next_delta = visible[len(self.visible_sent):]
        self.visible_sent = visible
        self.send("delta", {"text": next_delta})

    def error(self, status: int, message: str, code: str) -> None:
        self._heartbeat_stop.set()
        self.send("error", {"status": status, "message": message, "type": code})
        self.finish()

    def done(self, payload: dict) -> None:
        self._heartbeat_stop.set()
        self.send("done", payload)
        self.finish()

    def done_text(self, answer: str) -> None:
        """Finish with an ASCII payload that requires no JSON decoder."""
        self._heartbeat_stop.set()
        encoded = base64.b64encode(answer.encode("utf-8")).decode("ascii")
        self._write(f"event: done64\ndata: {encoded}\n\n".encode("ascii"))
        self.finish()

    def finish(self) -> None:
        if self.disconnected or self.finished:
            return
        try:
            with self._write_lock:
                self.handler.wfile.write(b"0\r\n\r\n")
                self.handler.wfile.flush()
                self.finished = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.disconnected = True
        finally:
            self._heartbeat_stop.set()


def error_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    message: str,
    code: str | None = None,
    *,
    include_request_id: bool = False,
) -> None:
    error = {"message": message, "type": code or "error"}
    if include_request_id:
        error["request_id"] = getattr(handler, "request_id", "")
    json_response(handler, status, {"error": error})


def empty_redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def html_response(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def connector_html_response(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    """HTML hardened for the unauthenticated, one-time connector setup form."""
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def plain_text_response(handler: BaseHTTPRequestHandler, status: int, value: str) -> None:
    body = value.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def account_deletion_html() -> bytes:
    return """<!doctype html><html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='robots' content='index,follow'><title>Eliminar cuenta | Agent Genia</title>
<style>body{font:16px system-ui;background:#f6f6f4;color:#171717;margin:0}main{max-width:680px;margin:8vh auto;padding:32px}
section{background:white;border:1px solid #ddd;border-radius:22px;padding:28px}h1{font-size:32px;margin-top:0}p,li{line-height:1.55;color:#555}
a{display:inline-block;background:#171717;color:white;padding:13px 18px;border-radius:12px;text-decoration:none;font-weight:650}</style></head>
<body><main><section><h1>Eliminar tu cuenta de Agent Genia</h1>
<p>En la app de escritorio, iOS o Android abre <strong>Cuenta → Eliminar cuenta y datos</strong>. Esa opción cancela una suscripción activa y elimina bots, sesiones, conectores y computadoras asociadas.</p>
<p>Si ya no tienes acceso a la app, solicita la eliminación desde el email usado en tu cuenta. Verificaremos que seas su titular antes de procesarla.</p>
<a href='mailto:privacy@agentgenia.com?subject=Eliminar%20mi%20cuenta%20de%20Agent%20Genia'>Solicitar eliminación</a>
<p>Solo conservaremos información cuando una obligación legal, fiscal o antifraude lo requiera.</p></section></main></body></html>""".encode("utf-8")


class _InternalAgentHandler:
    """In-process JSON handler that reuses the public agent-run contract.

    WhatsApp webhooks run asynchronously but must pass through the exact same
    validation, credit reservation, connector grants, and durable result path
    as desktop/mobile requests. The principal is an object reference set only
    by Backend code; it cannot be supplied over HTTP.
    """

    def __init__(self, user: dict, payload: dict):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.agentgenia_internal_user = user
        self.command = "POST"
        self.path = "/v1/agent/run"
        self.headers = {"content-length": str(len(body)), "content-type": "application/json"}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 0)
        self.request_id = "req_whatsapp_" + secrets.token_hex(8)
        self.close_connection = False
        self.status = 0
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name.lower()] = value

    def end_headers(self) -> None:
        return None

    def json(self) -> dict:
        try:
            value = json.loads(self.wfile.getvalue())
        except json.JSONDecodeError as exc:
            raise RuntimeError("La ejecución interna devolvió JSON inválido") from exc
        if not isinstance(value, dict):
            raise RuntimeError("La ejecución interna devolvió una respuesta inválida")
        return value


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._run_timing_lock = threading.Lock()
        self._run_timings: dict[str, dict[str, float]] = {}
        self._run_provider_lock = threading.Lock()
        self._run_providers: dict[str, dict[str, Any]] = {}
        self._run_principal_lock = threading.Lock()
        self._run_principals: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._pi_warm_lock = threading.Lock()
        self._pi_warm_events: dict[str, threading.Event] = {}
        self._conversation_locks_lock = threading.Lock()
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._local_rate_limit_lock = threading.Lock()
        self._local_rate_limits: dict[str, tuple[int, int]] = {}
        self._whatsapp_wake = threading.Event()
        validate_runtime_security(cfg)
        validate_pi_chrome_security(cfg.pi_chrome_isolation)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.secret_file.parent.mkdir(parents=True, exist_ok=True)
        self.store = create_store(database_url=cfg.database_url, db_path=cfg.db_path)
        self.credits = CreditService(self.store, cfg.credits)
        self.whatsapp = WhatsAppCloudAPI(
            WhatsAppConfig(
                enabled=cfg.whatsapp_enabled,
                verify_token=cfg.whatsapp_verify_token,
                app_secret=cfg.whatsapp_app_secret,
                access_token=cfg.whatsapp_access_token,
                phone_number_id=cfg.whatsapp_phone_number_id,
                public_number=cfg.whatsapp_public_number,
                graph_version=cfg.whatsapp_graph_version,
                link_ttl_seconds=cfg.whatsapp_link_ttl_seconds,
            )
        )
        try:
            billing_config = BillingConfig.from_values(
                enabled=cfg.stripe_enabled,
                live_mode=cfg.stripe_live_mode,
                secret_key=cfg.stripe_secret_key,
                webhook_secret=cfg.stripe_webhook_secret,
                basic_price_id=cfg.stripe_starter_price_id,
                pro_price_id=cfg.stripe_pro_price_id,
                business_price_id=cfg.stripe_business_price_id,
                success_url=cfg.stripe_success_url,
                cancel_url=cfg.stripe_cancel_url,
                portal_return_url=cfg.stripe_portal_return_url,
                webhook_tolerance_seconds=cfg.stripe_webhook_tolerance_seconds,
            )
        except ValueError as exc:
            raise UnsafeConfigurationError(f"Configuración de Stripe inválida: {exc}") from exc
        self.billing = BillingService(self.store, billing_config)
        self.google_auth = GoogleAccountAuth(
            store=self.store,
            client_id=cfg.google_oauth_client_id,
            client_secret=cfg.google_oauth_client_secret,
            redirect_uri=cfg.google_oauth_redirect_uri,
            access_ttl_seconds=cfg.account_access_ttl_seconds,
            refresh_ttl_seconds=cfg.account_refresh_ttl_seconds,
            attempt_ttl_seconds=cfg.account_auth_attempt_ttl_seconds,
            secret_env=cfg.wrapper_secret,
            secret_path=cfg.secret_file,
            key_version=cfg.wrapper_secret_version,
            secret_versions=cfg.wrapper_secret_versions,
            allow_secret_file=cfg.environment != "production",
        )
        self.apple_auth = AppleAccountAuth(
            store=self.store,
            session_issuer=self.google_auth,
            client_id=cfg.apple_client_id,
            team_id=cfg.apple_team_id,
            key_id=cfg.apple_key_id,
            private_key_base64=cfg.apple_private_key_base64,
            secret_env=cfg.wrapper_secret,
            secret_path=cfg.secret_file,
            key_version=cfg.wrapper_secret_version,
            secret_versions=cfg.wrapper_secret_versions,
            allow_secret_file=cfg.environment != "production",
        )
        self.connectors = ConnectorBroker(
            default_ttl_seconds=cfg.pi_connector_token_ttl_seconds,
            operation_store=self.store,
        )
        try:
            self.native_connector_gateway = NativeConnectorGateway(
                store=self.store,
                secret_env=cfg.wrapper_secret,
                secret_path=cfg.secret_file,
                public_base_url=cfg.connector_public_url,
                key_version=cfg.wrapper_secret_version,
                secret_versions=cfg.wrapper_secret_versions,
                allow_secret_file=cfg.environment != "production",
                attempt_ttl_seconds=cfg.composio_auth_attempt_ttl_seconds,
            )
            self.connector_gateway = ComposioConnectorGateway(
                api_key=cfg.composio_api_key,
                public_base_url=cfg.composio_public_url,
                auth_configs=parse_config_mapping(
                    cfg.composio_auth_configs_json,
                    name="COMPOSIO_AUTH_CONFIGS_JSON",
                ),
                direct_auth_configs=parse_config_mapping(
                    cfg.composio_direct_auth_configs_json,
                    name="COMPOSIO_DIRECT_AUTH_CONFIGS_JSON",
                ),
                toolkit_overrides=parse_config_mapping(
                    cfg.composio_toolkit_overrides_json,
                    name="COMPOSIO_TOOLKIT_OVERRIDES_JSON",
                ),
                attempt_ttl_seconds=cfg.composio_auth_attempt_ttl_seconds,
                native_gateway=self.native_connector_gateway,
                store=self.store,
            )
        except (RuntimeError, ValueError) as exc:
            raise UnsafeConfigurationError(f"Configuracion de conectores invalida: {exc}") from exc
        for connector_id in CONNECTOR_CATALOG:
            self.connectors.register_adapter(
                connector_id,
                ComposioConnectorAdapter(self.connector_gateway, connector_id),
            )
        try:
            self.computers = ComputerManager(
                store=self.store,
                config=ComputerConfig(
                    enabled=cfg.computers_enabled,
                    api_key=cfg.daytona_api_key,
                    api_url=cfg.daytona_api_url,
                    target=cfg.daytona_target,
                    snapshot=cfg.daytona_snapshot,
                    auto_stop_minutes=cfg.computer_auto_stop_minutes,
                    auto_archive_minutes=cfg.computer_auto_archive_minutes,
                    preview_ttl_seconds=cfg.computer_preview_ttl_seconds,
                    vnc_port=cfg.computer_vnc_port,
                    vnc_resolution=cfg.computer_vnc_resolution,
                    basic_limit=cfg.computer_basic_limit,
                    pro_limit=cfg.computer_pro_limit,
                ),
            )
        except (RuntimeError, ValueError) as exc:
            raise UnsafeConfigurationError(f"Configuración de computadoras inválida: {exc}") from exc
        self.pi = PiHarness(
            enabled=cfg.pi_enabled,
            binary=cfg.pi_bin,
            backend_url=cfg.pi_backend_url,
            runs_dir=cfg.pi_runs_dir,
            model=cfg.pi_model,
            thinking=cfg.pi_thinking,
            timeout_seconds=cfg.pi_timeout_seconds,
            max_concurrent=cfg.pi_max_concurrent,
            max_browser_concurrent=cfg.pi_browser_max_concurrent,
            max_prompt_chars=cfg.pi_max_prompt_chars,
            warm_sessions_enabled=cfg.pi_warm_sessions,
            session_idle_seconds=cfg.pi_session_idle_seconds,
            max_warm_sessions=cfg.pi_max_warm_sessions,
            supports_images=False,
            node_bin_dir=cfg.pi_node_bin_dir,
            connector_extension=cfg.pi_connector_extension,
            connector_broker_url=cfg.pi_backend_url,
            chrome_extension=cfg.pi_chrome_extension,
            chrome_auto_authorize=cfg.pi_chrome_auto_authorize,
            chrome_authorize_minutes=cfg.pi_chrome_authorize_minutes,
            chrome_binary=cfg.pi_chrome_bin,
            chrome_isolation=cfg.pi_chrome_isolation,
        )
        self._run_retention_once()
        threading.Thread(target=self._retention_loop, daemon=True).start()
        if self.whatsapp.config.configured:
            # PostgreSQL uses SKIP LOCKED and SQLite claims under BEGIN
            # IMMEDIATE, so independent workers cannot lease the same inbound
            # message. A slow agent no longer blocks every WhatsApp account.
            worker_count = max(2, min(4, cfg.pi_max_concurrent))
            for index in range(worker_count):
                threading.Thread(
                    target=self._whatsapp_worker_loop,
                    name=f"agentgenia-whatsapp-{index + 1}",
                    daemon=True,
                ).start()

    def _run_retention_once(self) -> None:
        try:
            self.store.purge_expired_ephemeral_data()
            # A hard process/container exit cannot run the normal settlement
            # path. Release reservations whose durable run token has expired
            # so a dead browser/Pi process cannot consume a concurrency slot
            # indefinitely after the service restarts.
            self.store.expire_stale_reservations()
            self.store.expire_past_due_entitlements()
            self.pi.purge_expired_runs()
        except Exception:
            logging.exception("Retention maintenance failed")

    def _retention_loop(self) -> None:
        while True:
            time.sleep(6 * 3600)
            self._run_retention_once()

    def _start_run_timing(
        self,
        run_id: str,
        started_at: float,
        initial: dict[str, float] | None = None,
    ) -> None:
        with self._run_timing_lock:
            self._run_timings[run_id] = {"_origin": started_at, **(initial or {})}

    def _mark_run_timing(self, run_id: str, name: str) -> None:
        with self._run_timing_lock:
            timing = self._run_timings.get(run_id)
            if timing is None or name in timing:
                return
            timing[name] = round((time.monotonic() - timing["_origin"]) * 1000, 3)

    def _start_upstream_call_timing(self, run_id: str) -> int:
        """Number and timestamp every model round within one Pi run."""
        with self._run_timing_lock:
            timing = self._run_timings.get(run_id)
            if timing is None:
                return 0
            call_index = int(timing.get("_upstream_call_count", 0)) + 1
            timing["_upstream_call_count"] = call_index
            timing[f"upstream_{call_index}_request_ms"] = round(
                (time.monotonic() - timing["_origin"]) * 1000, 3
            )
            return call_index

    def _run_timing_snapshot(self, run_id: str, *, pop: bool = False) -> dict[str, float]:
        with self._run_timing_lock:
            timing = self._run_timings.pop(run_id, None) if pop else self._run_timings.get(run_id)
            if timing is None:
                return {}
            return {key: value for key, value in timing.items() if not key.startswith("_")}

    def _run_provider(
        self,
        run_id: str,
        *,
        value: dict[str, Any] | None = None,
        pop: bool = False,
    ) -> dict[str, Any] | None:
        """Keep a decrypted provider credential only for one active run."""
        with self._run_provider_lock:
            if value is not None:
                self._run_providers[run_id] = value
                return value
            if pop:
                return self._run_providers.pop(run_id, None)
            return self._run_providers.get(run_id)

    def _run_principal(
        self,
        token: str,
        *,
        value: tuple[dict[str, Any], dict[str, Any]] | None = None,
        pop: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Cache one active run token inside the process that issued it.

        Pi calls the model proxy almost immediately after ``/v1/agent/run``.
        Re-querying Postgres to validate a token this process just created
        added another cross-region round trip to every model call. The durable
        database lookup remains the fallback for restarts and other replicas.
        """
        key = hash_agent_run_token(token)
        with self._run_principal_lock:
            if value is not None:
                self._run_principals[key] = value
                return value
            if pop:
                return self._run_principals.pop(key, None)
            return self._run_principals.get(key)

    def _forget_run_principals(self, user_id: str) -> None:
        with self._run_principal_lock:
            stale = [
                key for key, (user, _run) in self._run_principals.items()
                if user.get("id") == user_id
            ]
            for key in stale:
                self._run_principals.pop(key, None)

    # ---------- auth helpers ----------
    def encrypt_secret(self, plaintext: str, key_id: str) -> bytes:
        """Encrypt a connector or identity secret with the active master key."""
        return encrypt_api_key(
            plaintext,
            key_id,
            self.cfg.wrapper_secret,
            self.cfg.secret_file,
            key_version=self.cfg.wrapper_secret_version,
            secret_versions=self.cfg.wrapper_secret_versions,
            allow_secret_file=self.cfg.environment != "production",
        )

    def decrypt_secret(self, blob: bytes, key_id: str, key_version: int) -> str:
        return decrypt_api_key(
            blob,
            key_id,
            self.cfg.wrapper_secret,
            self.cfg.secret_file,
            key_version=key_version,
            secret_versions=self.cfg.wrapper_secret_versions,
            allow_secret_file=self.cfg.environment != "production",
        )

    def subscription_key(self, subscription: dict) -> str:
        """Decrypt and opportunistically rotate a server-owned provider key."""
        version = int(subscription.get("key_version") or 1)
        plaintext = self.decrypt_secret(
            bytes(subscription["api_key_enc"]), subscription["key_id"], version
        )
        if version != self.cfg.wrapper_secret_version:
            self.store.update_subscription_encryption(
                subscription["id"],
                self.encrypt_secret(plaintext, subscription["key_id"]),
                self.cfg.wrapper_secret_version,
            )
        return plaintext

    def model_provider(self, user: dict) -> dict:
        """Resolve the authenticated account's server-side model provider."""
        if user.get("model_provider_override") == "opencode":
            subscription_id = user.get("subscription_id")
            subscription = None
            if subscription_id and user.get("provider_subscription_id") == subscription_id:
                subscription = {
                    "id": user.get("provider_subscription_id"),
                    "api_key_enc": user.get("provider_api_key_enc"),
                    "key_id": user.get("provider_key_id"),
                    "key_version": user.get("provider_key_version"),
                    "status": user.get("provider_subscription_status"),
                    "assigned_user_id": user.get("provider_assigned_user_id"),
                }
            elif subscription_id:
                subscription = self.store.get_subscription(subscription_id)
            if (
                not subscription
                or subscription.get("status") != "assigned"
                or subscription.get("assigned_user_id") != user["id"]
            ):
                raise ModelProviderUnavailable(
                    "La credencial privada de OpenCode no está asignada"
                )
            try:
                api_key = self.subscription_key(subscription)
            except Exception as exc:
                logging.exception(
                    "No se pudo descifrar la credencial OpenCode de %s", user["id"]
                )
                raise ModelProviderUnavailable(
                    "No se pudo cargar la credencial privada de OpenCode"
                ) from exc
            return {
                "name": "opencode",
                "base_url": self.cfg.opencode_base_url,
                "api_key": api_key,
                "subscription_id": subscription["id"],
            }
        if not self.cfg.deepseek_api_key:
            raise ModelProviderUnavailable(
                "El proveedor de modelos predeterminado no está configurado"
            )
        return {
            "name": "deepseek",
            "base_url": self.cfg.deepseek_base_url,
            "api_key": self.cfg.deepseek_api_key,
            "subscription_id": None,
        }

    @staticmethod
    def unlimited_usage(user: dict) -> bool:
        return bool(int(user.get("unlimited_usage") or 0))

    def _consume_local_rate_limit(
        self, scope: str, *, limit: int, window_seconds: int
    ) -> bool:
        """Low-latency guard for trusted unlimited accounts.

        Durable credit/concurrency checks still run in Postgres. This small
        per-process bucket avoids removing abuse protection merely to skip a
        cross-region rate-limit write from the chat critical path.
        """
        window = int(time.time() // window_seconds)
        with self._local_rate_limit_lock:
            previous_window, count = self._local_rate_limits.get(scope, (window, 0))
            if previous_window != window:
                count = 0
            count += 1
            self._local_rate_limits[scope] = (window, count)
            if len(self._local_rate_limits) > 1_000:
                self._local_rate_limits = {
                    key: value for key, value in self._local_rate_limits.items()
                    if value[0] >= window - 1
                }
            return count <= limit

    def bearer(self, handler: BaseHTTPRequestHandler) -> str | None:
        auth = handler.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def require_user(self, handler: BaseHTTPRequestHandler) -> dict | None:
        internal_user = getattr(handler, "agentgenia_internal_user", None)
        if isinstance(internal_user, dict) and internal_user.get("account_status") == "active":
            return internal_user
        key = self.bearer(handler)
        if not key:
            error_response(handler, 401, "Falta Authorization: Bearer <api_key>", "unauthorized")
            return None
        # Token prefixes are disjoint. Avoid a guaranteed-miss Postgres query
        # for every mobile request before checking the Agent Genia session.
        if key.startswith("aga_"):
            user = self.google_auth.authenticate(key)
        else:
            user = self.store.get_user_by_api_key(key)
        if not user:
            error_response(handler, 401, "Sesión o API key del wrapper inválida", "unauthorized")
            return None
        return user

    def require_model_principal(
        self, handler: BaseHTTPRequestHandler
    ) -> tuple[dict, dict | None] | None:
        """Authenticate either a user session or a single-purpose run token."""
        key = self.bearer(handler)
        if not key:
            error_response(handler, 401, "Falta Authorization: Bearer <token>", "unauthorized")
            return None
        cached = self._run_principal(key) if key.startswith("agrn_") else None
        if cached is not None:
            return cached
        run = self.store.get_agent_run_by_token(key) if key.startswith("agrn_") else None
        if run:
            user = {
                "id": run.get("principal_user_id") or run["user_id"],
                "tier": run.get("principal_tier") or DEFAULT_TIER,
                "unlimited_usage": run.get("principal_unlimited_usage") or 0,
                "model_provider_override": run.get("principal_model_provider_override"),
                "subscription_id": run.get("principal_subscription_id"),
                "account_status": run.get("account_status"),
                "provider_subscription_id": run.get("provider_subscription_id"),
                "provider_api_key_enc": run.get("provider_api_key_enc"),
                "provider_key_id": run.get("provider_key_id"),
                "provider_key_version": run.get("provider_key_version"),
                "provider_subscription_status": run.get("provider_subscription_status"),
                "provider_assigned_user_id": run.get("provider_assigned_user_id"),
            }
            if user.get("account_status") == "active":
                return user, run
        if key.startswith("aga_"):
            user = self.google_auth.authenticate(key)
        else:
            user = self.store.get_user_by_api_key(key)
        if not user:
            error_response(handler, 401, "Sesión o token de ejecución inválido", "unauthorized")
            return None
        return user, None

    def ensure_trial(self, user: dict) -> None:
        self.credits.ensure_trial(user["id"])

    def credits_payload(self, user: dict, *, recent_limit: int = 20) -> dict:
        self.ensure_trial(user)
        tier = user.get("tier") or DEFAULT_TIER
        unlimited = self.unlimited_usage(user)
        plan = plan_for(tier)
        summary = self.store.credit_summary(user["id"], recent_limit=recent_limit)
        activity = [
            {
                "type": item["entry_type"],
                "run_id": item.get("run_id"),
                "credits": round(item["amount_milli"] / 1000, 3),
                "created_at": item["created_at"],
            }
            for item in summary["recent_activity"]
        ]
        billing = self.store.get_billing_status(user["id"])
        subscription = billing.get("subscription") or {}
        return {
            "mode": "unlimited" if unlimited else self.cfg.credits.mode,
            "unlimited": unlimited,
            "plan": {
                "tier": tier,
                "name": plan.label,
                "monthly_credits": plan.monthly_credit_milli // 1000,
                "five_hour_credits": plan.five_hour_credit_milli // 1000,
                "seven_day_credits": plan.seven_day_credit_milli // 1000,
                "max_concurrent_runs": (
                    self.cfg.pi_max_concurrent if unlimited else plan.max_concurrent_runs
                ),
            },
            "credits": {
                "available": credits_float(summary["available_milli"]),
                "reserved": credits_float(summary["reserved_milli"]),
                "total": credits_float(summary["total_milli"]),
            },
            "cycle": {"ends_at": subscription.get("current_period_end")},
            "recent_activity": activity,
        }

    def require_admin(self, handler: BaseHTTPRequestHandler) -> bool:
        key = self.bearer(handler)
        expected = self.cfg.admin_token or ""
        if not key or not hmac.compare_digest(key, expected):
            error_response(handler, 401, "Admin token invalido", "unauthorized")
            return False
        return True

    # ---------- signup ----------
    def handle_account_auth_start(self, handler: BaseHTTPRequestHandler) -> None:
        body = self.read_json(handler) or {}
        result = self.google_auth.start(
            device_id=body.get("device_id") if isinstance(body.get("device_id"), str) else "",
            app_version=body.get("app_version") if isinstance(body.get("app_version"), str) else "",
            remote_key=handler.client_address[0],
        )
        json_response(handler, 201, result)

    def handle_account_auth_status(self, handler: BaseHTTPRequestHandler) -> None:
        body = self.read_json(handler) or {}
        attempt_id = body.get("attempt_id") if isinstance(body.get("attempt_id"), str) else ""
        device_id = body.get("device_id") if isinstance(body.get("device_id"), str) else ""
        json_response(
            handler,
            200,
            self.google_auth.status(attempt_id=attempt_id, device_id=device_id),
        )

    def handle_account_auth_status_get(
        self,
        handler: BaseHTTPRequestHandler,
        attempt_id: str,
        query: dict[str, list[str]],
    ) -> None:
        device_id = (query.get("device_id") or [""])[0]
        json_response(
            handler,
            200,
            self.google_auth.status(attempt_id=attempt_id, device_id=device_id),
        )

    def handle_account_auth_callback(
        self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]
    ) -> None:
        self.google_auth.callback(query)
        empty_redirect(handler, "/v1/account-auth/complete")

    def handle_account_auth_refresh(self, handler: BaseHTTPRequestHandler) -> None:
        refresh_token = self.bearer(handler) or ""
        body = self.read_json(handler) or {}
        result = self.google_auth.refresh(
            refresh_token=refresh_token,
            device_id=body.get("device_id") if isinstance(body.get("device_id"), str) else "",
            remote_key=handler.client_address[0],
        )
        json_response(handler, 200, result)

    def handle_account_auth_logout(self, handler: BaseHTTPRequestHandler) -> None:
        access_token = self.bearer(handler) or ""
        if not access_token or not self.google_auth.logout(access_token):
            error_response(handler, 401, "La sesión ya no es válida", "unauthorized")
            return
        json_response(handler, 200, {"revoked": True})

    def handle_account_auth_apple(self, handler: BaseHTTPRequestHandler) -> None:
        body = self.read_json(handler) or {}
        required = {
            key: body.get(key) if isinstance(body.get(key), str) else ""
            for key in ("identity_token", "authorization_code", "nonce", "device_id")
        }
        result = self.apple_auth.login(
            **required,
            name=body.get("name") if isinstance(body.get("name"), str) else None,
            remote_key=handler.client_address[0],
        )
        json_response(handler, 200, result)

    def handle_account_delete(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        if body.get("confirmation") != "DELETE":
            error_response(
                handler,
                400,
                "Confirma la eliminación definitiva enviando confirmation=DELETE",
                "deletion_confirmation_required",
            )
            return

        subscription_canceled = self.billing.cancel_for_account_deletion(user)
        apple_revoked = self.apple_auth.revoke_user(user["id"])
        managed_deleted = self.connector_gateway.disconnect_all(user["id"])
        computer_cleanup = self.computers.delete_all(user_id=user["id"])
        if computer_cleanup["errors"]:
            raise ComputerError(
                502,
                "No fue posible eliminar todas las computadoras. Intenta nuevamente.",
                "computer_cleanup_failed",
            )
        ephemeral_grants = self.connectors.revoke_user(user["id"])
        pi_sessions_deleted = self.pi.forget_user(user["id"])
        self.google_auth.forget_user(user["id"])
        self._forget_run_principals(user["id"])
        result = self.store.delete_user_account(user["id"])
        json_response(
            handler,
            200,
            {
                "deleted": True,
                **result,
                "stripe_subscription_canceled": subscription_canceled,
                "apple_revoked": apple_revoked,
                "managed_connectors_deleted": managed_deleted,
                "ephemeral_grants_revoked": ephemeral_grants,
                "computers_deleted": computer_cleanup["deleted"],
                "pi_sessions_deleted": pi_sessions_deleted,
            },
        )

    # ---------- estado sincronizado de la aplicación ----------
    @staticmethod
    def _account_state_payload(row: dict | None) -> dict:
        if not row:
            return {
                "revision": 0,
                "state": empty_account_state(),
                "updated_at": None,
            }
        try:
            state = normalize_account_state(json.loads(row.get("state_json") or "{}"))
        except (json.JSONDecodeError, AccountStateError):
            logging.exception("Estado de cuenta inválido almacenado para %s", row.get("user_id"))
            state = empty_account_state()
        return {
            "revision": int(row.get("revision") or 0),
            "state": state,
            "updated_at": row.get("updated_at"),
        }

    def handle_account_state_get(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, self._account_state_payload(
            self.store.get_account_state(user["id"])
        ))

    def handle_account_state_save(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        base_revision = body.get("base_revision")
        device_id = body.get("device_id")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
            error_response(handler, 400, "base_revision inválida", "invalid_account_state")
            return
        if not isinstance(device_id, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            device_id,
        ):
            error_response(handler, 400, "device_id inválido", "invalid_account_state")
            return
        try:
            state = normalize_account_state(body.get("state"))
            state_json = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
            row = self.store.save_account_state(
                user_id=user["id"],
                base_revision=base_revision,
                state_json=state_json,
                device_hash=hashlib.sha256(
                    f"account-state-device|{device_id}".encode()
                ).hexdigest(),
            )
        except AccountStateError as exc:
            error_response(handler, 400, str(exc), "invalid_account_state")
            return
        except AccountStateConflict as exc:
            json_response(handler, 409, {
                "error": {
                    "message": "La cuenta cambió en otro dispositivo.",
                    "type": "account_state_conflict",
                },
                "current": self._account_state_payload(exc.current),
            })
            return
        json_response(handler, 200, self._account_state_payload(row))

    # ---------- canal oficial de WhatsApp ----------
    def handle_whatsapp_link_start(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        if not self.whatsapp.config.configured:
            error_response(handler, 503, "WhatsApp todavía no está habilitado", "whatsapp_unavailable")
            return
        if not self.store.consume_rate_limit(
            f"whatsapp-link:{user['id']}", limit=5, window_seconds=600
        ):
            error_response(handler, 429, "Espera antes de generar otro enlace", "rate_limit")
            return
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        code = f"AG-{raw[:4]}-{raw[4:]}"
        expires_at = time.time() + self.whatsapp.config.link_ttl_seconds
        self.store.create_whatsapp_link_code(
            user_id=user["id"], code=code, expires_at=expires_at
        )
        json_response(handler, 201, {
            "configured": True,
            "connected": False,
            "code": code,
            "expires_at": expires_at,
            "url": self.whatsapp.link_url(code),
        })

    def handle_whatsapp_status(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        link = self.store.get_whatsapp_link_for_user(user["id"])
        json_response(handler, 200, {
            "configured": self.whatsapp.config.configured,
            "connected": bool(link),
            "display_name": (link or {}).get("display_name") or "",
            "phone_hint": (
                f"••••{str(link['wa_user_id'])[-4:]}" if link else ""
            ),
            "active_bot_id": (link or {}).get("active_bot_id"),
        })

    def handle_whatsapp_unlink(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, {
            "disconnected": self.store.delete_whatsapp_link(user["id"])
        })

    def handle_whatsapp_webhook_verification(
        self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]
    ) -> None:
        mode = (query.get("hub.mode") or [""])[0]
        token = (query.get("hub.verify_token") or [""])[0]
        challenge = (query.get("hub.challenge") or [""])[0]
        if (
            not self.whatsapp.config.configured
            or mode != "subscribe"
            or not token
            or not hmac.compare_digest(token, self.whatsapp.config.verify_token)
            or not challenge
        ):
            plain_text_response(handler, 403, "Forbidden")
            return
        plain_text_response(handler, 200, challenge)

    def handle_whatsapp_webhook(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.whatsapp.config.configured:
            error_response(handler, 404, "Endpoint no disponible", "not_found")
            return
        body = self.read_body(handler, max_bytes=MAX_WHATSAPP_WEBHOOK_BODY)
        if not verify_webhook_signature(
            body,
            handler.headers.get("X-Hub-Signature-256", ""),
            self.whatsapp.config.app_secret,
        ):
            error_response(handler, 401, "Firma de webhook inválida", "invalid_signature")
            return
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestBodyError("Webhook de WhatsApp inválido") from exc
        accepted = 0
        for message in parse_webhook_messages(payload):
            if message["phone_number_id"] != self.whatsapp.config.phone_number_id:
                continue
            durable_payload = dict(message["payload"])
            durable_payload["_agentgenia_display_name"] = message["display_name"]
            if self.store.enqueue_whatsapp_message(
                message_id=message["message_id"],
                phone_number_id=message["phone_number_id"],
                wa_user_id=message["wa_user_id"],
                message_type=message["message_type"],
                text=message["text"],
                payload=durable_payload,
            ):
                accepted += 1
        if accepted:
            self._whatsapp_wake.set()
        # Meta needs an immediate 2xx. Durable processing happens after this.
        json_response(handler, 200, {"received": True, "accepted": accepted})

    def _whatsapp_worker_loop(self) -> None:
        while True:
            try:
                message = self.store.claim_whatsapp_message()
                if message is None:
                    self._whatsapp_wake.wait(2)
                    self._whatsapp_wake.clear()
                    continue
                try:
                    self._process_whatsapp_message(message)
                except WhatsAppError as exc:
                    logging.warning(
                        "WhatsApp transport failure message_id=%s error=%s",
                        message.get("message_id"),
                        exc,
                    )
                    self.store.retry_whatsapp_message(
                        message_id=message["message_id"], error=str(exc)
                    )
                except Exception as exc:
                    logging.exception(
                        "WhatsApp worker failure message_id=%s", message.get("message_id")
                    )
                    self.store.retry_whatsapp_message(
                        message_id=message["message_id"], error=type(exc).__name__
                    )
            except Exception:
                logging.exception("WhatsApp queue polling failed")
                self._whatsapp_wake.wait(5)
                self._whatsapp_wake.clear()

    def _deliver_whatsapp_answer(
        self,
        *,
        message_id: str,
        sender: str,
        answer: str,
        user_id: str | None,
    ) -> None:
        # Claim delivery durably before talking to Meta. Cloud API does not
        # accept a caller idempotency key, so a timeout after submission is an
        # uncertain one-shot delivery and must never be sent a second time.
        self.store.prepare_whatsapp_outbound(
            message_id=message_id, result_text=answer, user_id=user_id
        )
        outbound = self.whatsapp.send_text(
            to=sender, text=answer, reply_to_message_id=message_id
        )
        self.store.complete_whatsapp_message(
            message_id=message_id,
            status="succeeded",
            result_text=answer,
            outbound_message_id=outbound,
            user_id=user_id,
        )

    def _process_whatsapp_message(self, message: dict) -> None:
        message_id = str(message["message_id"])
        sender = str(message["wa_user_id"])
        phone_number_id = str(message["phone_number_id"])
        text = str(message.get("text") or "").strip()
        try:
            raw_payload = json.loads(message.get("payload_json") or "{}")
        except json.JSONDecodeError:
            raw_payload = {}
        display_name = (
            raw_payload.get("_agentgenia_display_name", "")
            if isinstance(raw_payload, dict)
            else ""
        )
        display_name = display_name if isinstance(display_name, str) else ""
        link = self.store.get_whatsapp_link_for_sender(
            wa_user_id=sender, phone_number_id=phone_number_id
        )
        code = extract_link_code(text)
        if code:
            if link:
                answer = "Este WhatsApp ya está vinculado con tu cuenta de Agentgenia. Ya puedes pedirme una tarea."
                self._deliver_whatsapp_answer(
                    message_id=message_id, sender=sender, answer=answer,
                    user_id=link["user_id"],
                )
                return
            link = self.store.consume_whatsapp_link_code(
                code=code,
                wa_user_id=sender,
                phone_number_id=phone_number_id,
                display_name=display_name,
            )
            if link:
                answer = (
                    "Listo: tu WhatsApp quedó vinculado con Agentgenia. "
                    "Puedes pedirme que cree un agente, elegir uno por su nombre o asignarle una tarea."
                )
            else:
                answer = "Ese código no es válido o ya expiró. Genera uno nuevo desde Agentgenia."
            self._deliver_whatsapp_answer(
                message_id=message_id, sender=sender, answer=answer,
                user_id=(link or {}).get("user_id"),
            )
            return
        if not link:
            # Do not turn the public number into an unauthenticated general AI
            # endpoint. Unlinked chatter is acknowledged by Meta but ignored.
            self.store.complete_whatsapp_message(
                message_id=message_id, status="ignored"
            )
            return
        user_id = str(link["user_id"])
        if message.get("message_type") not in {"text", "button", "interactive"}:
            answer = (
                "Por ahora este canal acepta mensajes de texto. "
                "Las notas de voz, imágenes y documentos llegarán en una siguiente actualización."
            )
            self._deliver_whatsapp_answer(
                message_id=message_id, sender=sender, answer=answer,
                user_id=user_id,
            )
            return
        if not text:
            self.store.complete_whatsapp_message(
                message_id=message_id, status="ignored", user_id=user_id
            )
            return

        state_payload = self._account_state_payload(self.store.get_account_state(user_id))
        state = state_payload["state"]
        active_bot = next(
            (
                item for item in state.get("bots", [])
                if item.get("id") == (link.get("active_bot_id") or state.get("activeBotId"))
            ),
            state.get("bots", [None])[0] if state.get("bots") else None,
        )
        connector_action = whatsapp_connector_command(text)
        if (
            connector_action == ("refresh", None)
            and not self.store.has_pending_connector_auth_attempt(user_id)
        ):
            # "Listo" is common conversational language. Only reserve it for
            # connector setup while this account actually has a live OAuth
            # attempt; otherwise it belongs to the active agent.
            connector_action = None
        if connector_action:
            action, connector_id = connector_action
            active_bot_id = str(active_bot.get("id") or "") if active_bot else None
            try:
                if action in {"list", "refresh"}:
                    snapshot = self.connector_gateway.snapshot(user_id)
                    connected_ids = [
                        str(item["connector_id"])
                        for item in snapshot
                        if item.get("connected") is True
                    ]
                    self._sync_whatsapp_connector_state(
                        user_id=user_id,
                        connected_ids=connected_ids,
                        active_bot_id=active_bot_id,
                    )
                    if action == "refresh" and connected_ids:
                        self.store.consume_connected_auth_attempts(user_id, connected_ids)
                    names = [CONNECTOR_CATALOG[item]["name"] for item in connected_ids]
                    if names:
                        prefix = "Listo. Tus conexiones activas son:" if action == "refresh" else "Tus conexiones activas son:"
                        answer = prefix + "\n• " + "\n• ".join(names)
                    elif action == "refresh":
                        answer = (
                            "Todavía no detecto una autorización terminada. Completa el enlace "
                            "del proveedor y vuelve a escribir “listo”."
                        )
                    else:
                        answer = (
                            "Todavía no tienes conexiones activas. Puedes decir, por ejemplo, "
                            "“Conecta Gmail”."
                        )
                elif action == "disconnect" and connector_id:
                    name = CONNECTOR_CATALOG[connector_id]["name"]
                    self.connector_gateway.disconnect(user_id, connector_id)
                    self._remove_whatsapp_connector_state(user_id, connector_id)
                    answer = f"Listo. Desconecté {name} de tu cuenta y de tus agentes."
                elif action == "connect" and connector_id:
                    name = CONNECTOR_CATALOG[connector_id]["name"]
                    status = self.connector_gateway.status(user_id, connector_id)
                    if status.get("connected") is True:
                        self._add_whatsapp_connector_state(
                            user_id=user_id,
                            connector_id=connector_id,
                            active_bot_id=active_bot_id,
                        )
                        answer = f"{name} ya está conectado y disponible para este agente."
                    else:
                        started = self.connector_gateway.start(user_id, connector_id)
                        authorize_url = str(started.get("authorize_url") or "")
                        answer = (
                            f"Para conectar {name}, abre este enlace seguro y autoriza tu cuenta:\n"
                            f"{authorize_url}\n\n"
                            "Cuando termines, vuelve aquí y escribe “listo”."
                        )
                else:  # pragma: no cover - defensa ante futuras acciones
                    raise RuntimeError("Acción de conector no soportada")
            except ConnectorBrokerError as exc:
                connector_name = (
                    CONNECTOR_CATALOG[connector_id]["name"] if connector_id else "esa conexión"
                )
                if exc.code == "connector_not_configured":
                    answer = f"{connector_name} todavía no está disponible para conexión."
                elif exc.code in {
                    "connector_rate_limit", "connector_unavailable",
                    "connector_provider_error", "connector_timeout",
                }:
                    # Let the durable queue retry provider outages instead of
                    # finalizing a transient failure as a successful turn.
                    raise WhatsAppError(f"Conector temporalmente no disponible: {exc.code}") from exc
                else:
                    answer = f"No pude gestionar {connector_name} en este momento. Inténtalo de nuevo."
            self._deliver_whatsapp_answer(
                message_id=message_id, sender=sender, answer=answer, user_id=user_id,
            )
            return
        if wants_bot_list(text):
            names = [bot["name"] for bot in state.get("bots", [])]
            answer = (
                "Tus agentes son:\n• " + "\n• ".join(names)
                if names
                else "Todavía no tienes agentes. Puedes decir: “Crea un agente para cotizaciones”."
            )
            self._deliver_whatsapp_answer(
                message_id=message_id, sender=sender, answer=answer, user_id=user_id,
            )
            return

        created = create_bot_from_request(text)
        if created:
            created["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentgenia:whatsapp:bot:{message_id}"))
            created["connectorIds"] = list(state.get("selectedConnectorIds", []))

            def add_created(current: dict) -> dict:
                if not any(bot.get("id") == created["id"] for bot in current["bots"]):
                    current["bots"].append(created)
                current["activeBotId"] = created["id"]
                current["onboardingCompleted"] = True
                return current

            self._mutate_whatsapp_state(user_id, add_created)
            self.store.update_whatsapp_active_bot(user_id=user_id, bot_id=created["id"])
            answer = (
                f"Creé el agente “{created['name']}” y quedó seleccionado. "
                "Ahora dime qué quieres que haga."
            )
            self._deliver_whatsapp_answer(
                message_id=message_id, sender=sender, answer=answer, user_id=user_id,
            )
            return

        selected = requested_bot(state, text)
        if selected:
            self.store.update_whatsapp_active_bot(user_id=user_id, bot_id=selected["id"])
            link["active_bot_id"] = selected["id"]
        bot = selected or next(
            (
                item for item in state.get("bots", [])
                if item.get("id") == (link.get("active_bot_id") or state.get("activeBotId"))
            ),
            None,
        )
        if bot is None and state.get("bots"):
            bot = state["bots"][0]
        if bot is None:
            bot = create_bot_from_request("crea un agente")
            if bot is None:
                raise RuntimeError("No se pudo crear el agente inicial")
            bot["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentgenia:whatsapp:default:{user_id}"))

            def add_default(current: dict) -> dict:
                if not any(item.get("id") == bot["id"] for item in current["bots"]):
                    current["bots"].append(bot)
                current["activeBotId"] = bot["id"]
                current["onboardingCompleted"] = True
                return current

            state = self._mutate_whatsapp_state(user_id, add_default)
            bot = next(item for item in state["bots"] if item["id"] == bot["id"])
            self.store.update_whatsapp_active_bot(user_id=user_id, bot_id=bot["id"])

        normalized = " ".join(text.casefold().split())
        switch_prefix = re.match(r"^(?:cambia|cambiar|usa|selecciona|habla con)\b", normalized)
        if selected and switch_prefix and len(normalized.split()) <= len(selected["name"].split()) + 3:
            answer = f"Listo. Ahora estás hablando con {selected['name']}."
            self._deliver_whatsapp_answer(
                message_id=message_id, sender=sender, answer=answer, user_id=user_id,
            )
            return

        # The bot assignment is the sole scope. Account-wide connected tools
        # are intentionally not inherited by every agent.
        connector_ids = list(dict.fromkeys(bot.get("connectorIds", [])))
        bot_for_run = {**bot, "connectorIds": connector_ids}
        prompt = build_whatsapp_bot_prompt(bot_for_run, text)
        user_message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentgenia:whatsapp:user:{message_id}"))
        assistant_message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentgenia:whatsapp:assistant:{message_id}"))
        self._append_whatsapp_state_message(
            user_id=user_id,
            bot_id=bot["id"],
            message={
                "id": user_message_id,
                "role": "user",
                "text": text,
                "createdAt": self._whatsapp_timestamp(),
            },
        )
        internal = _InternalAgentHandler(
            self.store.get_user_by_id(user_id) or link,
            {
                "prompt": prompt,
                "execution_mode": "auto",
                "chat_prompt": prompt,
                "user_message": text,
                "browser": False,
                "computer": False,
                "stream": False,
                "bot_id": bot["id"],
                "connector_ids": connector_ids,
                "idempotency_key": "whatsapp:" + hashlib.sha256(message_id.encode()).hexdigest(),
            },
        )
        try:
            self.handle_agent_run(internal)  # exact same credits/harness path as every app
        finally:
            # Internal WhatsApp dispatch does not pass through Handler._dispatch,
            # so release the per-bot conversation lease here as well.
            lock = getattr(internal, "agent_conversation_lock", None)
            internal.agent_conversation_lock = None
            if lock is not None:
                lock.release()
        response = internal.json()
        if internal.status != 200:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            if internal.status in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise WhatsAppError(
                    str(error.get("message") or f"agent_http_{internal.status}")
                )
            answer = (
                "No pude completar esa tarea: "
                + str(error.get("message") or "el agente no está disponible en este momento")
            )
        else:
            answer = parse_whatsapp_agent_answer(str(response.get("answer") or ""))
        self._append_whatsapp_state_message(
            user_id=user_id,
            bot_id=bot["id"],
            message={
                "id": assistant_message_id,
                "role": "assistant",
                "text": answer,
                "createdAt": self._whatsapp_timestamp(),
            },
        )
        self._deliver_whatsapp_answer(
            message_id=message_id, sender=sender, answer=answer, user_id=user_id,
        )

    @staticmethod
    def _whatsapp_timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _mutate_account_state(self, user_id: str, mutate, *, source: str) -> dict:
        device_hash = hashlib.sha256(
            f"account-state-device|{source}".encode()
        ).hexdigest()
        for _attempt in range(5):
            payload = self._account_state_payload(self.store.get_account_state(user_id))
            # JSON round-trip gives the mutator an isolated copy.
            state = json.loads(json.dumps(payload["state"]))
            next_state = normalize_account_state(mutate(state))
            if next_state == payload["state"]:
                return next_state
            try:
                saved = self.store.save_account_state(
                    user_id=user_id,
                    base_revision=payload["revision"],
                    state_json=json.dumps(next_state, separators=(",", ":"), ensure_ascii=False),
                    device_hash=device_hash,
                )
                return self._account_state_payload(saved)["state"]
            except AccountStateConflict:
                continue
        raise RuntimeError("La cuenta cambió demasiadas veces durante la sincronización")

    def _mutate_whatsapp_state(self, user_id: str, mutate) -> dict:
        return self._mutate_account_state(user_id, mutate, source="whatsapp-channel")

    def _add_connected_connectors_to_state(
        self, user_id: str, connector_ids: list[str] | tuple[str, ...]
    ) -> dict:
        connected = [
            item for item in dict.fromkeys(connector_ids) if item in CONNECTOR_CATALOG
        ]

        def add(current: dict) -> dict:
            selected = list(current.get("selectedConnectorIds", []))
            current["selectedConnectorIds"] = list(dict.fromkeys(selected + connected))
            return current

        return self._mutate_account_state(
            user_id, add, source="connector-reconciliation"
        )

    def _replace_connected_connectors_in_state(
        self, user_id: str, connector_ids: list[str] | tuple[str, ...]
    ) -> dict:
        """Reconcile account and bot selections from authoritative connections."""
        connected = list(dict.fromkeys(
            item for item in connector_ids if item in CONNECTOR_CATALOG
        ))
        connected_set = set(connected)

        def replace(current: dict) -> dict:
            current["selectedConnectorIds"] = connected
            changed_at = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            for bot in current.get("bots", []):
                previous = list(bot.get("connectorIds", []))
                reconciled = [
                    item for item in bot.get("connectorIds", [])
                    if item in connected_set
                ]
                bot["connectorIds"] = reconciled
                if reconciled != previous:
                    # A server-confirmed revocation must be newer than an
                    # offline device snapshot, otherwise per-bot merge rules
                    # can resurrect the removed connector later.
                    bot["updatedAt"] = changed_at
            return current

        return self._mutate_account_state(
            user_id, replace, source="connector-reconciliation"
        )

    def _assigned_connector_ids(self, user_id: str, bot_id: str | None) -> tuple[str, ...]:
        """Return only connector ids assigned to this bot in server state."""
        if not bot_id:
            return ()
        payload = self._account_state_payload(self.store.get_account_state(user_id))
        bot = next(
            (item for item in payload["state"].get("bots", []) if item.get("id") == bot_id),
            None,
        )
        if not isinstance(bot, dict):
            return ()
        return tuple(
            item for item in bot.get("connectorIds", []) if item in CONNECTOR_CATALOG
        )

    def _conversation_lock(self, user_id: str, bot_id: str | None) -> threading.Lock | None:
        if not bot_id:
            return None
        key = hashlib.sha256(f"{user_id}\0{bot_id}".encode()).hexdigest()
        with self._conversation_locks_lock:
            return self._conversation_locks.setdefault(key, threading.Lock())

    def _append_whatsapp_state_message(
        self, *, user_id: str, bot_id: str, message: dict
    ) -> None:
        def append(current: dict) -> dict:
            for bot in current["bots"]:
                if bot.get("id") != bot_id:
                    continue
                messages = bot.get("messages") if isinstance(bot.get("messages"), list) else []
                if not any(item.get("id") == message["id"] for item in messages):
                    bot["messages"] = (messages + [message])[-200:]
                current["activeBotId"] = bot_id
                return current
            raise RuntimeError("El agente de WhatsApp ya no existe")
        self._mutate_whatsapp_state(user_id, append)

    def _add_whatsapp_connector_state(
        self, *, user_id: str, connector_id: str, active_bot_id: str | None
    ) -> None:
        def add(current: dict) -> dict:
            selected = list(current.get("selectedConnectorIds", []))
            if connector_id not in selected:
                selected.append(connector_id)
            current["selectedConnectorIds"] = selected
            for bot in current.get("bots", []):
                if active_bot_id and bot.get("id") == active_bot_id:
                    connector_ids = list(bot.get("connectorIds", []))
                    if connector_id not in connector_ids:
                        connector_ids.append(connector_id)
                    bot["connectorIds"] = connector_ids
                    bot["updatedAt"] = self._whatsapp_timestamp()
                    break
            return current
        self._mutate_whatsapp_state(user_id, add)

    def _sync_whatsapp_connector_state(
        self, *, user_id: str, connected_ids: list[str], active_bot_id: str | None
    ) -> None:
        connected = list(dict.fromkeys(
            item for item in connected_ids if item in CONNECTOR_CATALOG
        ))

        def sync(current: dict) -> dict:
            current["selectedConnectorIds"] = connected
            for bot in current.get("bots", []):
                if active_bot_id and bot.get("id") == active_bot_id:
                    bot["connectorIds"] = connected
                    break
            return current
        self._mutate_whatsapp_state(user_id, sync)

    def _remove_whatsapp_connector_state(
        self, user_id: str, connector_id: str
    ) -> None:
        def remove(current: dict) -> dict:
            current["selectedConnectorIds"] = [
                item for item in current.get("selectedConnectorIds", [])
                if item != connector_id
            ]
            for bot in current.get("bots", []):
                before = list(bot.get("connectorIds", []))
                bot["connectorIds"] = [
                    item for item in before
                    if item != connector_id
                ]
                if bot["connectorIds"] != before:
                    bot["updatedAt"] = self._whatsapp_timestamp()
            return current
        self._mutate_whatsapp_state(user_id, remove)

    # ---------- connector accounts ----------
    def handle_connectors_snapshot(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        snapshot = self.connector_gateway.snapshot(user["id"])
        connected_ids = [
            item["connector_id"] for item in snapshot if item.get("connected") is True
        ]
        self._replace_connected_connectors_in_state(user["id"], connected_ids)
        json_response(
            handler,
            200,
            {"connectors": snapshot},
        )

    def handle_connector_status_public(
        self, handler: BaseHTTPRequestHandler, connector_id: str
    ) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, self.connector_gateway.status(user["id"], connector_id))

    def handle_connector_start(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        if not self.store.consume_rate_limit(
            f"connector-start:{user['id']}", limit=20, window_seconds=60
        ):
            error_response(handler, 429, "Demasiadas conexiones nuevas", "rate_limit")
            return
        body = self.read_json(handler) or {}
        connector_id = body.get("connector_id")
        if not isinstance(connector_id, str):
            raise ConnectorBrokerError(400, "Falta connector_id", "bad_connector")
        json_response(handler, 201, self.connector_gateway.start(user["id"], connector_id))

    def handle_connector_auth_status(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        attempt_id = body.get("attempt_id") if isinstance(body.get("attempt_id"), str) else ""
        if not attempt_id:
            raise ConnectorBrokerError(400, "Falta attempt_id", "bad_connector")
        result = self.connector_gateway.poll(user["id"], attempt_id)
        session = result.get("session") if isinstance(result, dict) else None
        connector_id = session.get("connector_id") if isinstance(session, dict) else None
        if result.get("status") == "complete" and isinstance(connector_id, str):
            self._add_connected_connectors_to_state(user["id"], [connector_id])
        json_response(handler, 200, result)

    def handle_connector_disconnect(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        connector_id = body.get("connector_id")
        if not isinstance(connector_id, str):
            raise ConnectorBrokerError(400, "Falta connector_id", "bad_connector")
        result = self.connector_gateway.disconnect(user["id"], connector_id)
        self._remove_whatsapp_connector_state(user["id"], connector_id)
        json_response(handler, 200, result)

    def handle_native_connector_setup(
        self, handler: BaseHTTPRequestHandler, attempt_id: str
    ) -> None:
        connector_html_response(
            handler,
            200,
            self.native_connector_gateway.setup_html(attempt_id),
        )

    def handle_native_connector_submit(
        self, handler: BaseHTTPRequestHandler, attempt_id: str
    ) -> None:
        content_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise ConnectorBrokerError(415, "Formulario no compatible", "bad_connector_credentials")
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ConnectorBrokerError(400, "Content-Length invalido", "bad_connector_credentials") from exc
        if length <= 0 or length > 64 * 1024:
            raise ConnectorBrokerError(413, "Formulario demasiado grande", "bad_connector_credentials")
        try:
            raw = handler.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConnectorBrokerError(400, "Formulario invalido", "bad_connector_credentials") from exc
        parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=20)
        values = {key: items[-1] for key, items in parsed.items() if items}
        connector_html_response(
            handler,
            200,
            self.native_connector_gateway.submit(attempt_id, values),
        )

    def handle_signup(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.cfg.public_legacy_signup_enabled:
            error_response(handler, 404, "Endpoint no disponible", "not_found")
            return
        self._check_signup_rate(handler.client_address[0])
        body = self.read_json(handler) or {}
        name = body.get("name")
        email = body.get("email")
        # El cliente nunca decide su tier. Los upgrades pagados solo pueden
        # ocurrir mediante un webhook de pago verificado o el endpoint admin.
        tier = DEFAULT_TIER
        api_key = secrets.token_hex(32)
        user = self.store.create_user(api_key, name, email, tier=tier)
        credits = self.credits_payload(user, recent_limit=0)
        json_response(handler, 201, {
            "api_key": api_key,
            "user_id": user["id"],
            "name": user["name"],
            "tier": tier,
            "tier_label": tier_label(tier),
            "limits": effective_limits(tier),
            "credits": credits["credits"],
            "note": (
                "Guarda el api_key; no se puede volver a mostrar. La cuenta inicia en free; "
                "Starter/Pro/Business se activan exclusivamente después de verificar el pago."
            ),
        })

    def _check_signup_rate(self, remote_key: str) -> None:
        if not self.store.consume_rate_limit(f"signup:{remote_key}", limit=20, window_seconds=60):
            raise GoogleAuthError(
                "Demasiadas altas. Espera un minuto.", status=429, code="rate_limit"
            )

    def readiness(self) -> dict:
        database = self.store.health()
        pi = self.pi.status()
        pi.pop("binary", None)
        memory_limit = runtime_memory_limit_mb()
        browser_resource_ready = bool(
            memory_limit is None
            or self.cfg.pi_browser_min_memory_mb == 0
            or memory_limit >= self.cfg.pi_browser_min_memory_mb
        )
        pi["browser_resource_ready"] = browser_resource_ready
        connectors = self.connector_gateway.health()
        computers = self.computers.health()
        checks = {
            "database": bool(database.get("ready")),
            "google_auth": self.google_auth.configured,
            "apple_auth": self.apple_auth.configured,
            "stripe": self.billing.configured,
            # A third-party catalog entry can be temporarily unavailable (or
            # require customer-specific OAuth) without making every other
            # connector unusable. Readiness therefore verifies the private
            # gateway and at least one executable connector; catalog
            # completeness remains visible in the detailed health payload.
            "connectors": bool(connectors.get("configured"))
            and int(connectors.get("available_connectors") or 0) > 0,
            # Persistent computers are an optional capability. When explicitly
            # enabled, ComputerConfig already fails closed without a provider
            # key and readiness still requires the provider to be configured.
            "computers": not self.cfg.computers_enabled
            or bool(computers.get("configured")),
            "pi": bool(pi.get("enabled"))
            and bool(pi.get("available"))
            and bool(pi.get("node_available"))
            and bool(pi.get("connectors_available")),
            "pi_chrome": bool(pi.get("browser_available"))
            and bool(pi.get("browser_auto_authorize"))
            and pi.get("browser_isolation") == CHROME_ISOLATION_PER_RUN
            and browser_resource_ready,
            "model_provider": bool(self.cfg.deepseek_api_key),
            "whatsapp": (
                not self.cfg.whatsapp_enabled or self.whatsapp.config.configured
            ),
        }
        ready = all(checks.values()) if self.cfg.environment == "production" else checks["database"]
        return {
            "ready": ready,
            "checks": checks,
            "database": database,
            "pi": pi,
            "connectors": connectors,
            "computers": computers,
            "model_provider": {
                "configured": bool(self.cfg.deepseek_api_key),
                "base_url": self.cfg.deepseek_base_url,
            },
            "whatsapp": {
                "enabled": self.cfg.whatsapp_enabled,
                "configured": self.whatsapp.config.configured,
            },
        }

    # ---------- billing ----------
    def handle_billing_status(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        payload = self.billing.status(user)
        payload["credits"] = self.credits_payload(user, recent_limit=5)["credits"]
        json_response(handler, 200, payload)

    def handle_billing_checkout(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        tier = body.get("tier")
        if not isinstance(tier, str):
            raise BillingError("Falta el plan solicitado", code="invalid_plan")
        json_response(handler, 201, self.billing.create_checkout(user, tier))

    def handle_billing_portal(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 201, self.billing.create_portal(user))

    def handle_billing_webhook(self, handler: BaseHTTPRequestHandler) -> None:
        signature = handler.headers.get("Stripe-Signature", "")
        payload = self.read_body(handler, max_bytes=MAX_STRIPE_WEBHOOK_BODY)
        json_response(handler, 200, self.billing.process_webhook(payload, signature))

    # ---------- proxy ----------
    def handle_proxy(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        # Chat completions may stream and authentication/budget checks run before
        # reading the body. Never reuse that HTTP/1.1 connection: otherwise an
        # early rejection can leave JSON bytes queued as a bogus next request.
        if handler.command == "POST":
            handler.close_connection = True
        principal = self.require_model_principal(handler)
        if not principal:
            return
        user, run = principal
        run_id = run["id"] if run else None
        upstream_call_index = 0
        if run_id:
            self._mark_run_timing(run_id, "proxy_received_ms")
            upstream_call_index = self._start_upstream_call_timing(run_id)
        unlimited = self.unlimited_usage(user)
        if run and path != "/chat/completions":
            error_response(
                handler,
                403,
                "El token de ejecución solo puede llamar chat/completions",
                "run_token_scope",
            )
            return
        tier = user.get("tier") or DEFAULT_TIER
        if not run:
            self.ensure_trial(user)
        if path == "/chat/completions":
            if run and not unlimited:
                consumed = self.credits.billable_milli(
                    self.store.agent_run_cost_microusd(run["id"]),
                    int(run.get("extra_cost_microusd") or 0),
                )
                if consumed >= int(run["max_credit_milli"]):
                    error_response(
                        handler, 402, "La ejecución alcanzó su máximo autorizado",
                        "run_budget_exhausted",
                    )
                    return
            elif self.cfg.credits.mode == "enforce" and not unlimited:
                error_response(
                    handler, 403,
                    "Las llamadas al modelo requieren un token efímero de ejecución",
                    "run_token_required",
                )
                return
            elif not unlimited and not has_model_access(tier):
                summary = self.store.credit_summary(user["id"], recent_limit=0)
                if summary["available_milli"] <= 0:
                    error_response(
                        handler, 402, "No quedan créditos disponibles", "insufficient_credits"
                    )
                    return
        provider = self._run_provider(run_id) if run_id else None
        if provider is None:
            try:
                provider = self.model_provider(user)
            except ModelProviderUnavailable as exc:
                error_response(handler, 503, str(exc), "model_unavailable")
                return
        body = self.read_body(handler)

        # DeepSeek's user identifier is security-sensitive provider metadata.
        # Never let a client choose or share another account's cache/scheduling bucket.
        if body and path == "/chat/completions":
            try:
                provider_body = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestBodyError("JSON inválido para el proveedor") from exc
            if not isinstance(provider_body, dict):
                raise RequestBodyError("El body del proveedor debe ser un objeto JSON")
            secret = (self.cfg.wrapper_secret or "development-only").encode("utf-8")
            provider_body["user_id"] = hmac.new(
                secret,
                str(user["id"]).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            body = json.dumps(
                provider_body,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")

        ua = handler.headers.get("user-agent", "")
        headers = {
            "content-type": handler.headers.get("content-type", "application/json"),
            "accept": handler.headers.get("accept", "application/json"),
            "user-agent": ua if (ua.startswith("Mozilla") or ua.startswith("curl")) else DEFAULT_UA,
        }
        if handler.headers.get("stream", "").lower() in ("true", "1"):
            headers["stream"] = "true"
        if body:
            try:
                data = json.loads(body)
                if isinstance(data, dict) and data.get("stream") is True:
                    headers["stream"] = "true"
            except Exception:
                pass

        stream_state = {"started": False}

        def on_headers(status: int, out_headers: dict) -> None:
            if run_id:
                self._mark_run_timing(run_id, "upstream_headers_ms")
                self._mark_run_timing(
                    run_id, f"upstream_{upstream_call_index}_headers_ms"
                )
            stream_state["started"] = True
            try:
                handler.send_response(status)
                for k, v in out_headers.items():
                    if k.lower() in ("content-length", "transfer-encoding", "cache-control", "pragma"):
                        continue
                    handler.send_header(k, v)
                handler.send_header("Cache-Control", "no-store, max-age=0")
                handler.send_header("Pragma", "no-cache")
                if handler.close_connection:
                    handler.send_header("Connection", "close")
                handler.send_header("X-Request-Id", handler.request_id)
                handler.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def on_chunk(chunk: bytes) -> None:
            if run_id and chunk.strip():
                self._mark_run_timing(run_id, "upstream_first_byte_ms")
                self._mark_run_timing(
                    run_id, f"upstream_{upstream_call_index}_first_byte_ms"
                )
                self._inspect_upstream_delta_timing(
                    run_id, chunk, upstream_call_index=upstream_call_index
                )
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        if run_id:
            self._mark_run_timing(run_id, "upstream_request_ms")
        status, out_headers, out_body, usage = proxy_request(
            handler.command,
            provider["base_url"],
            path,
            headers,
            body,
            provider["api_key"],
            on_chunk=on_chunk, on_headers=on_headers,
        )
        if run_id:
            self._mark_run_timing(run_id, "upstream_complete_ms")
            self._mark_run_timing(
                run_id, f"upstream_{upstream_call_index}_complete_ms"
            )
            logging.info(
                "model timing run_id=%s provider=%s timings=%s",
                run_id,
                provider["name"],
                json.dumps(self._run_timing_snapshot(run_id), separators=(",", ":")),
            )
        self.record(
            user,
            provider,
            path,
            status,
            usage,
            run_id=run["id"] if run else None,
        )

        if stream_state["started"]:
            handler.close_connection = True
            return

        # No-stream: responder con el body crudo del upstream
        raw = out_body or b""
        try:
            handler.send_response(status)
            for k, v in out_headers.items():
                if k.lower() in ("content-length", "transfer-encoding", "cache-control", "pragma"):
                    continue
                handler.send_header(k, v)
            handler.send_header("Cache-Control", "no-store, max-age=0")
            handler.send_header("Pragma", "no-cache")
            if handler.close_connection:
                handler.send_header("Connection", "close")
            handler.send_header("X-Request-Id", handler.request_id)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            if raw:
                handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _inspect_upstream_delta_timing(
        self, run_id: str, chunk: bytes, *, upstream_call_index: int = 0
    ) -> None:
        line = chunk.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return
        choices = event.get("choices") if isinstance(event, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            return
        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self._mark_run_timing(run_id, "upstream_first_reasoning_ms")
            if upstream_call_index:
                self._mark_run_timing(
                    run_id, f"upstream_{upstream_call_index}_first_reasoning_ms"
                )
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._mark_run_timing(run_id, "upstream_first_content_ms")
            if upstream_call_index:
                self._mark_run_timing(
                    run_id, f"upstream_{upstream_call_index}_first_content_ms"
                )
        if delta.get("tool_calls"):
            self._mark_run_timing(run_id, "upstream_first_tool_call_ms")
            if upstream_call_index:
                self._mark_run_timing(
                    run_id, f"upstream_{upstream_call_index}_first_tool_call_ms"
                )

    def record(
        self,
        user: dict,
        provider: dict,
        path: str,
        status: int,
        usage,
        *,
        run_id: str | None = None,
    ) -> None:
        model = usage.model
        estimator = (
            estimate_opencode_cost_microusd
            if provider["name"] == "opencode"
            else estimate_cost_microusd
        )
        cost_microusd = estimator(
            model, usage.input_tokens, usage.output_tokens,
            usage.cached_read, usage.cached_write,
        ) if usage.any() else 0
        cost = cost_microusd / 1_000_000
        self.store.record_usage(
            user["id"], provider["subscription_id"], model, path,
            usage.input_tokens if usage.any() else None,
            usage.output_tokens if usage.any() else None,
            usage.cached_read if usage.any() else None,
            usage.cached_write if usage.any() else None,
            cost, status, run_id=run_id, estimated_cost_microusd=cost_microusd,
        )

    # ---------- Pi agent harness ----------
    def handle_agent_status(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        status = self.pi.status()
        status.pop("binary", None)  # no exponer rutas internas del servidor
        status["model_provider"] = (
            "opencode" if user.get("model_provider_override") == "opencode" else "deepseek"
        )
        memory_limit = runtime_memory_limit_mb()
        status["browser_resource_ready"] = bool(
            memory_limit is None
            or self.cfg.pi_browser_min_memory_mb == 0
            or memory_limit >= self.cfg.pi_browser_min_memory_mb
        )
        json_response(handler, 200, status)

    def handle_agent_run_status(
        self, handler: BaseHTTPRequestHandler, run_id: str
    ) -> None:
        user = self.require_user(handler)
        if not user:
            return
        run = self.store.get_agent_run_for_user(run_id, user["id"])
        if not run:
            error_response(handler, 404, "Ejecución no encontrada", "run_not_found")
            return
        result = None
        if run.get("result_json"):
            try:
                result = json.loads(run["result_json"])
            except (TypeError, json.JSONDecodeError):
                logging.exception("Invalid durable agent result run_id=%s", run_id)
        json_response(handler, 200, {
            "run_id": run_id,
            "status": run.get("status"),
            "error_code": run.get("error_code"),
            "created_at": run.get("created_at"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "result": result,
        })

    def handle_agent_run_recover(self, handler: BaseHTTPRequestHandler) -> None:
        """Resolve an uncertain transport result using the client's stable key."""
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        idempotency_key = body.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key.strip()) <= 200:
            error_response(handler, 400, "idempotency_key no es válida", "bad_idempotency_key")
            return
        run = self.store.get_agent_run_by_idempotency(user["id"], idempotency_key.strip())
        if not run:
            error_response(handler, 404, "Ejecución no encontrada", "run_not_found")
            return
        result = None
        if run.get("result_json"):
            try:
                result = json.loads(run["result_json"])
            except (TypeError, json.JSONDecodeError):
                logging.exception("Invalid durable agent result run_id=%s", run["id"])
        json_response(handler, 200, {
            "run_id": run["id"],
            "status": run.get("status"),
            "error_code": run.get("error_code"),
            "result": result,
        })

    @staticmethod
    def _direct_chat_allowed(
        execution_mode: str,
        user_message: str,
        *,
        browser: bool,
        computer: bool,
        conversation_context: str = "",
    ) -> bool:
        if execution_mode == "agent" or browser or computer:
            return False
        if execution_mode == "chat":
            return True
        message = user_message.strip().lower()
        if not message:
            return False
        # Stay conservative: anything that sounds like an external mutation,
        # lookup or computer action keeps the full Pi/tool path. Ordinary
        # conversation, explanation and drafting take the direct path.
        tool_intent = re.compile(
            r"(?:https?://|\b(?:abre|busca|consulta|revisa|revisar|checa|checar|"
            r"comprueba|verifica|mira|mu[eé]strame|dime\s+(?:qu[eé]|cu[aá]l(?:es)?|si)|lee|descarga|sube|"
            r"env[ií]a|manda|publica|actualiza|modifica|elimina|borra|crea(?:r)?\s+(?:un\s+)?"
            r"(?:issue|ticket|evento|archivo|carpeta|tarea|documento)|agenda|programa|"
            r"reserva|compra|conecta|instala|ejecuta|corre|inicia\s+sesi[oó]n|"
            r"open|search|look\s+up|check|review|read|download|upload|send|post|"
            r"update|edit|delete|remove|schedule|book|buy|connect|install|run|"
            r"log\s+in|create\s+(?:an?\s+)?(?:issue|ticket|event|file|folder|task|document)|"
            r"qu[eé]\s+tengo\s+(?:hoy|ma[nñ]ana|esta\s+semana)|cu[aá]ndo\s+es\s+(?:mi|la)|"
            r"pr[oó]xim[oa]\s+(?:reuni[oó]n|cita|evento)|mi\s+agenda|"
            r"gmail|correo(?:s)?|email(?:s)?|bandeja|calendar|calendario|agenda|"
            r"slack|notion|github|jira|drive|dropbox|shopify|stripe|crm|salesforce|"
            r"computadora|navegador|browser|archivo|terminal|shell)\b)",
            re.IGNORECASE,
        )
        if tool_intent.search(message):
            return False
        # A short follow-up such as "no necesita hora final" still belongs to
        # the pending Calendar/tool turn even though it no longer repeats the
        # provider name. The client supplies only recent conversation here;
        # generic bot instructions are deliberately excluded by callers.
        context = conversation_context.strip().lower()
        return not context or tool_intent.search(context) is None

    def _run_direct_chat(
        self,
        *,
        run_id: str,
        user: dict,
        provider: dict,
        prompt: str,
        event_stream: _AgentEventStream | None,
    ) -> PiRunResult:
        started = time.monotonic()
        answer_parts: list[str] = []
        pending = ""
        finish_reason = ""
        structured_answer = _expects_agent_envelope(prompt)
        secret = (self.cfg.wrapper_secret or "development-only").encode("utf-8")
        provider_user_id = hmac.new(
            secret,
            str(user["id"]).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        request_payload: dict[str, object] = {
            "model": self.cfg.pi_model,
            "messages": [
                {"role": "system", "content": AGENT_RESPONSE_STYLE_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            # Product chat is intentionally concise. A smaller ceiling avoids
            # runaway mobile replies while leaving ample room for a question
            # widget or a useful draft.
            "max_tokens": 1024,
            "user_id": provider_user_id,
        }
        if self.cfg.pi_model.startswith("deepseek-"):
            # DeepSeek V4 enables thinking by default. Direct chat is the
            # latency-sensitive path for conversation and drafting, so do not
            # spend a hidden high-effort reasoning pass before the first
            # visible token. This applies to both the first-party API and the
            # OpenCode Zen OpenAI-compatible endpoint. Tool/computer work still
            # uses Pi with thinking.
            request_payload["thinking"] = {"type": "disabled"}
        request_body = json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        def process_line(line: str) -> None:
            nonlocal finish_reason
            line = line.strip()
            if not line.startswith("data:"):
                return
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return
            reason = choices[0].get("finish_reason")
            if isinstance(reason, str) and reason:
                finish_reason = reason
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                return
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self._mark_run_timing(run_id, "upstream_first_reasoning_ms")
            content = delta.get("content")
            if not isinstance(content, str) or not content:
                return
            self._mark_run_timing(run_id, "upstream_first_content_ms")
            answer_parts.append(content)
            if event_stream:
                self._mark_run_timing(run_id, "first_visible_delta_ms")
                if structured_answer:
                    event_stream.model_delta(content)
                else:
                    event_stream.text_delta(content)

        def on_headers(_status: int, _headers: dict) -> None:
            self._mark_run_timing(run_id, "upstream_headers_ms")

        def on_chunk(chunk: bytes) -> None:
            nonlocal pending
            if chunk.strip():
                self._mark_run_timing(run_id, "upstream_first_byte_ms")
            pending += chunk.decode("utf-8", errors="replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                process_line(line.rstrip("\r"))

        self._mark_run_timing(run_id, "direct_dispatch_ms")
        self._mark_run_timing(run_id, "upstream_request_ms")
        status, _headers, response_body, usage = proxy_request(
            "POST",
            provider["base_url"],
            "/chat/completions",
            {
                "content-type": "application/json",
                "accept": "text/event-stream",
                "stream": "true",
                "user-agent": DEFAULT_UA,
            },
            request_body,
            provider["api_key"],
            on_chunk=on_chunk,
            on_headers=on_headers,
            # Conversation should fail recoverably instead of leaving a
            # durable run in `running` for the global 15-minute tool timeout.
            # Full Pi/computer work retains its longer execution budget.
            timeout=httpx.Timeout(60.0, connect=20.0),
        )
        if pending.strip():
            process_line(pending)
        self._mark_run_timing(run_id, "upstream_complete_ms")
        if status < 200 or status >= 300:
            self.record(user, provider, "/chat/completions", status, usage, run_id=run_id)
            message = "El proveedor no pudo completar la respuesta."
            if response_body:
                try:
                    payload = json.loads(response_body)
                    nested = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(nested, dict) and isinstance(nested.get("message"), str):
                        message = nested["message"]
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            raise DirectChatError(status, message)
        answer = "".join(answer_parts).strip()
        if not answer or (
            structured_answer
            and (finish_reason == "length" or not _valid_agent_envelope(answer))
        ):
            # Some OpenAI-compatible gateways occasionally acknowledge an SSE
            # request with a terminal/usage frame but no content frame. Retry
            # once as ordinary JSON so a valid model response is not surfaced
            # to the app as "El modelo no devolvió texto".
            logging.warning(
                "Invalid direct-chat stream; retrying as JSON run_id=%s structured=%s finish_reason=%s",
                run_id,
                structured_answer,
                finish_reason or "unknown",
            )
            retry_payload = dict(request_payload)
            retry_payload["stream"] = False
            retry_payload.pop("stream_options", None)
            if structured_answer:
                retry_payload["messages"] = [
                    *request_payload["messages"],
                    {
                        "role": "user",
                        "content": (
                            "La respuesta anterior quedó incompleta o no respetó el contrato. "
                            "Devuelve nuevamente y exclusivamente el objeto JSON completo, "
                            "válido y sin markdown."
                        ),
                    },
                ]
            retry_status, _retry_headers, retry_body, retry_usage = proxy_request(
                "POST",
                provider["base_url"],
                "/chat/completions",
                {
                    "content-type": "application/json",
                    "accept": "application/json",
                    "user-agent": DEFAULT_UA,
                },
                json.dumps(
                    retry_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                provider["api_key"],
                timeout=httpx.Timeout(60.0, connect=20.0),
            )
            for field in (
                "input_tokens", "output_tokens", "cached_read", "cached_write"
            ):
                first = getattr(usage, field, None)
                second = getattr(retry_usage, field, None)
                setattr(
                    usage,
                    field,
                    None if first is None and second is None else int(first or 0) + int(second or 0),
                )
            usage.model = retry_usage.model or usage.model
            status = retry_status
            response_body = retry_body
            answer = _completion_message_text(retry_body)
            if answer and event_stream:
                self._mark_run_timing(run_id, "first_visible_delta_ms")
                if structured_answer:
                    visible = _agent_envelope_text(answer)
                    if visible and len(visible) > len(event_stream.visible_sent):
                        event_stream.text_delta(visible[len(event_stream.visible_sent):])
                else:
                    event_stream.text_delta(answer)
        self.record(user, provider, "/chat/completions", status, usage, run_id=run_id)
        if status < 200 or status >= 300:
            raise DirectChatError(status, "El proveedor no pudo completar la respuesta.")
        if not answer:
            raise DirectChatError(502, "El modelo no devolvió texto.", "empty_model_response")
        if structured_answer and not _valid_agent_envelope(answer):
            raise DirectChatError(
                502,
                "El modelo devolvió una respuesta estructurada incompleta.",
                "invalid_model_response",
            )
        return PiRunResult(
            run_id=run_id,
            answer=answer,
            model=usage.model or self.cfg.pi_model,
            duration_seconds=round(time.monotonic() - started, 3),
            usage={
                "input_tokens": int(usage.input_tokens or 0),
                "output_tokens": int(usage.output_tokens or 0),
                "cached_read_tokens": int(usage.cached_read or 0),
                "cached_write_tokens": int(usage.cached_write or 0),
            },
            browser=False,
            event_log="",
            stderr_log="",
        )

    def handle_agent_warm(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        bot_id = body.get("bot_id")
        if not isinstance(bot_id, str) or not bot_id.strip() or len(bot_id.strip()) > 200:
            error_response(handler, 400, "bot_id no es válido", "bad_bot_id")
            return
        try:
            self.model_provider(user)
        except ModelProviderUnavailable as exc:
            error_response(handler, 503, str(exc), "model_unavailable")
            return
        conversation_key = f"{user['id']}\0{bot_id.strip()}"

        with self._pi_warm_lock:
            warm_event = self._pi_warm_events.get(conversation_key)
            if warm_event is None or warm_event.is_set():
                warm_event = threading.Event()
                self._pi_warm_events[conversation_key] = warm_event
                should_start = True
            else:
                should_start = False

        if not should_start:
            # Another request is already warming the same bot. Wait for its
            # verified outcome instead of returning a false "ready" signal.
            if not warm_event.wait(timeout=28.0):
                error_response(handler, 503, "El agente sigue iniciando", "pi_warm_timeout")
                return
            json_response(handler, 200, {"ready": True, "started": False, "warming": False})
            return
        try:
            result = self.pi.prewarm(conversation_key=conversation_key)
        except PiHarnessBusy as exc:
            error_response(handler, 429, str(exc), "pi_busy")
            return
        except PiHarnessTimeout as exc:
            error_response(handler, 504, str(exc), "pi_warm_timeout")
            return
        except PiHarnessError as exc:
            error_response(handler, 502, str(exc), "pi_error")
            return
        finally:
            warm_event.set()
        json_response(handler, 200, {
            "ready": True,
            "started": bool(result.get("started")),
            "warming": False,
            "duration_ms": result.get("duration_ms"),
        })

    def handle_agent_run(self, handler: BaseHTTPRequestHandler) -> None:
        request_started_at = time.monotonic()
        pre_run_timings: dict[str, float] = {}

        def mark_pre_run(name: str) -> None:
            if name not in pre_run_timings:
                pre_run_timings[name] = round(
                    (time.monotonic() - request_started_at) * 1000, 3
                )

        user = self.require_user(handler)
        if not user:
            return
        mark_pre_run("auth_complete_ms")
        unlimited = self.unlimited_usage(user)
        # Internal unlimited accounts are already protected by authenticated
        # sessions and the concurrent-run reservation. Avoid an additional
        # cross-region Postgres write on their latency-sensitive chat path.
        rate_scope = f"agent-run:{user['id']}"
        rate_allowed = (
            self._consume_local_rate_limit(rate_scope, limit=30, window_seconds=60)
            if unlimited
            else self.store.consume_rate_limit(rate_scope, limit=30, window_seconds=60)
        )
        if not rate_allowed:
            error_response(handler, 429, "Demasiadas ejecuciones", "rate_limit")
            return
        mark_pre_run("rate_limit_complete_ms")
        tier = user.get("tier") or DEFAULT_TIER
        if not unlimited:
            self.ensure_trial(user)
        try:
            selected_provider = self.model_provider(user)
        except ModelProviderUnavailable as exc:
            error_response(handler, 503, str(exc), "model_unavailable")
            return

        body = self.read_json(handler) or {}
        prompt = body.get("prompt")
        browser = body.get("browser", False)
        computer_requested = body.get("computer", False)
        stream_requested = body.get("stream", False)
        execution_mode = body.get("execution_mode", "agent")
        chat_prompt = body.get("chat_prompt", "")
        routing_context = body.get("routing_context")
        user_message = body.get("user_message", "")
        client_timezone = body.get("client_timezone")
        bot_id = body.get("bot_id")
        connector_ids_value = body.get("connector_ids", [])
        idempotency_key = body.get("idempotency_key")
        max_credits_value = body.get(
            "max_credits", self.cfg.credits.default_run_max_milli / 1000
        )
        if not isinstance(prompt, str) or not prompt.strip():
            error_response(handler, 400, "Envia un prompt de texto no vacio", "bad_prompt")
            return
        if not isinstance(browser, bool):
            error_response(handler, 400, "browser debe ser true o false", "bad_browser")
            return
        if browser and self.cfg.pi_browser_min_memory_mb > 0:
            memory_limit = runtime_memory_limit_mb()
            if (
                memory_limit is not None
                and memory_limit < self.cfg.pi_browser_min_memory_mb
            ):
                logging.warning(
                    "Browser run rejected: container memory %sMB is below %sMB",
                    memory_limit,
                    self.cfg.pi_browser_min_memory_mb,
                )
                error_response(
                    handler,
                    503,
                    "La navegación web está temporalmente fuera de servicio; el resto de Agentgenia sigue disponible",
                    "pi_browser_insufficient_memory",
                )
                return
        if not isinstance(computer_requested, bool):
            error_response(handler, 400, "computer debe ser true o false", "bad_computer")
            return
        if not isinstance(stream_requested, bool):
            error_response(handler, 400, "stream debe ser true o false", "bad_stream")
            return
        if execution_mode not in ("agent", "auto", "chat"):
            error_response(handler, 400, "execution_mode no es válido", "bad_execution_mode")
            return
        if not isinstance(chat_prompt, str) or len(chat_prompt) > self.cfg.pi_max_prompt_chars:
            error_response(handler, 400, "chat_prompt no es válido", "bad_chat_prompt")
            return
        if routing_context is not None and (
            not isinstance(routing_context, str) or len(routing_context) > 10_000
        ):
            error_response(handler, 400, "routing_context no es válido", "bad_routing_context")
            return
        if not isinstance(user_message, str) or len(user_message) > 20_000:
            error_response(handler, 400, "user_message no es válido", "bad_user_message")
            return
        local_timezone = None
        if client_timezone is not None:
            if not isinstance(client_timezone, str) or not client_timezone.strip() or len(client_timezone) > 100:
                error_response(handler, 400, "client_timezone no es válido", "bad_client_timezone")
                return
            client_timezone = client_timezone.strip()
            try:
                local_timezone = ZoneInfo(client_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                error_response(handler, 400, "client_timezone no es una zona IANA válida", "bad_client_timezone")
                return
        if computer_requested and (not isinstance(bot_id, str) or not bot_id):
            error_response(handler, 400, "bot_id es obligatorio para usar una computadora", "bad_bot_id")
            return
        if computer_requested and not unlimited and not has_model_access(tier):
            error_response(
                handler,
                402,
                "Tu plan no incluye una computadora persistente",
                "computer_upgrade_required",
            )
            return
        if bot_id is not None and (
            not isinstance(bot_id, str)
            or not bot_id.strip()
            or len(bot_id.strip()) > 200
        ):
            error_response(handler, 400, "bot_id no es válido", "bad_bot_id")
            return
        bot_id = bot_id.strip() if isinstance(bot_id, str) else None
        approved_action: dict[str, Any] | None = None
        approval_rejected = False
        approval_value = body.get("approval")
        if approval_value is not None:
            if (
                not isinstance(approval_value, dict)
                or not isinstance(approval_value.get("approval_id"), str)
                or approval_value.get("decision") not in {"approve", "reject"}
                or not bot_id
            ):
                error_response(handler, 400, "approval no es válida", "bad_approval")
                return
            approval_id = approval_value["approval_id"].strip()
            if not approval_id.startswith("apr_") or len(approval_id) > 100:
                error_response(handler, 400, "approval_id no es válido", "bad_approval")
                return
            if approval_value["decision"] == "approve":
                approved_action = self.store.approve_pending_approval(
                    user_id=user["id"], bot_id=bot_id, approval_id=approval_id
                )
                if approved_action is None:
                    error_response(
                        handler,
                        409,
                        "La aprobación venció, ya fue usada o no pertenece a este agente",
                        "approval_unavailable",
                    )
                    return
                if approved_action["target_type"] == "computer":
                    computer_requested = True
            else:
                if not self.store.reject_pending_approval(
                    user_id=user["id"], bot_id=bot_id, approval_id=approval_id
                ):
                    error_response(
                        handler,
                        409,
                        "La aprobación venció, ya fue usada o no pertenece a este agente",
                        "approval_unavailable",
                    )
                    return
                approval_rejected = True
        if (
            not isinstance(idempotency_key, str)
            or not 8 <= len(idempotency_key.strip()) <= 200
        ):
            error_response(
                handler, 400, "idempotency_key es obligatorio", "bad_idempotency_key"
            )
            return
        idempotency_key = idempotency_key.strip()
        requested_credits = Decimal(0)
        try:
            requested_credits = Decimal(str(max_credits_value))
            max_credit_milli = int(requested_credits * 1000)
        except (InvalidOperation, ValueError):
            max_credit_milli = 0
        if (
            max_credit_milli <= 0
            or requested_credits * 1000 != max_credit_milli
            or max_credit_milli > self.cfg.credits.deep_run_max_milli
        ):
            error_response(
                handler, 400,
                f"max_credits debe ser positivo y no superar "
                f"{credits_float(self.cfg.credits.deep_run_max_milli)}",
                "bad_max_credits",
            )
            return
        try:
            requested_connector_ids = self.connectors.normalize_connector_ids(connector_ids_value)
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return

        # Current clients send only recent user-visible conversation as a
        # routing hint. Decide the no-tools path before reading account state
        # or calling Composio: ordinary conversation must go directly to the
        # model even when the bot owns connected tools.
        has_fast_routing_context = routing_context is not None
        direct_chat = bool(
            has_fast_routing_context
            and not approved_action
            and self._direct_chat_allowed(
                execution_mode,
                user_message,
                browser=browser,
                computer=computer_requested,
                conversation_context=routing_context or "",
            )
            and chat_prompt.strip()
        )
        assigned_connector_ids: tuple[str, ...] = ()
        connected_connector_ids: tuple[str, ...] = ()
        connector_ids: tuple[str, ...] = ()
        if not direct_chat and not approval_rejected:
            assigned_connector_ids = self._assigned_connector_ids(user["id"], bot_id)
            mark_pre_run("connector_assignment_complete_ms")
        # ``connector_ids`` from a client is a hint for backwards
        # compatibility, never an authorization source. A connector must be
        # assigned to this bot in server state and currently connected.
        if (
            not direct_chat
            and not approval_rejected
            and requested_connector_ids
            and set(requested_connector_ids) != set(assigned_connector_ids)
        ):
            logging.info(
                "Ignoring stale connector scope user_id=%s bot_id=%s requested=%s assigned=%s",
                user["id"], bot_id, requested_connector_ids, assigned_connector_ids,
            )
        if not direct_chat and not approval_rejected and assigned_connector_ids:
            try:
                connected_connector_ids = self.connector_gateway.connected_connector_ids(
                    user["id"]
                )
            except ConnectorBrokerError as exc:
                error_response(
                    handler,
                    503,
                    "No pudimos verificar las conexiones de este agente",
                    "connector_status_unavailable",
                )
                logging.warning("Connector reconciliation failed: %s", exc)
                return
            mark_pre_run("connector_verification_complete_ms")
            # Authorization is the intersection below, not the mutable account
            # snapshot. Connector list/auth/disconnect endpoints already
            # reconcile UI state; doing it again on every agent turn performed
            # a second cross-region account-state read before Pi could start.
        if not direct_chat and not approval_rejected:
            connected_set = set(connected_connector_ids)
            connector_ids = tuple(
                item for item in assigned_connector_ids if item in connected_set
            )
        if approved_action and approved_action["target_type"] == "connector":
            if approved_action["connector_id"] not in connector_ids:
                error_response(
                    handler,
                    409,
                    "El conector aprobado ya no está conectado o asignado a este agente",
                    "approval_connector_unavailable",
                )
                return
        approved_connector_execution = bool(
            approved_action is not None
            and approved_action["target_type"] == "connector"
        )

        if not has_fast_routing_context and not approved_action and not approval_rejected:
            # Backwards-compatible routing for clients that predate the
            # compact context field. It intentionally retains the older
            # connector verification order until those clients update.
            direct_chat = self._direct_chat_allowed(
                execution_mode,
                user_message,
                browser=browser,
                computer=computer_requested,
                conversation_context=chat_prompt if connector_ids else "",
            )
        if direct_chat and chat_prompt.strip():
            effective_prompt = chat_prompt.strip()
        else:
            direct_chat = False
            effective_prompt = prompt
        if approved_action:
            direct_chat = False
            effective_prompt = (
                f"{prompt}\n\nEl usuario aprobó una acción estructurada de un solo uso. "
                "Ejecuta exactamente esta operación con exactamente estos argumentos; "
                "no cambies destinatarios, contenido, comando, URL ni ningún otro campo: "
                f"target={approved_action['target_type']}; "
                f"connector={approved_action['connector_id']}; "
                f"operation={approved_action['operation']}; "
                f"arguments={json.dumps(approved_action['arguments'], ensure_ascii=False, sort_keys=True)}. "
                "Si la herramienta rechaza la coincidencia, informa que la aprobación ya no es válida."
            )

        if not direct_chat:
            if AGENT_RESPONSE_STYLE_INSTRUCTION not in effective_prompt:
                effective_prompt = (
                    f"{effective_prompt}\n\n{AGENT_RESPONSE_STYLE_INSTRUCTION}"
                )
            if connector_ids:
                connector_names = ", ".join(
                    f"{CONNECTOR_CATALOG[item]['name']} ({item})"
                    for item in connector_ids
                )
                eager_tools = len(connector_ids) <= MAX_EAGER_CONNECTORS
                connector_tool_context = (
                    "Las herramientas exactas de esos conectores ya están activadas "
                    "para esta ejecución. Invoca directamente la herramienta del "
                    "proveedor y su operación; no llames connector_search primero. "
                    + " ".join(
                        f"connector_{item.replace('-', '_')} admite: "
                        f"{', '.join(CONNECTOR_CATALOG[item]['operations'])}."
                        for item in connector_ids
                    )
                    if eager_tools
                    else (
                        "Hay muchos conectores asignados; usa connector_search una "
                        "sola vez para activar únicamente el proveedor necesario."
                    )
                )
                connector_context = (
                    "Conectores autenticados disponibles para esta ejecución: "
                    f"{connector_names}. {connector_tool_context} Para "
                    "consultar o modificar datos externos debes basar la respuesta "
                    "en el resultado exitoso de esa herramienta. Si no ejecutaste "
                    "la herramienta o falló, dilo claramente: nunca inventes correos, "
                    "eventos, archivos, registros ni acciones completadas."
                    " Para una operación de escritura con todos los datos y una "
                    "solicitud explícita, invoca inmediatamente la herramienta de "
                    "escritura. No pidas que el usuario responda 'apruebo' ni "
                    "solicites confirmación en prosa: el backend detendrá esa "
                    "herramienta y mostrará la aprobación estructurada de un solo "
                    "uso. Si falta un argumento imprescindible, pregunta únicamente "
                    "por ese dato."
                )
                if local_timezone is not None:
                    local_now = datetime.now(timezone.utc).astimezone(local_timezone)
                    connector_context += (
                        " Fecha y hora local del dispositivo: "
                        f"{local_now.isoformat(timespec='seconds')}; zona IANA: "
                        f"{client_timezone}. Para acciones de calendario convierte "
                        "fechas relativas a ISO 8601 exacto usando esta zona."
                    )
                else:
                    connector_context += (
                        " El dispositivo no envió su zona horaria. Para una acción "
                        "de calendario con hora local, pregunta la zona IANA al "
                        "usuario en vez de asumir UTC."
                    )
                if "No hay conectores seleccionados." in effective_prompt:
                    effective_prompt = effective_prompt.replace(
                        "No hay conectores seleccionados.", connector_context, 1
                    )
                elif connector_context not in effective_prompt:
                    effective_prompt = f"{effective_prompt}\n\n{connector_context}"

        if len(effective_prompt) > self.cfg.pi_max_prompt_chars:
            error_response(
                handler,
                400,
                f"El prompt final excede {self.cfg.pi_max_prompt_chars} caracteres",
                "prompt_too_large",
            )
            return

        pi_status = self.pi.status()
        if not direct_chat and not approved_connector_execution and not approval_rejected:
            if not pi_status["enabled"]:
                error_response(handler, 503, "El harness de Pi esta desactivado", "pi_disabled")
                return
            if not pi_status["available"]:
                error_response(handler, 503, "Pi no esta instalado o PI_BIN es invalido", "pi_unavailable")
                return
            if browser and not (
                pi_status["browser_available"] and pi_status["browser_auto_authorize"]
            ):
                error_response(handler, 409, "Chrome no esta configurado para Pi", "pi_browser_unavailable")
                return
        if computer_requested and not self.computers.configured:
            error_response(
                handler,
                409,
                "La computadora solicitada no está disponible en este despliegue",
                "computer_unavailable",
            )
            return
        computer_enabled = bool(computer_requested)
        if (
            not direct_chat
            and not approved_connector_execution
            and not approval_rejected
            and (connector_ids or computer_enabled)
            and not pi_status["connectors_available"]
        ):
            error_response(
                handler,
                409,
                "La extension TypeScript de conectores no esta instalada",
                "pi_connectors_unavailable",
            )
            return

        conversation_lock = self._conversation_lock(user["id"], bot_id)
        if conversation_lock is not None:
            # One bot is one ordered conversation across desktop, mobile and
            # WhatsApp. Queue the next turn instead of letting the harness or
            # the credit concurrency guard reject it with a transient 429.
            if not conversation_lock.acquire(timeout=self.cfg.pi_timeout_seconds + 60):
                error_response(
                    handler, 503,
                    "La conversación anterior sigue ejecutándose",
                    "conversation_busy",
                )
                return
            handler.agent_conversation_lock = conversation_lock

        run_api_key = "agrn_" + secrets.token_urlsafe(48)
        mark_pre_run("pre_reservation_complete_ms")
        try:
            plan = plan_for(tier)
            run_values = {
                "user_id": user["id"],
                "idempotency_key": idempotency_key,
                "model": self.cfg.pi_model,
                "browser": browser,
                "max_credit_milli": max_credit_milli,
                "max_concurrent_runs": (
                    self.cfg.pi_max_concurrent if unlimited else plan.max_concurrent_runs
                ),
                "token_hash": hash_agent_run_token(run_api_key),
                "token_expires_at": (
                    time.time() + self.cfg.credits.reservation_ttl_seconds
                ),
            }
            if unlimited:
                prepared = self.store.create_unmetered_agent_run(**run_values)
            else:
                prepared = self.store.create_agent_run(
                    **run_values,
                    five_hour_credit_milli=plan.five_hour_credit_milli,
                    seven_day_credit_milli=plan.seven_day_credit_milli,
                    enforce=self.cfg.credits.mode == "enforce",
                )
        except RuntimeError as exc:
            if str(exc) == "insufficient_credits":
                error_response(handler, 402, "Créditos insuficientes", "insufficient_credits")
                return
            if str(exc) == "credit_concurrency_limit":
                error_response(
                    handler, 429, "Tu plan alcanzó su límite de ejecuciones simultáneas",
                    "run_concurrency_limit",
                )
                return
            if str(exc) == "credit_5h_limit":
                error_response(handler, 429, "Tu plan alcanzó el límite de créditos de 5 horas", "credit_5h_limit")
                return
            if str(exc) == "credit_7d_limit":
                error_response(handler, 429, "Tu plan alcanzó el límite de créditos de 7 días", "credit_7d_limit")
                return
            raise
        if prepared["duplicate"]:
            existing = prepared["run"]
            # A mobile client may retry after losing the HTTP response while
            # the original request is still finishing. For non-streaming
            # idempotent replays, briefly follow the durable row instead of
            # returning a 409 that discards a result already in progress.
            if not stream_requested and existing.get("status") in {"reserved", "running"}:
                deadline = time.monotonic() + 60.0
                while time.monotonic() < deadline:
                    time.sleep(0.2)
                    refreshed = self.store.get_agent_run_for_user(
                        existing["id"], user["id"]
                    )
                    if not refreshed:
                        break
                    existing = refreshed
                    if existing.get("status") not in {"reserved", "running"}:
                        break
            if existing.get("status") == "succeeded" and existing.get("result_json"):
                try:
                    recovered = json.loads(existing["result_json"])
                except (TypeError, json.JSONDecodeError):
                    recovered = None
                if isinstance(recovered, dict):
                    if stream_requested:
                        recovered_stream = _AgentEventStream(handler)
                        recovered_stream.start(existing["id"])
                        recovered_stream.done_text(str(recovered.get("answer") or ""))
                    else:
                        json_response(handler, 200, recovered)
                    return
            error_response(
                handler, 409,
                f"La ejecución ya existe: {existing['id']} ({existing['status']})",
                "run_already_exists",
            )
            return
        run = prepared["run"]
        run_id = run["id"]
        self._run_principal(run_api_key, value=(user, run))
        started_at = time.monotonic()
        self._start_run_timing(run_id, request_started_at, pre_run_timings)
        self._mark_run_timing(run_id, "run_reserved_ms")
        event_stream = _AgentEventStream(handler) if stream_requested else None
        if event_stream:
            event_stream.start(run_id)

        def agent_error(status: int, message: str, code: str) -> None:
            self._mark_run_timing(run_id, "failed_ms")
            if event_stream:
                event_stream.error(status, message, code)
            else:
                error_response(handler, status, message, code)
            self._run_timing_snapshot(run_id, pop=True)

        def settle(final_status: str, error_code: str | None = None) -> tuple[dict, dict]:
            timing_warning = "timing:" + json.dumps(
                self._run_timing_snapshot(run_id), separators=(",", ":")
            )
            if unlimited:
                settled = self.store.settle_unmetered_agent_run(
                    run_id=run_id,
                    final_status=final_status,
                    duration_seconds=max(0.0, time.monotonic() - started_at),
                    error_code=error_code,
                    warnings=[timing_warning],
                )
                return settled, {
                    "mode": "unlimited",
                    "reserved": 0.0,
                    "charged": 0.0,
                    "released": 0.0,
                    "balance_after": None,
                }
            llm_cost = self.store.agent_run_cost_microusd(run_id)
            run_state = self.store.get_agent_run(run_id) or run
            extra_cost = int(run_state.get("extra_cost_microusd") or 0)
            charged = (
                0
                if self.cfg.credits.mode == "off" or unlimited
                else self.credits.billable_milli(llm_cost, extra_cost)
            )
            if not unlimited and charged >= max_credit_milli and final_status != "succeeded":
                final_status = "budget_exhausted"
                error_code = "run_budget_exhausted"
            settled = self.store.settle_agent_run(
                run_id=run_id,
                charged_milli=charged,
                final_status=final_status,
                duration_seconds=max(0.0, time.monotonic() - started_at),
                error_code=error_code,
                warnings=[timing_warning],
            )
            balance = self.store.credit_summary(user["id"], recent_limit=0)
            reserved = int(settled["reserved_credit_milli"])
            actual = int(settled["charged_credit_milli"])
            credits = {
                "mode": self.cfg.credits.mode,
                "reserved": credits_float(reserved),
                "charged": credits_float(actual),
                "released": credits_float(max(0, reserved - actual)),
                "balance_after": credits_float(balance["available_milli"]),
            }
            return settled, credits

        connector_run_token = None
        self._run_provider(run_id, value=selected_provider)
        try:
            # The optimized unlimited Postgres reservation inserts the run in
            # ``running`` state in the same round trip. Metered/SQLite runs
            # retain the explicit transition used by the credit ledger.
            if not unlimited:
                self.store.mark_agent_run_running(run_id)
            if approval_rejected:
                result = PiRunResult(
                    run_id=run_id,
                    answer=json.dumps(
                        {
                            "text": "Acción cancelada. No se realizó ningún cambio.",
                            "widget": None,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    model=self.cfg.pi_model,
                    duration_seconds=round(time.monotonic() - started_at, 3),
                    usage={
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_read_tokens": 0,
                        "cached_write_tokens": 0,
                    },
                    browser=False,
                    event_log="",
                    stderr_log="",
                )
                self._mark_run_timing(run_id, "approval_rejected_ms")
            elif direct_chat:
                result = self._run_direct_chat(
                    run_id=run_id,
                    user=user,
                    provider=selected_provider,
                    prompt=effective_prompt,
                    event_stream=event_stream,
                )
                self._mark_run_timing(run_id, "direct_complete_ms")
            else:
                if bot_id is not None and not approved_connector_execution:
                    conversation_key = f"{user['id']}\0{bot_id}"
                    with self._pi_warm_lock:
                        warm_event = self._pi_warm_events.pop(conversation_key, None)
                    if warm_event is not None and not warm_event.is_set():
                        self._mark_run_timing(run_id, "pi_warm_wait_started_ms")
                        warm_event.wait(timeout=28.0)
                        self._mark_run_timing(run_id, "pi_warm_wait_finished_ms")
                if connector_ids or computer_enabled:
                    connector_run_token = self.connectors.issue(
                        user_id=user["id"],
                        run_id=run_id,
                        connector_ids=connector_ids,
                        bot_id=bot_id,
                        computer_id=bot_id if computer_enabled else None,
                        approved_action=approved_action,
                    )
                self._mark_run_timing(run_id, "pi_dispatch_ms")

                if approved_connector_execution:
                    # The model already selected the operation and the user
                    # approved its canonical argument hash. Sending the exact
                    # capability back through another model round adds latency
                    # and can only introduce drift. Execute it directly through
                    # the same one-shot broker boundary used by Pi.
                    action_started = time.monotonic()
                    self.connectors.execute(
                        token=connector_run_token or "",
                        connector_id=approved_action["connector_id"],
                        operation=approved_action["operation"],
                        arguments=approved_action["arguments"],
                        operation_id=approved_action["action_id"],
                        approval_id=approved_action["id"],
                        action_id=approved_action["action_id"],
                    )
                    result = PiRunResult(
                        run_id=run_id,
                        answer=_approved_connector_confirmation(approved_action),
                        model=self.cfg.pi_model,
                        duration_seconds=round(
                            time.monotonic() - action_started, 3
                        ),
                        usage={
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cached_read_tokens": 0,
                            "cached_write_tokens": 0,
                        },
                        browser=False,
                        event_log="",
                        stderr_log="",
                    )
                    self._mark_run_timing(run_id, "connector_action_complete_ms")
                else:

                    def on_text_delta(delta: str) -> None:
                        self._mark_run_timing(run_id, "pi_first_text_ms")
                        if event_stream:
                            before = len(event_stream.visible_sent)
                            event_stream.model_delta(delta)
                            if len(event_stream.visible_sent) > before:
                                self._mark_run_timing(run_id, "first_visible_delta_ms")

                    result = self.pi.run(
                        run_id=run_id,
                        run_api_key=run_api_key,
                        prompt=effective_prompt,
                        browser=browser,
                        connector_run_token=connector_run_token,
                        connector_ids=connector_ids,
                        computer_enabled=computer_enabled,
                        thinking_level=(
                            self.cfg.pi_connector_thinking
                            if len(connector_ids) == 1 and not browser and not computer_enabled
                            else self.cfg.pi_thinking
                        ),
                        conversation_key=(
                            f"{user['id']}\0{bot_id}" if bot_id is not None else None
                        ),
                        on_text_delta=on_text_delta,
                        # Transport loss is not cancellation. The run remains
                        # durable and recoverable by run_id, avoiding duplicate
                        # external side effects when a mobile/desktop stream drops.
                        is_cancelled=None,
                    )
                    self._mark_run_timing(run_id, "pi_complete_ms")
        except DirectChatError as e:
            _settled, _credits = settle("failed", e.code)
            agent_error(e.status, str(e), e.code)
            return
        except ConnectorBrokerError as e:
            self.store.release_agent_run(
                run_id=run_id,
                final_status="failed",
                error_code=e.code,
                duration_seconds=max(0.0, time.monotonic() - started_at),
            )
            agent_error(e.status, str(e), e.code)
            return
        except PiHarnessBusy as e:
            self.store.release_agent_run(
                run_id=run_id,
                final_status="failed",
                error_code="pi_busy",
                duration_seconds=max(0.0, time.monotonic() - started_at),
            )
            agent_error(429, str(e), "pi_busy")
            return
        except PiHarnessCancelled as e:
            _settled, _credits = settle("cancelled", "client_disconnected")
            agent_error(499, str(e), "client_disconnected")
            return
        except PiHarnessUsageError as e:
            error_code = "pi_timeout" if isinstance(e, PiHarnessTimeout) else "pi_task_error"
            _settled, _credits = settle("failed", error_code)
            agent_error(
                504 if isinstance(e, PiHarnessTimeout) else 502,
                str(e),
                error_code,
            )
            return
        except PiHarnessError as e:
            stderr_path = self.cfg.pi_runs_dir / run_id / "stderr.log"
            try:
                stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                stderr_tail = "<stderr unavailable>"
            logging.error(
                "Pi harness failure run_id=%s error=%s stderr_tail=%s",
                run_id,
                type(e).__name__,
                stderr_tail,
            )
            # A failure in our own harness must not consume customer credits.
            # The only exception is a model call that was explicitly stopped
            # because it reached the user's authorized run budget.
            llm_cost = self.store.agent_run_cost_microusd(run_id)
            run_state = self.store.get_agent_run(run_id) or run
            consumed = self.credits.billable_milli(
                llm_cost, int(run_state.get("extra_cost_microusd") or 0)
            )
            if not unlimited and consumed >= max_credit_milli:
                settle("budget_exhausted", "run_budget_exhausted")
                agent_error(
                    402,
                    "La ejecución alcanzó su máximo autorizado",
                    "run_budget_exhausted",
                )
            else:
                self.store.release_agent_run(
                    run_id=run_id,
                    final_status="failed",
                    error_code="pi_error",
                    duration_seconds=max(0.0, time.monotonic() - started_at),
                )
                agent_error(502, str(e), "pi_error")
            return
        except Exception:
            self.store.release_agent_run(
                run_id=run_id,
                final_status="failed",
                error_code="internal_error",
                duration_seconds=max(0.0, time.monotonic() - started_at),
            )
            if event_stream:
                logging.exception("Streaming agent run failed run_id=%s", run_id)
                agent_error(500, "Internal server error", "internal_error")
                return
            self._run_timing_snapshot(run_id, pop=True)
            raise
        finally:
            self.connectors.revoke(connector_run_token)
            self._run_provider(run_id, pop=True)
            self._run_principal(run_api_key, pop=True)
        pending_approvals = self.store.pending_approvals_for_run(user["id"], run_id)
        if pending_approvals:
            # The provider/model cannot define the approval UI. Replace any
            # prose it produced after the denied tool call with a deterministic
            # typed event owned by the backend.
            result.answer = _approval_envelope(pending_approvals[0])
        normalized_answer = _bounded_agent_envelope(result.answer)
        if normalized_answer is None:
            settle("failed", "invalid_model_response")
            agent_error(
                502,
                "El agente devolvió una respuesta estructurada incompleta",
                "invalid_model_response",
            )
            return
        result.answer = normalized_answer
        # Persist the human-visible result before charging or marking success.
        # If persistence fails, release the reservation and return an error;
        # the system can no longer reach a charged-without-result state.
        provisional = result.as_dict()
        provisional.update({
            "run_id": run_id,
            "status": "running",
            "connector_ids": [] if direct_chat else list(connector_ids),
            "computer_enabled": computer_enabled,
            "execution_path": "direct_chat" if direct_chat else "pi",
        })
        try:
            self.store.save_agent_run_result(run_id, provisional)
        except Exception:
            self.store.release_agent_run(
                run_id=run_id,
                final_status="failed",
                error_code="result_persistence_failed",
                duration_seconds=max(0.0, time.monotonic() - started_at),
            )
            logging.exception("Could not persist agent result run_id=%s", run_id)
            agent_error(500, "No pudimos guardar el resultado de la ejecución", "result_persistence_failed")
            return

        settled, credits = settle("succeeded")
        payload = provisional
        payload["run_id"] = run_id
        payload["status"] = settled["status"]
        payload["credits"] = credits
        payload["usage"].update({
            "llm_cost_microusd": int(settled["llm_cost_microusd"]),
            "llm_cost_usd": round(int(settled["llm_cost_microusd"]) / 1_000_000, 6),
            "extra_cost_microusd": int(settled["extra_cost_microusd"]),
            "duration_seconds": float(settled["duration_seconds"] or 0),
        })
        payload["connector_ids"] = [] if direct_chat else list(connector_ids)
        payload["computer_enabled"] = computer_enabled
        payload["execution_path"] = "direct_chat" if direct_chat else "pi"
        self._mark_run_timing(run_id, "response_ready_ms")
        payload["timings"] = self._run_timing_snapshot(run_id)
        try:
            self.store.save_agent_run_result(run_id, payload)
        except Exception:
            # The provisional answer was durably stored before settlement, so
            # recovery remains possible even if the metadata refresh fails.
            logging.exception("Could not refresh final agent metadata run_id=%s", run_id)
        logging.info(
            "agent timing run_id=%s timings=%s",
            run_id,
            json.dumps(payload["timings"], separators=(",", ":")),
        )
        if event_stream:
            # The terminal frame is intentionally minimal and contains the
            # same human-readable text emitted by `delta`. Runtime metadata is
            # already persisted server-side; putting the full accounting
            # payload in this frame made mobile clients decode unrelated
            # optional fields before they could accept the answer.
            # Base64 keeps the terminal frame decoder-safe without throwing
            # away the structured widget next to the visible text. The iOS UI
            # has already streamed the human-readable ``text`` field and uses
            # this raw envelope to install the final widget atomically.
            event_stream.done_text(result.answer)
        else:
            json_response(handler, 200, payload)
        self._run_timing_snapshot(run_id, pop=True)

    # ---------- computadoras persistentes por bot ----------
    def handle_computer_status(self, handler: BaseHTTPRequestHandler, bot_id: str) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, self.computers.status(user_id=user["id"], bot_id=bot_id))

    def handle_computer_ensure(self, handler: BaseHTTPRequestHandler, bot_id: str) -> None:
        user = self.require_user(handler)
        if not user:
            return
        if not self.store.consume_rate_limit(
            f"computer-ensure:{user['id']}", limit=10, window_seconds=60
        ):
            error_response(handler, 429, "Demasiadas solicitudes de computadora", "rate_limit")
            return
        tier = user.get("tier") or DEFAULT_TIER
        if not has_model_access(tier):
            error_response(handler, 402, "Tu plan no incluye una computadora persistente", "tier_requires_upgrade")
            return
        body = self.read_json(handler) or {}
        bot_name = body.get("bot_name") if isinstance(body.get("bot_name"), str) else "Bot"
        json_response(
            handler,
            200,
            self.computers.ensure(user_id=user["id"], bot_id=bot_id, bot_name=bot_name),
        )

    def handle_computer_hand_back(self, handler: BaseHTTPRequestHandler, bot_id: str) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, self.computers.hand_back(user_id=user["id"], bot_id=bot_id))

    def handle_computer_delete(self, handler: BaseHTTPRequestHandler, bot_id: str) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, self.computers.delete(user_id=user["id"], bot_id=bot_id))

    # ---------- broker interno de conectores ----------
    @staticmethod
    def _is_loopback_request(handler: BaseHTTPRequestHandler) -> bool:
        try:
            return ipaddress.ip_address(handler.client_address[0]).is_loopback
        except ValueError:
            return False

    def _connector_token(self, handler: BaseHTTPRequestHandler) -> str | None:
        if not self._is_loopback_request(handler):
            error_response(handler, 403, "El broker solo acepta trafico loopback", "connector_loopback_only")
            return None
        token = (handler.headers.get("X-Connector-Run-Token") or "").strip()
        if not token:
            error_response(handler, 401, "Falta el token interno de ejecucion", "connector_token_required")
            return None
        return token

    def handle_connector_catalog(self, handler: BaseHTTPRequestHandler) -> None:
        token = self._connector_token(handler)
        if not token:
            return
        try:
            connectors = self.connectors.catalog(token)
            computer = self.connectors.has_computer(token)
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return
        json_response(handler, 200, {"connectors": connectors, "computer": computer})

    def handle_connector_execute(self, handler: BaseHTTPRequestHandler) -> None:
        token = self._connector_token(handler)
        if not token:
            return
        body = self.read_json(handler) or {}
        connector_id = body.get("connector_id")
        operation = body.get("operation")
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            error_response(handler, 400, "arguments debe ser un objeto JSON", "bad_connector_arguments")
            return
        try:
            action = self.connectors.approved_action(token)
            context = self.connectors.grant_context(token)
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return
        write_is_approved = False
        arguments_prepared = False
        if (
            not _connector_operation_is_read_only(operation)
            and isinstance(connector_id, str)
            and isinstance(operation, str)
            and action is not None
        ):
            try:
                # The adapter may canonicalize aliases before approval (for
                # example ``eventId`` -> ``event_id``). Reapply that same
                # contract before comparing the one-shot capability so an
                # approved retry cannot miss solely because it used the
                # original provider alias.
                arguments = self.connectors.prepare_arguments(
                    token,
                    connector_id,
                    operation,
                    arguments,
                    validate_provider=False,
                )
                arguments_prepared = True
                write_is_approved = self.connectors.write_is_approved(
                    token,
                    connector_id,
                    operation,
                    arguments,
                    action.approval_id,
                    action.action_id,
                )
            except ConnectorBrokerError as e:
                error_response(handler, e.status, str(e), e.code)
                return
        if (
            not _connector_operation_is_read_only(operation)
            and not write_is_approved
        ):
            if (
                not isinstance(connector_id, str)
                or connector_id not in CONNECTOR_CATALOG
                or not isinstance(operation, str)
                or operation not in CONNECTOR_CATALOG[connector_id]["operations"]
            ):
                error_response(
                    handler, 400, "Operación de conector inválida", "bad_connector_operation"
                )
                return
            if not arguments_prepared:
                try:
                    arguments = self.connectors.prepare_arguments(
                        token, connector_id, operation, arguments
                    )
                except ConnectorBrokerError as e:
                    error_response(handler, e.status, str(e), e.code)
                    return
            if not context.get("run_id") or not context.get("bot_id"):
                error_response(
                    handler, 409,
                    "Esta operación requiere una aprobación estructurada dentro de una ejecución",
                    "operation_approval_required",
                )
                return
            approval = self.store.create_pending_approval(
                user_id=context["user_id"],
                bot_id=context["bot_id"],
                run_id=context["run_id"],
                target_type="connector",
                connector_id=connector_id,
                operation=operation,
                arguments=arguments,
                arguments_hash=canonical_arguments_hash(arguments),
                human_summary=_action_summary(
                    target_type="connector",
                    connector_id=connector_id,
                    operation=operation,
                    arguments=arguments,
                ),
            )
            json_response(handler, 409, {
                "error": {
                    "message": "Esta operación requiere una aprobación humana específica",
                    "type": "operation_approval_required",
                },
                "approval": {
                    "approval_id": approval["id"],
                    "action_id": approval["action_id"],
                    "summary": approval["human_summary"],
                    "expires_at": approval["expires_at"],
                },
            })
            return
        try:
            result = self.connectors.execute(
                token=token,
                connector_id=connector_id,
                operation=operation,
                arguments=arguments,
                operation_id=(
                    action.action_id if write_is_approved and action else body.get("operation_id")
                ),
                approval_id=action.approval_id if write_is_approved and action else None,
                action_id=action.action_id if write_is_approved and action else None,
            )
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return
        json_response(handler, 200, result)

    def handle_computer_execute(self, handler: BaseHTTPRequestHandler) -> None:
        token = self._connector_token(handler)
        if not token:
            return
        user_id, bot_id = self.connectors.computer(token)
        body = self.read_json(handler) or {}
        operation = body.get("operation")
        arguments = body.get("arguments", {})
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            error_response(handler, 400, "Operación de computadora inválida", "bad_computer_operation")
            return
        action = self.connectors.approved_action(token)
        write_is_approved = False
        try:
            if action is not None:
                write_is_approved = self.connectors.computer_write_is_approved(
                    token,
                    operation,
                    arguments,
                    action.approval_id,
                    action.action_id,
                )
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return
        if (
            operation not in READ_ONLY_COMPUTER_OPERATIONS
            and not write_is_approved
        ):
            context = self.connectors.grant_context(token)
            approval = self.store.create_pending_approval(
                user_id=user_id,
                bot_id=bot_id,
                run_id=context["run_id"],
                target_type="computer",
                connector_id="computer",
                operation=operation,
                arguments=arguments,
                arguments_hash=canonical_arguments_hash(arguments),
                human_summary=_action_summary(
                    target_type="computer",
                    connector_id="computer",
                    operation=operation,
                    arguments=arguments,
                ),
            )
            json_response(handler, 409, {
                "error": {
                    "message": "Esta operación de computadora requiere aprobación humana específica",
                    "type": "operation_approval_required",
                },
                "approval": {
                    "approval_id": approval["id"],
                    "action_id": approval["action_id"],
                    "summary": approval["human_summary"],
                    "expires_at": approval["expires_at"],
                },
            })
            return
        if write_is_approved and action:
            if not self.store.dispatch_pending_approval(
                approval_id=action.approval_id,
                action_id=action.action_id,
                user_id=user_id,
                connector_id="computer",
                operation=operation,
                arguments_hash=canonical_arguments_hash(arguments),
            ):
                error_response(
                    handler, 409,
                    "Esta aprobación ya fue consumida o su resultado es incierto",
                    "approval_consumed",
                )
                return
            reservation = self.store.begin_connector_operation(
                user_id=user_id,
                run_id=self.connectors.grant_context(token)["run_id"],
                operation_id=action.action_id,
                connector_id="computer",
                operation=operation,
                arguments_hash=action.arguments_hash,
            )
            if reservation.get("status") != "owner":
                self.store.settle_pending_approval(
                    approval_id=action.approval_id,
                    action_id=action.action_id,
                    succeeded=False,
                )
                error_response(
                    handler, 409,
                    "La acción de computadora ya fue enviada y no se repetirá automáticamente",
                    "computer_operation_uncertain",
                )
                return
        try:
            result = self.computers.execute(
                user_id=user_id,
                bot_id=bot_id,
                operation=operation,
                arguments=arguments,
            )
        except Exception:
            if write_is_approved and action:
                self.store.fail_connector_operation(
                    user_id=user_id,
                    run_id=self.connectors.grant_context(token)["run_id"],
                    operation_id=action.action_id,
                    error_code="computer_operation_uncertain",
                )
                self.store.settle_pending_approval(
                    approval_id=action.approval_id,
                    action_id=action.action_id,
                    succeeded=False,
                )
            raise
        if write_is_approved and action:
            payload = {"operation": operation, "result": result}
            self.store.complete_connector_operation(
                user_id=user_id,
                run_id=self.connectors.grant_context(token)["run_id"],
                operation_id=action.action_id,
                result=payload,
            )
            self.store.settle_pending_approval(
                approval_id=action.approval_id,
                action_id=action.action_id,
                succeeded=True,
            )
        json_response(handler, 200, result)

    # ---------- body helpers ----------
    def read_body(self, handler: BaseHTTPRequestHandler, *, max_bytes: int = MAX_BODY) -> bytes:
        transfer_encoding = handler.headers.get("transfer-encoding", "").lower()
        content_length = handler.headers.get("content-length")
        encodings = [part.strip() for part in transfer_encoding.split(",") if part.strip()]
        if encodings and encodings != ["chunked"]:
            handler.close_connection = True
            raise RequestBodyError("Transfer-Encoding no soportado")
        if encodings and content_length is not None:
            handler.close_connection = True
            raise RequestBodyError("Content-Length y Transfer-Encoding no pueden combinarse")
        if encodings == ["chunked"]:
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = handler.rfile.readline(8193)
                if not size_line or len(size_line) > 8192:
                    handler.close_connection = True
                    raise RequestBodyError("Framing chunked invalido")
                try:
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                except ValueError as e:
                    handler.close_connection = True
                    raise RequestBodyError("Tamano de chunk invalido") from e
                if size == 0:
                    # Consumir trailers hasta la linea vacia final.
                    while True:
                        trailer = handler.rfile.readline(8193)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                        if len(trailer) > 8192:
                            handler.close_connection = True
                            raise RequestBodyError("Trailer chunked invalido")
                    break
                total += size
                if total > max_bytes:
                    handler.close_connection = True
                    raise RequestBodyTooLarge(f"Body mayor a {max_bytes} bytes")
                chunk = handler.rfile.read(size)
                ending = handler.rfile.read(2)
                if len(chunk) != size or ending != b"\r\n":
                    handler.close_connection = True
                    raise RequestBodyError("Chunk incompleto")
                chunks.append(chunk)
            return b"".join(chunks)
        if content_length is not None:
            if not content_length.isdigit():
                handler.close_connection = True
                raise RequestBodyError("Content-Length inválido")
            n = int(content_length)
            if n > max_bytes:
                handler.close_connection = True
                raise RequestBodyTooLarge(f"Body mayor a {max_bytes} bytes")
            body = handler.rfile.read(n)
            if len(body) != n:
                handler.close_connection = True
                raise RequestBodyError("Body incompleto")
            return body
        if handler.command in {"POST", "PUT", "PATCH"}:
            handler.close_connection = True
            raise RequestBodyError("Content-Length es obligatorio")
        return b""

    def read_json(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        max_bytes: int = MAX_JSON_BODY,
    ) -> dict | None:
        body = self.read_body(handler, max_bytes=max_bytes)
        if not body:
            return None
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestBodyError("JSON inválido") from exc
        if not isinstance(value, dict):
            raise RequestBodyError("El body JSON debe ser un objeto")
        return value

    # ---------- admin ----------
    def handle_admin_users(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_admin(handler):
            return
        users = []
        for u in self.store.list_users():
            users.append({
                "id": u["id"], "name": u["name"], "email": u["email"],
                "tier": u.get("tier") or DEFAULT_TIER,
                "model_provider": u.get("model_provider_override") or "deepseek",
                "unlimited_usage": self.unlimited_usage(u),
                "account_status": u.get("account_status") or "active",
                "disabled_at": u.get("disabled_at"),
                "created_at": u["created_at"],
            })
        json_response(handler, 200, {"users": users})

    def handle_admin_revoke(self, handler: BaseHTTPRequestHandler, user_id: str) -> None:
        if not self.require_admin(handler):
            return
        user = self.store.get_user_by_id(user_id)
        if not user:
            error_response(handler, 404, "Usuario no encontrado", "not_found")
            return
        if user.get("account_status") != "active":
            error_response(handler, 409, "La cuenta esta deshabilitada", "account_disabled")
            return
        result = self.store.revoke_user_account(user_id)
        self.google_auth.forget_user(user_id)
        self._forget_run_principals(user_id)
        ephemeral_grants_revoked = self.connectors.revoke_user(user_id)
        pi_sessions_deleted = self.pi.forget_user(user_id)
        cleanup_errors: list[str] = []
        managed_deleted = 0
        try:
            managed_deleted = self.connector_gateway.disconnect_all(user_id)
        except Exception:
            cleanup_errors.append("managed_connectors")
            logging.exception("Falló la limpieza remota de conectores para %s", user_id)
        computer_cleanup = self.computers.delete_all(user_id=user_id)
        if computer_cleanup["errors"]:
            cleanup_errors.append("computers")
            logging.error(
                "Falló la limpieza de computadoras para %s: %s",
                user_id,
                computer_cleanup["errors"],
            )
        json_response(
            handler,
            200,
            {
                "revoked": True,
                **result,
                "managed_connectors_deleted": managed_deleted,
                "ephemeral_grants_revoked": ephemeral_grants_revoked,
                "computers_deleted": computer_cleanup["deleted"],
                "pi_sessions_deleted": pi_sessions_deleted,
                "cleanup_pending": cleanup_errors,
            },
        )

    def handle_admin_set_tier(self, handler: BaseHTTPRequestHandler, user_id: str) -> None:
        if not self.require_admin(handler):
            return
        user = self.store.get_user_by_id(user_id)
        if not user:
            error_response(handler, 404, "Usuario no encontrado", "not_found")
            return
        if user.get("account_status") != "active":
            error_response(handler, 409, "La cuenta esta deshabilitada", "account_disabled")
            return
        body = self.read_json(handler) or {}
        tier = str(body.get("tier") or "").lower()
        if not is_valid(tier):
            error_response(handler, 400, f"Tier invalido: {tier}", "bad_tier")
            return
        self.store.transition_user_tier(user_id, tier)
        json_response(handler, 200, {
            "user_id": user_id,
            "tier": tier,
        })

    def handle_admin_usage(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_admin(handler):
            return
        json_response(handler, 200, self.store.usage_all())

    def handle_admin_credits(self, handler: BaseHTTPRequestHandler, user_id: str) -> None:
        if not self.require_admin(handler):
            return
        user = self.store.get_user_by_id(user_id)
        if not user:
            error_response(handler, 404, "Usuario no encontrado", "not_found")
            return
        body = self.read_json(handler) or {}
        reason = body.get("reason")
        idempotency_key = body.get("idempotency_key")
        try:
            amount = Decimal(str(body.get("credits")))
            amount_milli = int(amount * 1000)
        except (InvalidOperation, ValueError):
            amount = Decimal(0)
            amount_milli = 0
        if amount_milli <= 0 or amount * 1000 != amount_milli:
            error_response(handler, 400, "credits debe ser positivo", "bad_credits")
            return
        if not isinstance(reason, str) or not reason.strip():
            error_response(handler, 400, "reason es obligatorio", "bad_reason")
            return
        if not isinstance(idempotency_key, str) or len(idempotency_key.strip()) < 8:
            error_response(
                handler, 400, "idempotency_key es obligatorio", "bad_idempotency_key"
            )
            return
        self.store.grant_credits(
            user_id=user_id,
            amount_milli=amount_milli,
            source_type="admin_adjustment",
            source_key=f"admin:{idempotency_key.strip()}",
            metadata={"reason": reason.strip()},
        )
        json_response(handler, 201, self.credits_payload(user, recent_limit=10))

    # ---------- usage/me ----------
    def handle_credits(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(handler, 200, self.credits_payload(user))

    def handle_usage(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        summary = self.store.usage_summary(user["id"], None, tier)
        summary["tier"] = tier
        summary["tier_label"] = tier_label(tier)
        summary["legacy_windows"] = summary["windows"]
        summary["credits"] = self.credits_payload(user, recent_limit=0)["credits"]
        json_response(handler, 200, summary)

    def handle_me(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        json_response(handler, 200, {
            "user_id": user["id"], "name": user["name"], "email": user["email"],
            "tier": tier,
            "tier_label": tier_label(tier),
            "limits": effective_limits(tier),
            "plan": plan_payload(tier),
            "model_provider": user.get("model_provider_override") or "deepseek",
            "unlimited_usage": self.unlimited_usage(user),
            "credits": self.credits_payload(user, recent_limit=0)["credits"],
        })


class Handler(BaseHTTPRequestHandler):
    backend: Backend
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.agent_conversation_lock: threading.Lock | None = None

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "unknown"
        logging.info(
            "http request_id=%s method=%s status=%s",
            getattr(self, "request_id", ""),
            self.command,
            status,
        )

    def _dispatch(self):
        self.request_id = "req_" + secrets.token_hex(12)
        parsed = urlparse(self.path)
        path = parsed.path
        backend = self.backend
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if self.command == "POST" and path == "/v1/account-auth/start":
                backend.handle_account_auth_start(self)
            elif self.command == "GET" and path.startswith("/v1/account-auth/status/"):
                backend.handle_account_auth_status_get(self, path.rsplit("/", 1)[-1], query)
            elif self.command == "POST" and path == "/v1/account-auth/status":
                backend.handle_account_auth_status(self)
            elif self.command == "GET" and path == "/v1/account-auth/google/callback":
                backend.handle_account_auth_callback(self, query)
            elif self.command == "GET" and path == "/v1/account-auth/complete":
                html_response(self, 200, completion_html())
            elif self.command == "GET" and path == "/account-deletion":
                html_response(self, 200, account_deletion_html())
            elif self.command == "POST" and path == "/v1/account-auth/refresh":
                backend.handle_account_auth_refresh(self)
            elif self.command == "POST" and path == "/v1/account-auth/logout":
                backend.handle_account_auth_logout(self)
            elif self.command == "POST" and path == "/v1/account-auth/apple":
                backend.handle_account_auth_apple(self)
            elif self.command == "POST" and path == "/v1/account/delete":
                backend.handle_account_delete(self)
            elif self.command == "GET" and path == "/v1/account-state":
                backend.handle_account_state_get(self)
            elif self.command == "POST" and path == "/v1/account-state":
                backend.handle_account_state_save(self)
            elif self.command == "GET" and path == "/v1/whatsapp/webhook":
                backend.handle_whatsapp_webhook_verification(self, query)
            elif self.command == "POST" and path == "/v1/whatsapp/webhook":
                backend.handle_whatsapp_webhook(self)
            elif self.command == "POST" and path == "/v1/whatsapp/link":
                backend.handle_whatsapp_link_start(self)
            elif self.command == "GET" and path == "/v1/whatsapp/status":
                backend.handle_whatsapp_status(self)
            elif self.command == "POST" and path == "/v1/whatsapp/unlink":
                backend.handle_whatsapp_unlink(self)
            elif self.command == "GET" and path == "/connections/complete":
                html_response(self, 200, completion_html())
            elif path.startswith("/v1/connectors/native/setup/"):
                attempt_id = path.rsplit("/", 1)[-1]
                if self.command == "GET":
                    backend.handle_native_connector_setup(self, attempt_id)
                elif self.command == "POST":
                    backend.handle_native_connector_submit(self, attempt_id)
                else:
                    error_response(self, 405, "Metodo no permitido", "method_not_allowed")
            elif self.command == "POST" and path == "/v1/connectors/status":
                backend.handle_connector_auth_status(self)
            elif self.command == "POST" and path == "/v1/connectors/start":
                backend.handle_connector_start(self)
            elif self.command == "POST" and path == "/v1/connectors/disconnect":
                backend.handle_connector_disconnect(self)
            elif self.command == "GET" and path == "/v1/connectors":
                backend.handle_connectors_snapshot(self)
            elif self.command == "GET" and path.startswith("/v1/connectors/"):
                backend.handle_connector_status_public(self, path.rsplit("/", 1)[-1])
            elif self.command == "POST" and path == "/v1/signup":
                backend.handle_signup(self)
            elif self.command == "GET" and path == "/v1/billing":
                backend.handle_billing_status(self)
            elif self.command == "POST" and path == "/v1/billing/checkout":
                backend.handle_billing_checkout(self)
            elif self.command == "POST" and path == "/v1/billing/portal":
                backend.handle_billing_portal(self)
            elif self.command == "POST" and path == "/v1/billing/webhook":
                backend.handle_billing_webhook(self)
            elif self.command == "POST" and path == "/v1/chat/completions":
                backend.handle_proxy(self, UPSTREAM_PATHS[path])
            elif self.command == "GET" and path == "/v1/models":
                backend.handle_proxy(self, UPSTREAM_PATHS[path])
            elif self.command == "GET" and path == "/v1/usage":
                backend.handle_usage(self)
            elif self.command == "GET" and path == "/v1/credits":
                backend.handle_credits(self)
            elif self.command == "GET" and path == "/v1/me":
                backend.handle_me(self)
            elif self.command == "GET" and path == "/v1/agent/status":
                backend.handle_agent_status(self)
            elif self.command == "POST" and path == "/v1/agent/warm":
                backend.handle_agent_warm(self)
            elif self.command == "POST" and path == "/v1/agent/run":
                backend.handle_agent_run(self)
            elif self.command == "POST" and path == "/v1/agent/recover":
                backend.handle_agent_run_recover(self)
            elif self.command == "GET" and path.startswith("/v1/agent/runs/"):
                backend.handle_agent_run_status(self, path.rsplit("/", 1)[-1])
            elif path.startswith("/v1/computers/"):
                parts = path.strip("/").split("/")
                bot_id = parts[2] if len(parts) >= 3 else ""
                action = parts[3] if len(parts) == 4 else ""
                if self.command == "GET" and len(parts) == 3:
                    backend.handle_computer_status(self, bot_id)
                elif self.command == "POST" and action == "ensure":
                    backend.handle_computer_ensure(self, bot_id)
                elif self.command == "POST" and action == "hand-back":
                    backend.handle_computer_hand_back(self, bot_id)
                elif self.command == "POST" and action == "delete":
                    backend.handle_computer_delete(self, bot_id)
                else:
                    error_response(self, 405, "Operación de computadora no permitida", "method_not_allowed")
            elif self.command == "GET" and path == "/v1/internal/connectors/catalog":
                backend.handle_connector_catalog(self)
            elif self.command == "POST" and path == "/v1/internal/connectors/execute":
                backend.handle_connector_execute(self)
            elif self.command == "POST" and path == "/v1/internal/computers/execute":
                backend.handle_computer_execute(self)
            elif self.command == "GET" and path == "/admin/users":
                backend.handle_admin_users(self)
            elif self.command == "POST" and path.startswith("/admin/users/") and path.endswith("/revoke"):
                backend.handle_admin_revoke(self, path.split("/")[3])
            elif self.command == "POST" and path.startswith("/admin/users/") and path.endswith("/tier"):
                backend.handle_admin_set_tier(self, path.split("/")[3])
            elif self.command == "POST" and path.startswith("/admin/users/") and path.endswith("/credits"):
                backend.handle_admin_credits(self, path.split("/")[3])
            elif self.command == "GET" and path == "/admin/usage":
                backend.handle_admin_usage(self)
            elif self.command == "GET" and path == "/healthz":
                json_response(self, 200, {
                    "ok": True,
                    "version": __version__,
                    "build_commit": build_commit(),
                    "environment": backend.cfg.environment,
                    "liveness": True,
                })
            elif self.command == "GET" and path == "/platformz":
                database = backend.store.health()
                platform_ready = bool(database.get("ready"))
                json_response(self, 200 if platform_ready else 503, {
                    "ok": platform_ready,
                    "version": __version__,
                    "build_commit": build_commit(),
                    "environment": backend.cfg.environment,
                    "database_ready": platform_ready,
                })
            elif self.command == "GET" and path == "/readyz":
                readiness = backend.readiness()
                response = {
                    "ok": readiness["ready"],
                    "version": __version__,
                    "build_commit": build_commit(),
                    "environment": backend.cfg.environment,
                    "ready": readiness["ready"],
                    "checks": readiness["checks"],
                    "stripe_live_mode": backend.billing.config.live_mode if backend.billing.configured else None,
                }
                if backend.cfg.environment != "production":
                    response.update({
                        key: value for key, value in readiness.items()
                        if key not in {"ready", "checks"}
                    })
                json_response(self, 200 if readiness["ready"] else 503, response)
            else:
                error_response(self, 404, f"No existe {self.command} {path}", "not_found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except RequestBodyTooLarge as e:
            error_response(self, 413, str(e), "body_too_large")
        except RequestBodyError as e:
            error_response(self, 400, str(e), "bad_body")
        except GoogleAuthError as e:
            error_response(self, e.status, str(e), e.code)
        except AppleAuthError as e:
            error_response(self, e.status, str(e), e.code)
        except BillingError as e:
            error_response(self, e.status, str(e), e.code)
        except ConnectorBrokerError as e:
            error_response(self, e.status, str(e), e.code)
        except ComputerError as e:
            error_response(self, e.status, str(e), e.code)
        except Exception:  # noqa: BLE001 - nunca dejar colgar al cliente
            logging.exception(
                "Unhandled request error request_id=%s method=%s",
                self.request_id,
                self.command,
            )
            try:
                error_response(
                    self,
                    500,
                    "Internal server error",
                    "internal_error",
                    include_request_id=True,
                )
            except Exception:
                self.close_connection = True
        finally:
            lock = self.agent_conversation_lock
            self.agent_conversation_lock = None
            if lock is not None:
                lock.release()

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound the number of active request threads instead of growing without limit."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, max_workers: int = 32, **kwargs):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            payload = json.dumps({
                "error": {
                    "message": "El servidor está temporalmente ocupado",
                    "type": "server_busy",
                }
            }, separators=(",", ":")).encode("utf-8")
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\n"
                b"Retry-After: 2\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
                + payload
            )
            try:
                request.sendall(response)
            except OSError:
                pass
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def serve(cfg: Config) -> None:
    validate_runtime_security(cfg)
    if not cfg.admin_token:
        cfg.admin_token = secrets.token_hex(32)
        logging.warning("ADMIN_TOKEN efímero generado para desarrollo; no se imprimirá")
    backend = Backend(cfg)
    Handler.backend = backend
    httpd = BoundedThreadingHTTPServer((cfg.host, cfg.port), Handler)
    print(f"[server] wrapper backend v{__version__} escuchando en http://{cfg.host}:{cfg.port}")
    print(f"[server] model provider: DeepSeek ({cfg.deepseek_base_url})")
    database_backend = "postgres" if cfg.database_url else f"sqlite:{cfg.db_path}"
    print(f"[server] enforce_limits={cfg.enforce_limits} database={database_backend}")
    print(f"[server] pi_enabled={cfg.pi_enabled} pi_model={cfg.pi_model}")
    print(f"[server] computers={backend.computers.health()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] deteniendo...")
    finally:
        httpd.server_close()
        backend.pi.close()
        backend.store.close()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="wrapper-backend", description="Backend de Agent Genia sobre DeepSeek")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init-db", help="Crear la base de datos")
    serve_cmd = sub.add_parser("serve", help="Arrancar el servidor HTTP")
    serve_cmd.add_argument("--port", type=int, default=None)
    sub.add_parser("users", help="Listar usuarios")
    sub.add_parser("usage", help="Ver eventos de uso")

    args = parser.parse_args()
    cfg = Config()
    if args.cmd == "serve" and args.port is not None:
        cfg.port = args.port
        if "PI_BACKEND_URL" not in os.environ:
            cfg.pi_backend_url = f"http://127.0.0.1:{cfg.port}"
    if args.cmd == "serve":
        try:
            serve(cfg)
        except UnsafeConfigurationError as exc:
            print(f"[config] {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        return

    backend = Backend(cfg)
    if args.cmd == "init-db":
        location = "Supabase/Postgres" if cfg.database_url else str(cfg.db_path)
        print(f"[ok] base de datos en {location}")
    elif args.cmd == "users":
        for u in backend.store.list_users():
            print(u["id"], "|", u.get("name") or "-", "|", u.get("email") or "-",
                  "| tier:", u.get("tier") or "-")
    elif args.cmd == "usage":
        data = backend.store.usage_all()
        for e in data["events"]:
            print(f"{e['created_at']:.0f} | {e['user_id']} | {e['model'] or '-'} | {e['endpoint']} | "
                  f"in={e['input_tokens']} out={e['output_tokens']} | ${e['estimated_cost_usd']:.6f} | {e['status']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
