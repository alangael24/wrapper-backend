"""Servidor HTTP del wrapper backend.

Endpoints publicos (Bearer = api key del usuario del wrapper):
  POST /v1/signup          Crear usuario (tier: free|basic|pro) + asignar sub Go si aplica
  POST /v1/byok            El usuario registra su propia key de Go
  GET  /v1/models          Catalogo de modelos (proxy a Go)
  POST /v1/chat/completions
  POST /v1/responses
  POST /v1/messages
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
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .crypto_utils import decrypt_api_key, encrypt_api_key, hash_wrapper_key
from .go_prices import estimate_cost_usd
from .tiers import DEFAULT_TIER, SIGNUP_TIERS, effective_limits, is_valid, requires_subscription, tier_label
from .store import Store, new_id
from .upstream import DEFAULT_UA, proxy_request

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "wrapper.sqlite"
DEFAULT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "secret.key"
DEFAULT_GO_BASE = "https://opencode.ai/zen/go/v1"
MAX_BODY = 160 * 1024 * 1024  # 160 MB

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


class Config:
    def __init__(self):
        self.db_path = Path(os.environ.get("DB_PATH", str(DEFAULT_DB)))
        self.secret_file = Path(os.environ.get("SECRET_FILE", str(DEFAULT_SECRET_FILE)))
        self.go_base_url = os.environ.get("GO_BASE_URL", DEFAULT_GO_BASE).rstrip("/")
        self.port = int(os.environ.get("PORT", "8787"))
        self.enforce_limits = os.environ.get("ENFORCE_LIMITS", "1") != "0"
        self.wrapper_secret = os.environ.get("WRAPPER_SECRET") or None
        self.admin_token = os.environ.get("ADMIN_TOKEN") or None


def json_response(handler: BaseHTTPRequestHandler, status: int, obj) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: BaseHTTPRequestHandler, status: int, message: str, code: str | None = None) -> None:
    json_response(handler, status, {"error": {"message": message, "type": code or "error"}})


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.secret_file.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg.db_path)

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
        user = self.store.get_user_by_api_key(key)
        if not user:
            error_response(handler, 401, "API key del wrapper invalida", "unauthorized")
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
    def handle_signup(self, handler: BaseHTTPRequestHandler) -> None:
        body = self.read_json(handler) or {}
        name = body.get("name")
        email = body.get("email")
        tier = str(body.get("tier") or DEFAULT_TIER).lower()
        if not is_valid(tier):
            error_response(handler, 400, f"Tier invalido: {tier}. Opciones: {', '.join(SIGNUP_TIERS)}", "bad_tier")
            return
        sub = None
        if requires_subscription(tier):
            sub = self.store.next_available()
            if sub is None:
                error_response(
                    handler, 409,
                    "No hay suscripciones de OpenCode Go disponibles. El operador debe "
                    "agregar keys al pool (POST /admin/subscriptions o `add-key`).",
                    "no_subscriptions_available",
                )
                return
        api_key = secrets.token_hex(32)
        user = self.store.create_user(api_key, name, email,
                                      subscription_id=sub["id"] if sub else None, tier=tier)
        json_response(handler, 201, {
            "api_key": api_key,
            "user_id": user["id"],
            "name": user["name"],
            "tier": tier,
            "tier_label": tier_label(tier),
            "limits": effective_limits(tier),
            "subscription_id": sub["id"] if sub else None,
            "subscription_status": "assigned" if sub else "none",
            "available_left": self.store.available_count(),
            "note": "Guarda el api_key; no se puede volver a mostrar.",
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
        self.record(handler, user, sub, path, status, usage)

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
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            if raw:
                handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def record(self, handler, user, sub, path, status, usage) -> None:
        model = usage.model if usage.any() else None
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

    # ---------- body helpers ----------
    def read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        length = handler.headers.get("content-length")
        if length and length.isdigit():
            n = int(length)
            if n > MAX_BODY:
                return b""
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
        if user["subscription_id"]:
            self.store.revoke_subscription(user["subscription_id"])
            self.store.update_user_subscription(user_id, None)
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
        if requires_subscription(tier) and not user["subscription_id"]:
            sub = self.store.next_available()
            if sub is None:
                error_response(handler, 409, "No hay suscripciones disponibles para asignar al subir de tier", "no_subscriptions_available")
                return
            self.store.update_user_subscription(user_id, sub["id"])
        elif not requires_subscription(tier) and user["subscription_id"]:
            self.store.revoke_subscription(user["subscription_id"])
            self.store.update_user_subscription(user_id, None)
        self.store.set_user_tier(user_id, tier)
        updated = self.store.get_user_by_id(user_id)
        json_response(handler, 200, {
            "user_id": user_id,
            "tier": tier,
            "subscription_id": updated["subscription_id"] if updated else None,
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
            if self.command == "POST" and path == "/v1/signup":
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
                json_response(self, 200, {"ok": True, "version": __version__})
            else:
                error_response(self, 404, f"No existe {self.command} {path}", "not_found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
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
    if not cfg.admin_token:
        cfg.admin_token = secrets.token_hex(16)
        print(f"[config] ADMIN_TOKEN no definido; generado: {cfg.admin_token}", file=sys.stderr)
    backend = Backend(cfg)
    Handler.backend = backend
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port), Handler)
    print(f"[server] wrapper backend v{__version__} escuchando en http://127.0.0.1:{cfg.port}")
    print(f"[server] upstream Go: {cfg.go_base_url}")
    print(f"[server] enforce_limits={cfg.enforce_limits} db={cfg.db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] deteniendo...")


def cli() -> None:
    parser = argparse.ArgumentParser(prog="wrapper-backend", description="Backend del wrapper sobre OpenCode Go")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init-db", help="Crear la base de datos")
    sub.add_parser("serve", help="Arrancar el servidor HTTP")
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
    backend = Backend(cfg)
    if args.cmd == "init-db":
        print(f"[ok] base de datos en {cfg.db_path}")
    elif args.cmd == "serve":
        serve(cfg)
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
