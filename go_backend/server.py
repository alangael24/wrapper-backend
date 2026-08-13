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
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .apple_auth import AppleAccountAuth, AppleAuthError
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
from .connectors import CONNECTOR_CATALOG, ConnectorBroker, ConnectorBrokerError
from .computers import ComputerConfig, ComputerError, ComputerManager
from .credits import CreditConfig, CreditService, credits_float
from .deepseek_prices import estimate_cost_microusd
from .google_auth import GoogleAccountAuth, GoogleAuthError, completion_html
from .opencode_prices import estimate_cost_microusd as estimate_opencode_cost_microusd
from .pi_harness import (
    CHROME_ISOLATION_PER_RUN,
    PiHarness,
    PiHarnessBusy,
    PiHarnessError,
    PiHarnessTimeout,
    PiHarnessUsageError,
)
from .postgres_store import create_store
from .store import hash_agent_run_token
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
MAX_BODY = 160 * 1024 * 1024  # 160 MB
MAX_JSON_BODY = 1024 * 1024
MAX_STRIPE_WEBHOOK_BODY = 1024 * 1024  # Los eventos Stripe normales son mucho menores.
UNSAFE_ADMIN_TOKENS = frozenset({"cambia-este-token"})
JSON_TEXT_FIELD_RE = re.compile(r'"text"\s*:\s*"')

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
        self.pi_thinking = os.environ.get("PI_THINKING", "high")
        self.pi_timeout_seconds = int(os.environ.get("PI_TIMEOUT_SECONDS", "1800"))
        self.pi_max_concurrent = int(os.environ.get("PI_MAX_CONCURRENT", "4"))
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


def json_response(handler: BaseHTTPRequestHandler, status: int, obj) -> None:
    body = json.dumps(obj).encode()
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
        self._write_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()

    def start(self, run_id: str) -> None:
        self.handler.close_connection = True
        self.handler.send_response(200)
        self.handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.handler.send_header("Cache-Control", "no-store, no-cache, max-age=0")
        self.handler.send_header("Pragma", "no-cache")
        self.handler.send_header("X-Accel-Buffering", "no")
        self.handler.send_header("Connection", "close")
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
        if self.disconnected:
            return
        try:
            with self._write_lock:
                self.handler.wfile.write(frame)
                self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.disconnected = True
            self._heartbeat_stop.set()

    def send(self, event: str, payload: dict) -> None:
        if self.disconnected:
            return
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        frame = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        self._write(frame)

    def model_delta(self, delta: str) -> None:
        self.raw_answer += delta
        visible = _partial_json_text(self.raw_answer)
        if visible is None or len(visible) <= len(self.visible_sent):
            return
        next_delta = visible[len(self.visible_sent):]
        self.visible_sent = visible
        self.send("delta", {"text": next_delta})

    def error(self, status: int, message: str, code: str) -> None:
        self.send("error", {"status": status, "message": message, "type": code})
        self._heartbeat_stop.set()

    def done(self, payload: dict) -> None:
        self.send("done", payload)
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


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._run_timing_lock = threading.Lock()
        self._run_timings: dict[str, dict[str, float]] = {}
        self._run_provider_lock = threading.Lock()
        self._run_providers: dict[str, dict[str, Any]] = {}
        validate_runtime_security(cfg)
        validate_pi_chrome_security(cfg.pi_chrome_isolation)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.secret_file.parent.mkdir(parents=True, exist_ok=True)
        self.store = create_store(database_url=cfg.database_url, db_path=cfg.db_path)
        self.credits = CreditService(self.store, cfg.credits)
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
        self._signup_rate: dict[str, deque[float]] = defaultdict(deque)
        self._signup_rate_lock = threading.RLock()
        self.connectors = ConnectorBroker(
            default_ttl_seconds=cfg.pi_connector_token_ttl_seconds
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

    def _start_run_timing(self, run_id: str, started_at: float) -> None:
        with self._run_timing_lock:
            self._run_timings[run_id] = {"_origin": started_at}

    def _mark_run_timing(self, run_id: str, name: str) -> None:
        with self._run_timing_lock:
            timing = self._run_timings.get(run_id)
            if timing is None or name in timing:
                return
            timing[name] = round((time.monotonic() - timing["_origin"]) * 1000, 3)

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

    def bearer(self, handler: BaseHTTPRequestHandler) -> str | None:
        auth = handler.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def require_user(self, handler: BaseHTTPRequestHandler) -> dict | None:
        key = self.bearer(handler)
        if not key:
            error_response(handler, 401, "Falta Authorization: Bearer <api_key>", "unauthorized")
            return None
        user = self.store.get_user_by_api_key(key) or self.google_auth.authenticate(key)
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
        run = self.store.get_agent_run_by_token(key)
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
        user = self.store.get_user_by_api_key(key) or self.google_auth.authenticate(key)
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

    # ---------- connector accounts ----------
    def handle_connectors_snapshot(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        json_response(
            handler,
            200,
            {"connectors": self.connector_gateway.snapshot(user["id"])},
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
        json_response(handler, 200, self.connector_gateway.poll(user["id"], attempt_id))

    def handle_connector_disconnect(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        connector_id = body.get("connector_id")
        if not isinstance(connector_id, str):
            raise ConnectorBrokerError(400, "Falta connector_id", "bad_connector")
        json_response(handler, 200, self.connector_gateway.disconnect(user["id"], connector_id))

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
        now = time.monotonic()
        with self._signup_rate_lock:
            bucket = self._signup_rate[remote_key]
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= 20:
                raise GoogleAuthError(
                    "Demasiadas altas. Espera un minuto.", status=429, code="rate_limit"
                )
            bucket.append(now)

    def readiness(self) -> dict:
        database = self.store.health()
        pi = self.pi.status()
        pi.pop("binary", None)
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
            and pi.get("browser_isolation") == CHROME_ISOLATION_PER_RUN,
            "model_provider": bool(self.cfg.deepseek_api_key),
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
        if run_id:
            self._mark_run_timing(run_id, "proxy_received_ms")
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
                self._inspect_upstream_delta_timing(run_id, chunk)
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

    def _inspect_upstream_delta_timing(self, run_id: str, chunk: bytes) -> None:
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
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._mark_run_timing(run_id, "upstream_first_content_ms")
        if delta.get("tool_calls"):
            self._mark_run_timing(run_id, "upstream_first_tool_call_ms")

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
        json_response(handler, 200, status)

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
            result = self.pi.prewarm(
                conversation_key=f"{user['id']}\0{bot_id.strip()}",
            )
        except ModelProviderUnavailable as exc:
            error_response(handler, 503, str(exc), "model_unavailable")
            return
        except PiHarnessBusy as exc:
            error_response(handler, 429, str(exc), "pi_busy")
            return
        except PiHarnessTimeout as exc:
            error_response(handler, 504, str(exc), "pi_warm_timeout")
            return
        except PiHarnessError as exc:
            error_response(handler, 502, str(exc), "pi_warm_error")
            return
        json_response(handler, 200, result)

    def handle_agent_run(self, handler: BaseHTTPRequestHandler) -> None:
        request_started_at = time.monotonic()
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        unlimited = self.unlimited_usage(user)
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
        if not isinstance(computer_requested, bool):
            error_response(handler, 400, "computer debe ser true o false", "bad_computer")
            return
        if not isinstance(stream_requested, bool):
            error_response(handler, 400, "stream debe ser true o false", "bad_stream")
            return
        if computer_requested and (not isinstance(bot_id, str) or not bot_id):
            error_response(handler, 400, "bot_id es obligatorio para usar una computadora", "bad_bot_id")
            return
        if bot_id is not None and (
            not isinstance(bot_id, str)
            or not bot_id.strip()
            or len(bot_id.strip()) > 200
        ):
            error_response(handler, 400, "bot_id no es válido", "bad_bot_id")
            return
        bot_id = bot_id.strip() if isinstance(bot_id, str) else None
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
            connector_ids = self.connectors.normalize_connector_ids(connector_ids_value)
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return

        pi_status = self.pi.status()
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
        computer_enabled = bool(computer_requested and self.computers.configured)
        if (connector_ids or computer_enabled) and not pi_status["connectors_available"]:
            error_response(
                handler,
                409,
                "La extension TypeScript de conectores no esta instalada",
                "pi_connectors_unavailable",
            )
            return

        run_api_key = "agrn_" + secrets.token_urlsafe(48)
        try:
            plan = plan_for(tier)
            prepared = self.store.create_agent_run(
                user_id=user["id"],
                idempotency_key=idempotency_key,
                model=self.cfg.pi_model,
                browser=browser,
                max_credit_milli=max_credit_milli,
                max_concurrent_runs=(
                    self.cfg.pi_max_concurrent if unlimited else plan.max_concurrent_runs
                ),
                five_hour_credit_milli=plan.five_hour_credit_milli,
                seven_day_credit_milli=plan.seven_day_credit_milli,
                token_hash=hash_agent_run_token(run_api_key),
                token_expires_at=time.time() + self.cfg.credits.reservation_ttl_seconds,
                enforce=self.cfg.credits.mode == "enforce" and not unlimited,
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
            error_response(
                handler, 409,
                f"La ejecución ya existe: {existing['id']} ({existing['status']})",
                "run_already_exists",
            )
            return
        run = prepared["run"]
        run_id = run["id"]
        started_at = time.monotonic()
        self._start_run_timing(run_id, request_started_at)
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
                warnings=[
                    "timing:" + json.dumps(
                        self._run_timing_snapshot(run_id), separators=(",", ":")
                    )
                ],
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
            if connector_ids or computer_enabled:
                connector_run_token = self.connectors.issue(
                    user_id=user["id"],
                    connector_ids=connector_ids,
                    computer_id=bot_id if computer_enabled else None,
                )
            self.store.mark_agent_run_running(run_id)
            self._mark_run_timing(run_id, "pi_dispatch_ms")

            def on_text_delta(delta: str) -> None:
                self._mark_run_timing(run_id, "pi_first_text_ms")
                if event_stream:
                    event_stream.model_delta(delta)

            result = self.pi.run(
                run_id=run_id,
                run_api_key=run_api_key,
                prompt=prompt,
                browser=browser,
                connector_run_token=connector_run_token,
                conversation_key=(
                    f"{user['id']}\0{bot_id}" if bot_id is not None else None
                ),
                on_text_delta=on_text_delta,
            )
            self._mark_run_timing(run_id, "pi_complete_ms")
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
        settled, credits = settle("succeeded")
        payload = result.as_dict()
        payload["run_id"] = run_id
        payload["status"] = settled["status"]
        payload["credits"] = credits
        payload["usage"].update({
            "llm_cost_microusd": int(settled["llm_cost_microusd"]),
            "llm_cost_usd": round(int(settled["llm_cost_microusd"]) / 1_000_000, 6),
            "extra_cost_microusd": int(settled["extra_cost_microusd"]),
            "duration_seconds": float(settled["duration_seconds"] or 0),
        })
        payload["connector_ids"] = list(connector_ids)
        payload["computer_enabled"] = computer_enabled
        self._mark_run_timing(run_id, "response_ready_ms")
        payload["timings"] = self._run_timing_snapshot(run_id)
        if event_stream:
            event_stream.done(payload)
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
        try:
            result = self.connectors.execute(
                token=token,
                connector_id=body.get("connector_id"),
                operation=body.get("operation"),
                arguments=body.get("arguments", {}),
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
        result = self.computers.execute(
            user_id=user_id,
            bot_id=bot_id,
            operation=body.get("operation"),
            arguments=body.get("arguments", {}),
        )
        json_response(handler, 200, result)

    # ---------- body helpers ----------
    def read_body(self, handler: BaseHTTPRequestHandler, *, max_bytes: int = MAX_BODY) -> bytes:
        transfer_encoding = handler.headers.get("transfer-encoding", "").lower()
        if "chunked" in (part.strip() for part in transfer_encoding.split(",")):
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
        length = handler.headers.get("content-length")
        if length and length.isdigit():
            n = int(length)
            if n > max_bytes:
                handler.close_connection = True
                raise RequestBodyTooLarge(f"Body mayor a {max_bytes} bytes")
            return handler.rfile.read(n)
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
                    "environment": backend.cfg.environment,
                    "liveness": True,
                })
            elif self.command == "GET" and path == "/platformz":
                database = backend.store.health()
                platform_ready = bool(database.get("ready"))
                json_response(self, 200 if platform_ready else 503, {
                    "ok": platform_ready,
                    "version": __version__,
                    "environment": backend.cfg.environment,
                    "database_ready": platform_ready,
                })
            elif self.command == "GET" and path == "/readyz":
                readiness = backend.readiness()
                response = {
                    "ok": readiness["ready"],
                    "version": __version__,
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

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()


def serve(cfg: Config) -> None:
    validate_runtime_security(cfg)
    if not cfg.admin_token:
        cfg.admin_token = secrets.token_hex(32)
        logging.warning("ADMIN_TOKEN efímero generado para desarrollo; no se imprimirá")
    backend = Backend(cfg)
    Handler.backend = backend
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
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
