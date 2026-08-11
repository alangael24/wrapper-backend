"""Servidor HTTP del wrapper backend.

Endpoints publicos (Bearer = api key del usuario del wrapper):
  POST /v1/account-auth/start    Iniciar login con Google
  GET  /v1/account-auth/status  Consultar login desde Electron
  GET  /v1/account-auth/google/callback
  POST /v1/account-auth/refresh  Rotar la sesión del dispositivo
  POST /v1/account-auth/logout   Revocar la sesión actual
  POST /v1/signup          Crear usuario free (los tiers pagados requieren admin/pago verificado)
  POST /v1/byok            El usuario registra su propia key de Go
  GET  /v1/models          Catalogo de modelos (proxy a Go)
  POST /v1/chat/completions
  POST /v1/responses
  POST /v1/messages
  GET  /v1/agent/status    Estado del harness de Pi
  POST /v1/agent/run       Ejecutar una tarea con Pi
  GET  /v1/usage           Uso por ventanas con limites ajustados al tier
  GET  /v1/me

Endpoints admin (Bearer ADMIN_TOKEN):
  POST /admin/subscriptions    Agregar keys de Go al pool
  GET  /admin/subscriptions    Listar pool (keys enmascaradas)
  GET  /admin/users
  POST /admin/users/<id>/revoke
  POST /admin/users/<id>/tier  Cambiar tier (asigna/libera sub del pool)
  GET  /admin/usage

CLI:
  python3 -m go_backend.server init-db
  python3 -m go_backend.server serve [--port N]
  python3 -m go_backend.server add-key <sk-...> [...]   (o "-" para leer stdin)
  python3 -m go_backend.server users | subs | usage
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .crypto_utils import decrypt_api_key, encrypt_api_key, hash_wrapper_key
from .connectors import ConnectorBroker, ConnectorBrokerError
from .go_prices import estimate_cost_usd
from .google_auth import GoogleAccountAuth, GoogleAuthError, completion_html
from .pi_harness import (
    CHROME_ISOLATION_PER_RUN,
    PiHarness,
    PiHarnessBusy,
    PiHarnessError,
)
from .tiers import DEFAULT_TIER, effective_limits, is_valid, requires_subscription, tier_label
from .store import NoSubscriptionAvailable, Store, new_id
from .upstream import DEFAULT_UA, proxy_request
from .vision import VisionError, VisionRouter

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "wrapper.sqlite"
DEFAULT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "secret.key"
DEFAULT_PI_RUNS = Path(__file__).resolve().parent.parent / "data" / "pi-runs"
DEFAULT_PI_BIN = Path(__file__).resolve().parent.parent / "node_modules" / ".bin" / "pi"
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
DEFAULT_GO_BASE = "https://opencode.ai/zen/go/v1"
MAX_BODY = 160 * 1024 * 1024  # 160 MB
UNSAFE_ADMIN_TOKENS = frozenset({"cambia-este-token"})

# Rutas expuestas por el wrapper -> rutas relativas al upstream de Go
UPSTREAM_PATHS = {
    "/v1/chat/completions": "/chat/completions",
    "/v1/responses": "/responses",
    "/v1/messages": "/messages",
    "/v1/models": "/models",
}


def masked(key: str) -> str:
    if len(key) <= 12:
        return "***"
    return key[:4] + "..." + key[-4:]


class RequestBodyError(ValueError):
    pass


class RequestBodyTooLarge(RequestBodyError):
    pass


class UnsafeConfigurationError(RuntimeError):
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
        self.db_path = Path(os.environ.get("DB_PATH", str(DEFAULT_DB)))
        self.secret_file = Path(os.environ.get("SECRET_FILE", str(DEFAULT_SECRET_FILE)))
        self.go_base_url = os.environ.get("GO_BASE_URL", DEFAULT_GO_BASE).rstrip("/")
        self.port = int(os.environ.get("PORT", "8787"))
        self.enforce_limits = os.environ.get("ENFORCE_LIMITS", "1") != "0"
        self.wrapper_secret = os.environ.get("WRAPPER_SECRET") or None
        self.admin_token = (os.environ.get("ADMIN_TOKEN") or "").strip() or None
        self.google_oauth_client_id = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip() or None
        self.google_oauth_client_secret = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip() or None
        self.google_oauth_redirect_uri = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip() or None
        self.account_access_ttl_seconds = int(os.environ.get("ACCOUNT_ACCESS_TTL_SECONDS", "900"))
        self.account_refresh_ttl_seconds = int(os.environ.get("ACCOUNT_REFRESH_TTL_SECONDS", str(30 * 86400)))
        self.account_auth_attempt_ttl_seconds = int(os.environ.get("ACCOUNT_AUTH_ATTEMPT_TTL_SECONDS", "600"))
        self.vision_enabled = os.environ.get("VISION_ENABLED", "1") != "0"
        self.vision_model = os.environ.get("VISION_MODEL", "gpt-5.6-luna")
        self.vision_fallback_model = os.environ.get("VISION_FALLBACK_MODEL", "mimo-v2.5") or None
        self.vision_target_models = tuple(
            value.strip()
            for value in os.environ.get("VISION_TARGET_MODELS", "deepseek-v4").split(",")
            if value.strip()
        )
        self.vision_max_output_tokens = int(os.environ.get("VISION_MAX_OUTPUT_TOKENS", "2048"))
        self.vision_fallback_max_output_tokens = int(
            os.environ.get("VISION_FALLBACK_MAX_OUTPUT_TOKENS", "4096")
        )
        self.vision_reasoning_effort = os.environ.get("VISION_REASONING_EFFORT", "minimal")
        self.vision_report_limit = int(os.environ.get("VISION_REPORT_LIMIT", "8000"))
        self.vision_cache_entries = int(os.environ.get("VISION_CACHE_ENTRIES", "128"))
        self.vision_max_groups = int(os.environ.get("VISION_MAX_GROUPS", "6"))
        self.vision_max_images = int(os.environ.get("VISION_MAX_IMAGES", "12"))
        self.pi_enabled = os.environ.get("PI_ENABLED", "0") == "1"
        self.pi_bin = os.environ.get("PI_BIN", str(DEFAULT_PI_BIN))
        self.pi_backend_url = os.environ.get(
            "PI_BACKEND_URL", f"http://127.0.0.1:{self.port}"
        ).rstrip("/")
        self.pi_runs_dir = Path(os.environ.get("PI_RUNS_DIR", str(DEFAULT_PI_RUNS)))
        self.pi_model = os.environ.get("PI_MODEL", "deepseek-v4-flash")
        self.pi_thinking = os.environ.get("PI_THINKING", "high")
        self.pi_timeout_seconds = int(os.environ.get("PI_TIMEOUT_SECONDS", "1800"))
        self.pi_max_concurrent = int(os.environ.get("PI_MAX_CONCURRENT", "2"))
        self.pi_max_prompt_chars = int(os.environ.get("PI_MAX_PROMPT_CHARS", "100000"))
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


def json_response(handler: BaseHTTPRequestHandler, status: int, obj) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: BaseHTTPRequestHandler, status: int, message: str, code: str | None = None) -> None:
    json_response(handler, status, {"error": {"message": message, "type": code or "error"}})


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


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        validate_pi_chrome_security(cfg.pi_chrome_isolation)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.secret_file.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg.db_path)
        self.google_auth = GoogleAccountAuth(
            store=self.store,
            client_id=cfg.google_oauth_client_id,
            client_secret=cfg.google_oauth_client_secret,
            redirect_uri=cfg.google_oauth_redirect_uri,
            access_ttl_seconds=cfg.account_access_ttl_seconds,
            refresh_ttl_seconds=cfg.account_refresh_ttl_seconds,
            attempt_ttl_seconds=cfg.account_auth_attempt_ttl_seconds,
        )
        self.connectors = ConnectorBroker(
            default_ttl_seconds=cfg.pi_connector_token_ttl_seconds
        )
        self.vision = VisionRouter(
            enabled=cfg.vision_enabled,
            base_url=cfg.go_base_url,
            primary_model=cfg.vision_model,
            fallback_model=cfg.vision_fallback_model,
            target_model_prefixes=cfg.vision_target_models,
            max_output_tokens=cfg.vision_max_output_tokens,
            fallback_max_output_tokens=cfg.vision_fallback_max_output_tokens,
            reasoning_effort=cfg.vision_reasoning_effort,
            report_limit=cfg.vision_report_limit,
            cache_entries=cfg.vision_cache_entries,
            max_groups=cfg.vision_max_groups,
            max_images=cfg.vision_max_images,
        )
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
            supports_images=self.vision.supports_model(cfg.pi_model),
            node_bin_dir=cfg.pi_node_bin_dir,
            connector_extension=cfg.pi_connector_extension,
            connector_broker_url=cfg.pi_backend_url,
            chrome_extension=cfg.pi_chrome_extension,
            chrome_auto_authorize=cfg.pi_chrome_auto_authorize,
            chrome_authorize_minutes=cfg.pi_chrome_authorize_minutes,
            chrome_binary=cfg.pi_chrome_bin,
            chrome_isolation=cfg.pi_chrome_isolation,
        )

    # ---------- auth helpers ----------
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

    def handle_account_auth_status(
        self, handler: BaseHTTPRequestHandler, attempt_id: str, query: dict[str, list[str]]
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

    def handle_signup(self, handler: BaseHTTPRequestHandler) -> None:
        body = self.read_json(handler) or {}
        name = body.get("name")
        email = body.get("email")
        # El cliente nunca decide su tier. Los upgrades pagados solo pueden
        # ocurrir mediante un webhook de pago verificado o el endpoint admin.
        tier = DEFAULT_TIER
        api_key = secrets.token_hex(32)
        user = self.store.create_user(api_key, name, email, tier=tier)
        json_response(handler, 201, {
            "api_key": api_key,
            "user_id": user["id"],
            "name": user["name"],
            "tier": tier,
            "tier_label": tier_label(tier),
            "limits": effective_limits(tier),
            "subscription_id": None,
            "subscription_status": "none",
            "available_left": self.store.available_count(),
            "note": (
                "Guarda el api_key; no se puede volver a mostrar. La cuenta inicia en free; "
                "basic/pro se activan exclusivamente después de verificar el pago."
            ),
        })

    def handle_byok(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        body = self.read_json(handler) or {}
        go_key = (body.get("apiKey") or "").strip()
        if not go_key:
            error_response(handler, 400, "Falta apiKey", "bad_request")
            return
        # Validacion best-effort contra el upstream
        ok, message = self.validate_go_key(go_key)
        if ok is False:
            error_response(handler, 400, f"Key de Go rechazada por el upstream: {message}", "invalid_go_key")
            return
        sub_id = new_id("sub")
        blob = encrypt_api_key(go_key, sub_id, self.cfg.wrapper_secret, self.cfg.secret_file)
        self.store.add_subscription(blob, sub_id, label=f"byok-{user['id']}", source="byok", sub_id=sub_id)
        self.store.update_user_subscription(user["id"], sub_id)
        json_response(handler, 201, {
            "subscription_id": sub_id,
            "source": "byok",
            "validated": ok is True,
        })

    def validate_go_key(self, go_key: str) -> tuple[bool | None, str]:
        try:
            import urllib.request

            req = urllib.request.Request(self.cfg.go_base_url + "/models")
            req.add_header("Authorization", f"Bearer {go_key}")
            req.add_header("User-Agent", DEFAULT_UA)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status == 200, f"http {resp.status}"
            except Exception as e:
                code = getattr(e, "code", None)
                if code in (401, 403):
                    return False, f"http {code}"
                return None, f"network error: {e}"
        except Exception as e:
            return None, str(e)

    # ---------- proxy ----------
    def handle_proxy(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        if not requires_subscription(tier):
            error_response(
                handler, 402,
                f"El tier '{tier}' ({tier_label(tier)}) no incluye acceso a modelos. "
                "Cambia a basic o pro para usar el LLM.",
                "tier_requires_upgrade",
            )
            return
        sub = self.store.get_subscription(user["subscription_id"]) if user["subscription_id"] else None
        if not sub or sub["status"] != "assigned":
            error_response(handler, 402, "El usuario no tiene una suscripcion de Go asignada", "no_subscription")
            return
        if self.cfg.enforce_limits:
            summary = self.store.usage_summary(user["id"], sub["id"], tier)["windows"]
            hit = next((k for k, v in summary.items() if v["spent_usd"] >= v["limit_usd"]), None)
            if hit:
                error_response(
                    handler, 429,
                    f"Limite de uso alcanzado ({hit}: ${summary[hit]['spent_usd']:.2f} de "
                    f"${summary[hit]['limit_usd']:.2f}). Espera a que se renueve o usa /v1/byok.",
                    "usage_limit",
                )
                return
        try:
            go_key = decrypt_api_key(sub["api_key_enc"], sub["key_id"], self.cfg.wrapper_secret, self.cfg.secret_file)
        except Exception as e:
            error_response(handler, 500, f"No se pudo descifrar la key de Go: {e}", "crypto_error")
            return

        body = self.read_body(handler)
        vision_models: tuple[str, ...] = ()
        if body and handler.command == "POST":
            try:
                vision_result = self.vision.transform(path, body, go_key)
            except VisionError as e:
                for analysis in e.analyses:
                    self.record(user, sub, analysis.path, analysis.status, analysis.usage)
                error_response(
                    handler,
                    e.status,
                    f"No se pudo analizar la imagen: {e}",
                    e.code,
                )
                return
            body = vision_result.body
            vision_models = vision_result.models
            for analysis in vision_result.analyses:
                self.record(user, sub, analysis.path, analysis.status, analysis.usage)

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
            stream_state["started"] = True
            try:
                handler.send_response(status)
                for k, v in out_headers.items():
                    if k.lower() in ("content-length", "transfer-encoding"):
                        continue
                    handler.send_header(k, v)
                if vision_models:
                    handler.send_header("X-Wrapper-Vision-Model", ",".join(vision_models))
                handler.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def on_chunk(chunk: bytes) -> None:
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        status, out_headers, out_body, usage = proxy_request(
            handler.command, self.cfg.go_base_url, path, headers, body, go_key,
            on_chunk=on_chunk, on_headers=on_headers,
        )
        self.record(user, sub, path, status, usage)

        if stream_state["started"]:
            handler.close_connection = True
            return

        # No-stream: responder con el body crudo del upstream
        raw = out_body or b""
        try:
            handler.send_response(status)
            for k, v in out_headers.items():
                if k.lower() in ("content-length", "transfer-encoding"):
                    continue
                handler.send_header(k, v)
            if vision_models:
                handler.send_header("X-Wrapper-Vision-Model", ",".join(vision_models))
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            if raw:
                handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def record(self, user, sub, path, status, usage) -> None:
        model = usage.model
        cost = estimate_cost_usd(model, usage.input_tokens, usage.output_tokens,
                                 usage.cached_read, usage.cached_write) if usage.any() else 0.0
        self.store.record_usage(
            user["id"], sub["id"], model, path,
            usage.input_tokens if usage.any() else None,
            usage.output_tokens if usage.any() else None,
            usage.cached_read if usage.any() else None,
            usage.cached_write if usage.any() else None,
            cost, status,
        )

    # ---------- Pi agent harness ----------
    def handle_agent_status(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_user(handler):
            return
        status = self.pi.status()
        status.pop("binary", None)  # no exponer rutas internas del servidor
        status["vision"] = self.vision.status()
        json_response(handler, 200, status)

    def handle_agent_run(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        if not requires_subscription(tier):
            error_response(
                handler, 402,
                f"El tier '{tier}' no incluye ejecuciones de Pi",
                "tier_requires_upgrade",
            )
            return
        sub = self.store.get_subscription(user["subscription_id"]) if user["subscription_id"] else None
        if not sub or sub["status"] != "assigned":
            error_response(handler, 402, "El usuario no tiene una suscripcion de Go asignada", "no_subscription")
            return

        body = self.read_json(handler) or {}
        prompt = body.get("prompt")
        browser = body.get("browser", False)
        connector_ids_value = body.get("connector_ids", [])
        if not isinstance(prompt, str) or not prompt.strip():
            error_response(handler, 400, "Envia un prompt de texto no vacio", "bad_prompt")
            return
        if not isinstance(browser, bool):
            error_response(handler, 400, "browser debe ser true o false", "bad_browser")
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
        if connector_ids and not pi_status["connectors_available"]:
            error_response(
                handler,
                409,
                "La extension TypeScript de conectores no esta instalada",
                "pi_connectors_unavailable",
            )
            return

        api_key = self.bearer(handler)
        assert api_key is not None
        connector_run_token = (
            self.connectors.issue(user_id=user["id"], connector_ids=connector_ids)
            if connector_ids
            else None
        )
        try:
            result = self.pi.run(
                user_api_key=api_key,
                prompt=prompt,
                browser=browser,
                connector_run_token=connector_run_token,
            )
        except PiHarnessBusy as e:
            error_response(handler, 429, str(e), "pi_busy")
            return
        except PiHarnessError as e:
            error_response(handler, 502, str(e), "pi_error")
            return
        finally:
            self.connectors.revoke(connector_run_token)
        payload = result.as_dict()
        payload["connector_ids"] = list(connector_ids)
        json_response(handler, 200, payload)

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
        except ConnectorBrokerError as e:
            error_response(handler, e.status, str(e), e.code)
            return
        json_response(handler, 200, {"connectors": connectors})

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

    # ---------- body helpers ----------
    def read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
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
                if total > MAX_BODY:
                    handler.close_connection = True
                    raise RequestBodyTooLarge(f"Body mayor a {MAX_BODY} bytes")
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
            if n > MAX_BODY:
                handler.close_connection = True
                raise RequestBodyTooLarge(f"Body mayor a {MAX_BODY} bytes")
            return handler.rfile.read(n)
        return b""

    def read_json(self, handler: BaseHTTPRequestHandler) -> dict | None:
        body = self.read_body(handler)
        if not body:
            return None
        try:
            return json.loads(body)
        except Exception:
            return None

    # ---------- admin ----------
    def handle_admin_add_subscriptions(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_admin(handler):
            return
        body = self.read_json(handler) or {}
        keys = body.get("keys") or ([body["key"]] if body.get("key") else [])
        if not keys:
            error_response(handler, 400, "Envia {keys: [...]} o {key: ...}", "bad_request")
            return
        created = []
        for raw in keys:
            k = (raw or "").strip()
            if not k:
                continue
            sub_id = new_id("sub")
            blob = encrypt_api_key(k, sub_id, self.cfg.wrapper_secret, self.cfg.secret_file)
            sub = self.store.add_subscription(blob, sub_id, label=body.get("label"), sub_id=sub_id)
            created.append({"id": sub["id"], "status": sub["status"], "key_masked": masked(k)})
        json_response(handler, 201, {"created": created, "available_left": self.store.available_count()})

    def handle_admin_list_subscriptions(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_admin(handler):
            return
        subs = []
        for s in self.store.list_subscriptions():
            subs.append({
                "id": s["id"], "status": s["status"], "source": s["source"],
                "assigned_user_id": s["assigned_user_id"], "label": s["label"],
                "api_key_enc_length": len(s["api_key_enc"]),
            })
        json_response(handler, 200, {"subscriptions": subs})

    def handle_admin_users(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_admin(handler):
            return
        users = []
        for u in self.store.list_users():
            users.append({
                "id": u["id"], "name": u["name"], "email": u["email"],
                "tier": u.get("tier") or DEFAULT_TIER,
                "subscription_id": u["subscription_id"], "created_at": u["created_at"],
            })
        json_response(handler, 200, {"users": users})

    def handle_admin_revoke(self, handler: BaseHTTPRequestHandler, user_id: str) -> None:
        if not self.require_admin(handler):
            return
        user = self.store.get_user_by_id(user_id)
        if not user:
            error_response(handler, 404, "Usuario no encontrado", "not_found")
            return
        self.store.transition_user_tier(user_id, "free", needs_subscription=False)
        json_response(handler, 200, {"revoked": True, "user_id": user_id})

    def handle_admin_set_tier(self, handler: BaseHTTPRequestHandler, user_id: str) -> None:
        if not self.require_admin(handler):
            return
        user = self.store.get_user_by_id(user_id)
        if not user:
            error_response(handler, 404, "Usuario no encontrado", "not_found")
            return
        body = self.read_json(handler) or {}
        tier = str(body.get("tier") or "").lower()
        if not is_valid(tier):
            error_response(handler, 400, f"Tier invalido: {tier}", "bad_tier")
            return
        try:
            updated = self.store.transition_user_tier(
                user_id,
                tier,
                needs_subscription=requires_subscription(tier),
            )
        except NoSubscriptionAvailable:
            error_response(
                handler,
                409,
                "No hay suscripciones disponibles para asignar al subir de tier",
                "no_subscriptions_available",
            )
            return
        json_response(handler, 200, {
            "user_id": user_id,
            "tier": tier,
            "subscription_id": updated["subscription_id"],
        })

    def handle_admin_usage(self, handler: BaseHTTPRequestHandler) -> None:
        if not self.require_admin(handler):
            return
        json_response(handler, 200, self.store.usage_all())

    # ---------- usage/me ----------
    def handle_usage(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        summary = self.store.usage_summary(user["id"], user["subscription_id"], tier)
        summary["tier"] = tier
        summary["tier_label"] = tier_label(tier)
        json_response(handler, 200, summary)

    def handle_me(self, handler: BaseHTTPRequestHandler) -> None:
        user = self.require_user(handler)
        if not user:
            return
        tier = user.get("tier") or DEFAULT_TIER
        sub = self.store.get_subscription(user["subscription_id"]) if user["subscription_id"] else None
        json_response(handler, 200, {
            "user_id": user["id"], "name": user["name"], "email": user["email"],
            "tier": tier,
            "tier_label": tier_label(tier),
            "limits": effective_limits(tier),
            "subscription": {
                "id": sub["id"], "status": sub["status"], "source": sub["source"],
            } if sub else None,
        })


class Handler(BaseHTTPRequestHandler):
    backend: Backend
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path
        backend = self.backend
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if self.command == "POST" and path == "/v1/account-auth/start":
                backend.handle_account_auth_start(self)
            elif self.command == "GET" and path.startswith("/v1/account-auth/status/"):
                backend.handle_account_auth_status(self, path.rsplit("/", 1)[-1], query)
            elif self.command == "GET" and path == "/v1/account-auth/google/callback":
                backend.handle_account_auth_callback(self, query)
            elif self.command == "GET" and path == "/v1/account-auth/complete":
                html_response(self, 200, completion_html())
            elif self.command == "POST" and path == "/v1/account-auth/refresh":
                backend.handle_account_auth_refresh(self)
            elif self.command == "POST" and path == "/v1/account-auth/logout":
                backend.handle_account_auth_logout(self)
            elif self.command == "POST" and path == "/v1/signup":
                backend.handle_signup(self)
            elif self.command == "POST" and path == "/v1/byok":
                backend.handle_byok(self)
            elif path in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
                backend.handle_proxy(self, UPSTREAM_PATHS[path])
            elif self.command == "GET" and path == "/v1/models":
                backend.handle_proxy(self, UPSTREAM_PATHS[path])
            elif self.command == "GET" and path == "/v1/usage":
                backend.handle_usage(self)
            elif self.command == "GET" and path == "/v1/me":
                backend.handle_me(self)
            elif self.command == "GET" and path == "/v1/agent/status":
                backend.handle_agent_status(self)
            elif self.command == "POST" and path == "/v1/agent/run":
                backend.handle_agent_run(self)
            elif self.command == "GET" and path == "/v1/internal/connectors/catalog":
                backend.handle_connector_catalog(self)
            elif self.command == "POST" and path == "/v1/internal/connectors/execute":
                backend.handle_connector_execute(self)
            elif self.command == "POST" and path == "/admin/subscriptions":
                backend.handle_admin_add_subscriptions(self)
            elif self.command == "GET" and path == "/admin/subscriptions":
                backend.handle_admin_list_subscriptions(self)
            elif self.command == "GET" and path == "/admin/users":
                backend.handle_admin_users(self)
            elif self.command == "POST" and path.startswith("/admin/users/") and path.endswith("/revoke"):
                backend.handle_admin_revoke(self, path.split("/")[3])
            elif self.command == "POST" and path.startswith("/admin/users/") and path.endswith("/tier"):
                backend.handle_admin_set_tier(self, path.split("/")[3])
            elif self.command == "GET" and path == "/admin/usage":
                backend.handle_admin_usage(self)
            elif path == "/healthz":
                json_response(self, 200, {
                    "ok": True,
                    "version": __version__,
                    "google_auth_configured": backend.google_auth.configured,
                })
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
        except Exception as e:  # noqa: BLE001 - nunca dejar colgar al cliente
            try:
                error_response(self, 500, f"Error interno: {e}", "internal_error")
            except Exception:
                self.close_connection = True

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()


def serve(cfg: Config) -> None:
    validate_admin_token(cfg.admin_token)
    if not cfg.admin_token:
        cfg.admin_token = secrets.token_hex(16)
        print(f"[config] ADMIN_TOKEN no definido; generado: {cfg.admin_token}", file=sys.stderr)
    backend = Backend(cfg)
    Handler.backend = backend
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port), Handler)
    print(f"[server] wrapper backend v{__version__} escuchando en http://127.0.0.1:{cfg.port}")
    print(f"[server] upstream Go: {cfg.go_base_url}")
    print(f"[server] enforce_limits={cfg.enforce_limits} db={cfg.db_path}")
    print(f"[server] vision_enabled={cfg.vision_enabled} vision_model={cfg.vision_model}")
    print(f"[server] pi_enabled={cfg.pi_enabled} pi_model={cfg.pi_model}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] deteniendo...")


def cli() -> None:
    parser = argparse.ArgumentParser(prog="wrapper-backend", description="Backend del wrapper sobre OpenCode Go")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init-db", help="Crear la base de datos")
    serve_cmd = sub.add_parser("serve", help="Arrancar el servidor HTTP")
    serve_cmd.add_argument("--port", type=int, default=None)
    sub.add_parser("users", help="Listar usuarios")
    sub.add_parser("subs", help="Listar suscripciones del pool")
    sub.add_parser("usage", help="Ver eventos de uso")
    add_key = sub.add_parser("add-key", help="Agregar key(s) de Go al pool (usa '-' para leer de stdin)")
    add_key.add_argument("keys", nargs="*", help="Keys de Go, '-' para stdin, o nada con --from-keychain")
    add_key.add_argument("--label", default=None)
    add_key.add_argument("--from-keychain", action="store_true",
                         help="Leer la key del Keychain de macOS (item 'codex-opencode-api-key')")

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
        print(f"[ok] base de datos en {cfg.db_path}")
    elif args.cmd == "users":
        for u in backend.store.list_users():
            print(u["id"], "|", u.get("name") or "-", "|", u.get("email") or "-",
                  "| tier:", u.get("tier") or "-", "| sub:", u["subscription_id"] or "-")
    elif args.cmd == "subs":
        for s in backend.store.list_subscriptions():
            print(s["id"], "|", s["status"], "|", s["source"], "|", s["assigned_user_id"] or "-")
    elif args.cmd == "usage":
        data = backend.store.usage_all()
        for e in data["events"]:
            print(f"{e['created_at']:.0f} | {e['user_id']} | {e['model'] or '-'} | {e['endpoint']} | "
                  f"in={e['input_tokens']} out={e['output_tokens']} | ${e['estimated_cost_usd']:.6f} | {e['status']}")
    elif args.cmd == "add-key":
        keys: list[str] = []
        for k in args.keys:
            if k == "-":
                keys.extend(line.strip() for line in sys.stdin if line.strip())
            else:
                keys.append(k)
        if args.from_keychain:
            import subprocess
            out = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", "alan"),
                 "-s", "codex-opencode-api-key", "-w"],
                check=False, capture_output=True,
            )
            if out.returncode != 0:
                print("[error] no se pudo leer el item del Keychain:",
                      out.stderr.decode().strip(), file=sys.stderr)
                sys.exit(1)
            keys.append(out.stdout.decode().strip())
        if not keys:
            print("[error] no se dieron keys (usa: add-key <key> | add-key - | add-key --from-keychain)",
                  file=sys.stderr)
            sys.exit(1)
        created = []
        for k in keys:
            sub_id = new_id("sub")
            blob = encrypt_api_key(k, sub_id, cfg.wrapper_secret, cfg.secret_file)
            sub = backend.store.add_subscription(blob, sub_id, label=args.label, sub_id=sub_id)
            created.append(sub)
        print(f"[ok] {len(created)} key(s) agregadas al pool. Disponibles: {backend.store.available_count()}")
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
