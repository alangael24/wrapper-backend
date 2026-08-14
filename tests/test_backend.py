"""Integration tests against a mock DeepSeek-compatible upstream."""

from __future__ import annotations

import base64
import json
import http.client
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_backend.server import (  # noqa: E402
    Backend,
    Config,
    Handler,
    UnsafeConfigurationError,
    _partial_json_text,
    serve,
    validate_runtime_security,
)
from go_backend.connectors import (  # noqa: E402
    CONNECTOR_CATALOG,
    ConnectorBroker,
    ConnectorBrokerError,
)
from go_backend.connector_adapters import (  # noqa: E402
    ComposioConnectorAdapter,
    ComposioConnectorGateway,
)
from go_backend.native_connectors import NativeConnectorGateway  # noqa: E402
from go_backend.google_auth import GoogleAccountAuth  # noqa: E402
from go_backend.pi_harness import RUNTIME_AUTH_EXTENSION  # noqa: E402
from go_backend.store import Store, new_id  # noqa: E402


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body: bytes, ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).requests.append((self.command, self.path, dict(self.headers)))
        if self.path == "/v1/models":
            catalog = {"object": "list", "data": [{"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"}]}
            self._send(200, json.dumps(catalog).encode())
        else:
            self._send(404, json.dumps({"error": "not found"}).encode())

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        payload = json.loads(body) if body else {}
        if self.path == "/v1/chat/completions":
            if payload.get("stream"):
                self._send(
                    200,
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"hola"},"finish_reason":null}]}\n\n'
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
                    b'data: [DONE]\n\n',
                    ctype="text/event-stream",
                )
            else:
                resp = {
                    "id": "cmpl-test", "model": "deepseek-v4-flash",
                    "choices": [{"message": {"role": "assistant", "content": "hola"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15,
                              "prompt_cache_hit_tokens": 4,
                              "prompt_cache_miss_tokens": 6},
                }
                self._send(200, json.dumps(resp).encode())
        else:
            self._send(404, json.dumps({"error": "not found"}).encode())


def start_mock() -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class WrapperServer:
    def __init__(self, upstream_base: str, tmp: str):
        os.environ["DEEPSEEK_BASE_URL"] = upstream_base + "/v1"
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek-server"
        os.environ["OPENCODE_BASE_URL"] = upstream_base + "/v1"
        os.environ["DB_PATH"] = os.path.join(tmp, "test.sqlite")
        os.environ["SECRET_FILE"] = os.path.join(tmp, "secret.key")
        os.environ["ADMIN_TOKEN"] = "test-admin"
        os.environ["CREDITS_MODE"] = "shadow"
        os.environ["PI_ENABLED"] = "0"
        os.environ.pop("WRAPPER_SECRET", None)
        os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", None)
        os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)
        os.environ.pop("GOOGLE_OAUTH_REDIRECT_URI", None)
        for name in (
            "COMPOSIO_API_KEY", "COMPOSIO_PUBLIC_URL", "COMPOSIO_AUTH_CONFIGS_JSON",
            "COMPOSIO_TOOLKIT_OVERRIDES_JSON", "COMPOSIO_AUTH_ATTEMPT_TTL_SECONDS",
        ):
            os.environ.pop(name, None)
        for name in (
            "STRIPE_ENABLED", "STRIPE_LIVE_MODE", "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET", "STRIPE_PLUS_PRICE_ID", "STRIPE_STARTER_PRICE_ID",
            "STRIPE_PRO_PRICE_ID", "STRIPE_BUSINESS_PRICE_ID",
            "STRIPE_SUCCESS_URL", "STRIPE_CANCEL_URL", "STRIPE_PORTAL_RETURN_URL",
        ):
            os.environ.pop(name, None)
        for name in (
            "COMPUTERS_ENABLED", "DAYTONA_API_KEY", "DAYTONA_API_URL", "DAYTONA_TARGET",
            "DAYTONA_SNAPSHOT", "COMPUTER_AUTO_STOP_MINUTES", "COMPUTER_AUTO_ARCHIVE_MINUTES",
            "COMPUTER_PREVIEW_TTL_SECONDS", "COMPUTER_VNC_PORT", "COMPUTER_VNC_RESOLUTION", "COMPUTER_BASIC_LIMIT",
            "COMPUTER_PRO_LIMIT",
        ):
            os.environ.pop(name, None)
        self.cfg = Config()
        self.backend = Backend(self.cfg)
        Handler.backend = self.backend
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.admin_headers = {"Authorization": "Bearer test-admin"}

    def enable_fake_pi(self, browser=False):
        fake_pi = Path(__file__).resolve().parent / "fake_pi.py"
        self.backend.pi.enabled = True
        self.backend.pi.binary = str(fake_pi)
        self.backend.pi.warm_sessions_enabled = True
        self.backend.pi.backend_url = self.base
        self.backend.pi.connector_broker_url = self.base
        self.backend.pi.runs_dir = Path(self.cfg.db_path).parent / "pi-runs"
        # Process startup on loaded macOS/Linux CI runners can exceed five
        # seconds even though the fake model itself is immediate.
        self.backend.pi.timeout_seconds = 8
        if browser:
            fake_root = Path(self.cfg.db_path).parent / "fake-pi-chrome"
            fake_extension = fake_root / "index.ts"
            companion = fake_root / "browser-extension"
            companion.mkdir(parents=True, exist_ok=True)
            fake_extension.write_text("export default () => {}\n", encoding="utf-8")
            (companion / "service_worker.js").write_text(
                'const BRIDGE_URL = "http://127.0.0.1:17318";\n',
                encoding="utf-8",
            )
            (companion / "manifest.json").write_text(
                json.dumps({
                    "manifest_version": 3,
                    "name": "Fake Pi Chrome Connector",
                    "version": "1.0.0",
                    "permissions": ["tabs"],
                    "host_permissions": ["<all_urls>", "http://127.0.0.1:17318/*"],
                    "background": {"service_worker": "service_worker.js"},
                }),
                encoding="utf-8",
            )
            chrome_log = Path(self.cfg.db_path).parent / "fake-chrome.jsonl"
            fake_chrome = Path(self.cfg.db_path).parent / "fake_chrome.py"
            fake_chrome.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, signal, sys, time\n"
                "from pathlib import Path\n"
                f"log_path = Path({str(chrome_log)!r})\n"
                "with log_path.open('a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps({'argv': sys.argv[1:], "
                "'has_admin_token': 'ADMIN_TOKEN' in os.environ}) + '\\n')\n"
                "    stream.flush()\n"
                "def stop(*_):\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "while True:\n"
                "    time.sleep(0.05)\n",
                encoding="utf-8",
            )
            fake_chrome.chmod(0o755)
            self.backend.pi.chrome_extension = str(fake_extension)
            self.backend.pi.chrome_auto_authorize = True
            self.backend.pi.chrome_binary = str(fake_chrome)
            self.backend.pi.chrome_isolation = "per_run"
            self.backend.pi.chrome_test_log = chrome_log

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.backend.pi.close()

    def req(self, method, path, body=None, headers=None, raw=False, include_headers=False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                content = resp.read()
                parsed = content if raw else (json.loads(content) if content else None)
                if include_headers:
                    return resp.status, parsed, dict(resp.headers)
                return resp.status, parsed
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                parsed = json.loads(content) if content else None
            except Exception:
                parsed = content
            if include_headers:
                return e.code, parsed, dict(e.headers)
            return e.code, parsed


class FakeGitHubAdapter:
    def __init__(self, connected_user_id: str):
        self.connected_user_id = connected_user_id
        self.calls: list[tuple[str, str, dict]] = []

    def is_connected(self, user_id: str) -> bool:
        return user_id == self.connected_user_id

    def execute(self, user_id: str, operation: str, arguments: dict):
        self.calls.append((user_id, operation, arguments))
        return {"items": [{"name": "wrapper-backend", "private": True}]}


class FakeComposioAccounts:
    def __init__(self):
        self.items: dict[str, SimpleNamespace] = {}
        self.list_calls: list[dict] = []
        self.get_calls: list[str] = []

    def list(self, *, user_ids, statuses, limit, toolkit_slugs=None):
        self.list_calls.append({
            "user_ids": user_ids,
            "toolkit_slugs": toolkit_slugs,
            "statuses": statuses,
            "limit": limit,
        })
        matches = [
            account for account in self.items.values()
            if account.user_id in user_ids
            and (toolkit_slugs is None or account.toolkit in toolkit_slugs)
            and account.status in statuses
        ][:limit]
        return SimpleNamespace(items=matches)

    def get(self, account_id):
        self.get_calls.append(account_id)
        return self.items[account_id]

    def delete(self, account_id):
        self.items.pop(account_id, None)


class FakeComposioSession:
    def __init__(self, client, user_id, toolkit):
        self.client = client
        self.user_id = user_id
        self.toolkit = toolkit
        self.deleted = False

    def authorize(self, toolkit, **_options):
        account_id = f"ca_{len(self.client.connected_accounts.items) + 1}"
        self.client.connected_accounts.items[account_id] = SimpleNamespace(
            id=account_id,
            user_id=self.user_id,
            toolkit=toolkit,
            status="INITIATED",
            alias="",
            data={},
            status_reason="",
        )
        return SimpleNamespace(
            id=account_id,
            redirect_url=f"https://connect.composio.dev/link/{account_id}",
        )

    def search(self, *, query):
        self.client.searches.append((self.toolkit, query))
        return SimpleNamespace(
            results=[SimpleNamespace(primary_tool_slugs=[f"{self.toolkit.upper()}_SEARCH"])]
        )

    def execute(self, slug, *, arguments):
        self.client.executions.append((slug, arguments))
        return SimpleNamespace(data={"items": [{"name": "wrapper-backend"}]}, error=None)

    def delete(self):
        self.deleted = True


class FakeComposioSessions:
    def __init__(self, client):
        self.client = client

    def create(self, **options):
        self.client.session_options.append(options)
        return FakeComposioSession(
            self.client, options["user_id"], options["toolkits"][0]
        )


class FakeComposioClient:
    def __init__(self):
        self.connected_accounts = FakeComposioAccounts()
        self.sessions = FakeComposioSessions(self)
        self.session_options: list[dict] = []
        self.searches: list[tuple[str, str]] = []
        self.executions: list[tuple[str, dict]] = []


class TestBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, cls.mock_base = start_mock()

    @classmethod
    def tearDownClass(cls):
        cls.mock.shutdown()
        cls.mock.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wrapper-test-")
        self.ws = WrapperServer(self.mock_base, self.tmp)
        MockUpstream.requests.clear()

    def tearDown(self):
        self.ws.stop()
        MockUpstream.requests.clear()


    # ---------- helpers ----------
    def new_user(self, name=None, tier="pro"):
        status, signup = self.ws.req("POST", "/v1/signup", {"name": name} if name else {})
        self.assertEqual(status, 201)
        self.assertEqual(signup["tier"], "free")
        if tier != "free":
            status, upgraded = self.ws.req(
                "POST",
                f"/admin/users/{signup['user_id']}/tier",
                {"tier": tier},
                headers=self.ws.admin_headers,
            )
            self.assertEqual(status, 200)
            signup["tier"] = tier
        return signup

    def upstream_payloads(self, path, model=None):
        payloads = []
        for request in MockUpstream.requests:
            if len(request) < 4 or request[1] != path:
                continue
            payload = json.loads(request[3])
            if model is None or payload.get("model") == model:
                payloads.append(payload)
        return payloads

    def configure_fake_google(self, *, email="alan@example.com", verified=True):
        auth = self.ws.backend.google_auth
        auth.client_id = "google-client-id.apps.googleusercontent.com"
        auth.client_secret = "test-secret-not-for-production"
        auth.redirect_uri = self.ws.base + "/v1/account-auth/google/callback"
        auth._exchange_code = lambda **_: {"access_token": "google-access-token-for-tests"}
        auth._fetch_userinfo = lambda _token: {
            "sub": "google-subject-123",
            "email": email,
            "email_verified": verified,
            "name": "Alan Example",
            "picture": "https://images.example/avatar.png",
        }
        return auth

    # ---------- pool / signup ----------
    def test_account_state_sync_is_account_scoped_versioned_and_validated(self):
        first = self.new_user(tier="free")
        second = self.new_user(tier="free")
        first_headers = {"Authorization": f"Bearer {first['api_key']}"}
        second_headers = {"Authorization": f"Bearer {second['api_key']}"}

        status, empty = self.ws.req("GET", "/v1/account-state", headers=first_headers)
        self.assertEqual(status, 200)
        self.assertEqual(empty["revision"], 0)
        self.assertEqual(empty["state"]["bots"], [])

        device_id = str(uuid.uuid4())
        state = {
            "version": 99,
            "onboardingCompleted": True,
            "selectedConnectorIds": ["github", "not-real", "github"],
            "activeBotId": "bot-one",
            "bots": [{
                "id": "bot-one",
                "name": "  Research   bot ",
                "color": "#2F91F5",
                "shape": "bean",
                "connectorIds": ["github", "not-real"],
                "messages": [{
                    "id": "message-one",
                    "role": "assistant",
                    "text": "Listo",
                    "createdAt": "2026-08-13T20:00:00Z",
                }],
                "createdAt": "2026-08-13T19:00:00Z",
            }],
        }
        status, saved = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 0, "device_id": device_id, "state": state},
            headers=first_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["state"]["version"], 1)
        self.assertEqual(saved["state"]["selectedConnectorIds"], ["github"])
        self.assertEqual(saved["state"]["bots"][0]["name"], "Research bot")

        status, other = self.ws.req("GET", "/v1/account-state", headers=second_headers)
        self.assertEqual(status, 200)
        self.assertEqual(other["revision"], 0)

        stale_state = {**saved["state"], "activeBotId": None}
        status, conflict = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 0, "device_id": device_id, "state": stale_state},
            headers=first_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["type"], "account_state_conflict")
        self.assertEqual(conflict["current"]["revision"], 1)

        status, invalid = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 1, "device_id": "not-a-device", "state": state},
            headers=first_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["type"], "invalid_account_state")

    def test_google_account_auth_flow_issues_rotates_and_revokes_session(self):
        self.configure_fake_google()
        device_id = str(uuid.uuid4())
        status, started = self.ws.req(
            "POST",
            "/v1/account-auth/start",
            {"device_id": device_id, "app_version": "0.1.0"},
        )
        self.assertEqual(status, 201)
        authorize = urllib.parse.urlparse(started["authorize_url"])
        params = urllib.parse.parse_qs(authorize.query)
        self.assertEqual(authorize.hostname, "accounts.google.com")
        self.assertEqual(params["scope"], ["openid email profile"])
        self.assertEqual(params["code_challenge_method"], ["S256"])

        callback_path = "/v1/account-auth/google/callback?" + urllib.parse.urlencode({
            "state": params["state"][0],
            "code": "one-use-code",
        })
        status, html = self.ws.req("GET", callback_path, raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Regresa a Agent Genia", html)
        status, replayed = self.ws.req("GET", callback_path)
        self.assertEqual(status, 400)
        self.assertEqual(replayed["error"]["type"], "invalid_state")

        self.ws.backend.google_auth = GoogleAccountAuth(
            store=self.ws.backend.store,
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri=self.ws.base + "/v1/account-auth/google/callback",
            secret_env=self.ws.cfg.wrapper_secret,
            secret_path=self.ws.cfg.secret_file,
            key_version=self.ws.cfg.wrapper_secret_version,
            secret_versions=self.ws.cfg.wrapper_secret_versions,
        )

        status, completed = self.ws.req(
            "POST",
            "/v1/account-auth/status",
            {"attempt_id": started["attempt_id"], "device_id": device_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "complete")
        self.assertTrue(completed["token"].startswith("aga_"))
        self.assertTrue(completed["refresh_token"].startswith("agr_"))
        self.assertTrue(completed["account"]["id"].startswith("acct_"))
        status, replayed_result = self.ws.req(
            "POST",
            "/v1/account-auth/status",
            {"attempt_id": started["attempt_id"], "device_id": device_id},
        )
        self.assertEqual(status, 404)
        self.assertEqual(replayed_result["error"]["type"], "not_found")

        status, me = self.ws.req(
            "GET", "/v1/me", headers={"Authorization": f"Bearer {completed['token']}"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(me["email"], "alan@example.com")
        self.assertEqual(me["tier"], "free")

        # Mobile requests with the opaque account token use a bounded hot
        # cache instead of paying one cross-region session lookup per endpoint.
        original_lookup = self.ws.backend.store.get_user_by_access_token
        with patch.object(
            self.ws.backend.store,
            "get_user_by_access_token",
            wraps=original_lookup,
        ) as lookup:
            for _ in range(2):
                status, _ = self.ws.req(
                    "GET", "/v1/me",
                    headers={"Authorization": f"Bearer {completed['token']}"},
                )
                self.assertEqual(status, 200)
            self.assertEqual(lookup.call_count, 0)

        status, _ = self.ws.req(
            "POST",
            "/v1/account-auth/refresh",
            {"device_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {completed['refresh_token']}"},
        )
        self.assertEqual(status, 401)

        status, refreshed = self.ws.req(
            "POST",
            "/v1/account-auth/refresh",
            {"device_id": device_id},
            headers={"Authorization": f"Bearer {completed['refresh_token']}"},
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(refreshed["token"], completed["token"])
        self.assertNotEqual(refreshed["refresh_token"], completed["refresh_token"])

        status, _ = self.ws.req(
            "POST",
            "/v1/account-auth/refresh",
            {"device_id": device_id},
            headers={"Authorization": f"Bearer {completed['refresh_token']}"},
        )
        self.assertEqual(status, 401)

        status, body = self.ws.req(
            "POST",
            "/v1/account-auth/logout",
            headers={"Authorization": f"Bearer {refreshed['token']}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["revoked"])
        status, _ = self.ws.req(
            "GET", "/v1/me", headers={"Authorization": f"Bearer {refreshed['token']}"}
        )
        self.assertEqual(status, 401)

    def test_google_account_auth_status_supports_electron_get_endpoint(self):
        self.configure_fake_google()
        device_id = str(uuid.uuid4())
        _, started = self.ws.req(
            "POST", "/v1/account-auth/start", {"device_id": device_id, "app_version": "0.1.0"}
        )
        params = urllib.parse.parse_qs(urllib.parse.urlparse(started["authorize_url"]).query)
        self.ws.req(
            "GET",
            "/v1/account-auth/google/callback?" + urllib.parse.urlencode({
                "state": params["state"][0], "code": "ok"
            }),
            raw=True,
        )
        status, rejected = self.ws.req(
            "GET",
            f"/v1/account-auth/status/{started['attempt_id']}?device_id={uuid.uuid4()}",
        )
        self.assertEqual(status, 404)
        self.assertEqual(rejected["error"]["type"], "not_found")
        status, completed = self.ws.req(
            "GET",
            f"/v1/account-auth/status/{started['attempt_id']}?device_id={device_id}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "complete")
        self.assertTrue(completed["token"].startswith("aga_"))
        status, replayed = self.ws.req(
            "GET",
            f"/v1/account-auth/status/{started['attempt_id']}?device_id={device_id}",
        )
        self.assertEqual(status, 404)
        self.assertEqual(replayed["error"]["type"], "not_found")

    def test_google_login_does_not_link_unverified_signup_email(self):
        status, signup = self.ws.req("POST", "/v1/signup", {"email": "alan@example.com"})
        self.assertEqual(status, 201)
        self.configure_fake_google(email="alan@example.com")
        device_id = str(uuid.uuid4())
        _, started = self.ws.req(
            "POST", "/v1/account-auth/start", {"device_id": device_id, "app_version": "test"}
        )
        params = urllib.parse.parse_qs(urllib.parse.urlparse(started["authorize_url"]).query)
        self.ws.req(
            "GET",
            "/v1/account-auth/google/callback?" + urllib.parse.urlencode({
                "state": params["state"][0], "code": "ok"
            }),
            raw=True,
        )
        _, completed = self.ws.req(
            "POST", "/v1/account-auth/status",
            {"attempt_id": started["attempt_id"], "device_id": device_id},
        )
        google_user = self.ws.backend.store.get_user_by_access_token(completed["token"])
        self.assertNotEqual(google_user["id"], signup["user_id"])
        self.assertEqual(len(self.ws.backend.store.list_users()), 2)

    def test_google_auth_rejects_unverified_email_and_missing_configuration(self):
        device_id = str(uuid.uuid4())
        status, body = self.ws.req(
            "POST", "/v1/account-auth/start", {"device_id": device_id, "app_version": "test"}
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "google_not_configured")

        self.configure_fake_google(verified=False)
        _, started = self.ws.req(
            "POST", "/v1/account-auth/start", {"device_id": device_id, "app_version": "test"}
        )
        params = urllib.parse.parse_qs(urllib.parse.urlparse(started["authorize_url"]).query)
        self.ws.req(
            "GET",
            "/v1/account-auth/google/callback?" + urllib.parse.urlencode({
                "state": params["state"][0], "code": "ok"
            }),
            raw=True,
        )
        _, result = self.ws.req(
            "POST", "/v1/account-auth/status",
            {"attempt_id": started["attempt_id"], "device_id": device_id},
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("verificada", result["message"])
        status, replayed = self.ws.req(
            "POST", "/v1/account-auth/status",
            {"attempt_id": started["attempt_id"], "device_id": device_id},
        )
        self.assertEqual(status, 404)
        self.assertEqual(replayed["error"]["type"], "not_found")

    def test_public_signup_always_free_and_ignores_requested_tier(self):
        ws = self.ws

        for requested_tier in (None, "basic", "pro", "ultra"):
            payload = {"name": f"usuario-{requested_tier or 'default'}"}
            if requested_tier is not None:
                payload["tier"] = requested_tier
            status, body = ws.req("POST", "/v1/signup", payload)
            self.assertEqual(status, 201)
            self.assertIn("api_key", body)
            self.assertEqual(len(body["api_key"]), 64)
            self.assertEqual(body["tier"], "free")
            self.assertNotIn("subscription_id", body)

    def test_legacy_signup_can_be_disabled(self):
        self.ws.cfg.public_legacy_signup_enabled = False
        status, body = self.ws.req("POST", "/v1/signup", {})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["type"], "not_found")

    def test_authenticated_user_can_delete_account(self):
        signup = self.new_user("delete-me")
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        self.assertIsNotNone(user)
        status, body = self.ws.req(
            "POST",
            "/v1/account/delete",
            {"confirmation": "DELETE"},
            headers={"Authorization": f"Bearer {signup['api_key']}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"])
        self.assertIsNone(self.ws.backend.store.get_user_by_id(user["id"]))
        status, _ = self.ws.req(
            "GET", "/v1/me", headers={"Authorization": f"Bearer {signup['api_key']}"}
        )
        self.assertEqual(status, 401)

    def test_admin_auth_required(self):
        status, _ = self.ws.req("GET", "/admin/users")
        self.assertEqual(status, 401)

    def test_server_rejects_published_example_admin_token(self):
        cfg = Config()
        cfg.admin_token = "  CAMBIA-ESTE-TOKEN  "
        with self.assertRaisesRegex(UnsafeConfigurationError, "valor inseguro de ejemplo"):
            serve(cfg)

    def test_server_rejects_shared_chrome_profiles(self):
        cfg = Config()
        cfg.pi_chrome_isolation = "shared"
        with self.assertRaisesRegex(UnsafeConfigurationError, "per_run"):
            Backend(cfg)

    def test_pi_defaults_to_fail_closed_sandbox_launcher(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        self.assertEqual(Path(cfg.pi_bin).name, "pi-sandbox")
        self.assertEqual(Path(cfg.pi_bin).parent.name, "scripts")

    def test_warm_sessions_cannot_expose_tokens_to_the_strict_launcher(self):
        cfg = Config()
        cfg.pi_warm_sessions = True
        cfg.pi_bin = str(Path("scripts/pi-sandbox").resolve())
        with self.assertRaisesRegex(UnsafeConfigurationError, "pi-render-safe"):
            validate_runtime_security(cfg)

    def test_models_proxy(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("GET", "/v1/models", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["id"], "deepseek-v4-flash")

    def test_private_opencode_override_is_scoped_to_one_unlimited_account(self):
        ws = self.ws
        opencode_signup = self.new_user(tier="free")
        deepseek_signup = self.new_user(tier="pro")
        opencode_user = ws.backend.store.get_user_by_api_key(opencode_signup["api_key"])
        encrypted = ws.backend.encrypt_secret("sk-opencode-private", "opencode-test")
        credential = ws.backend.store.add_subscription(
            encrypted,
            "opencode-test",
            "Private OpenCode override",
            key_version=ws.cfg.wrapper_secret_version,
        )
        configured = ws.backend.store.configure_user_model_provider(
            opencode_user["id"],
            provider="opencode",
            subscription_id=credential["id"],
            unlimited_usage=True,
        )
        self.assertEqual(configured["model_provider_override"], "opencode")
        self.assertEqual(configured["unlimited_usage"], 1)

        opencode_headers = {"Authorization": f"Bearer {opencode_signup['api_key']}"}
        status, me = ws.req("GET", "/v1/me", headers=opencode_headers)
        self.assertEqual(status, 200)
        self.assertEqual(me["model_provider"], "opencode")
        self.assertTrue(me["unlimited_usage"])
        status, credits = ws.req("GET", "/v1/credits", headers=opencode_headers)
        self.assertEqual(status, 200)
        self.assertEqual(credits["mode"], "unlimited")

        MockUpstream.requests.clear()
        status, _ = ws.req("GET", "/v1/models", headers=opencode_headers)
        self.assertEqual(status, 200)
        self.assertEqual(
            MockUpstream.requests[-1][2]["Authorization"],
            "Bearer sk-opencode-private",
        )

        deepseek_headers = {"Authorization": f"Bearer {deepseek_signup['api_key']}"}
        status, _ = ws.req("GET", "/v1/models", headers=deepseek_headers)
        self.assertEqual(status, 200)
        self.assertEqual(
            MockUpstream.requests[-1][2]["Authorization"],
            "Bearer sk-deepseek-server",
        )

    def test_unlimited_opencode_account_runs_pi_without_consuming_credits(self):
        ws = self.ws
        signup = self.new_user(tier="free")
        user = ws.backend.store.get_user_by_api_key(signup["api_key"])
        encrypted = ws.backend.encrypt_secret("sk-opencode-private", "opencode-pi")
        credential = ws.backend.store.add_subscription(
            encrypted,
            "opencode-pi",
            "Private OpenCode Pi",
            key_version=ws.cfg.wrapper_secret_version,
        )
        ws.backend.store.configure_user_model_provider(
            user["id"],
            provider="opencode",
            subscription_id=credential["id"],
            unlimited_usage=True,
        )
        ws.enable_fake_pi()
        MockUpstream.requests.clear()
        status, result = ws.req(
            "POST",
            "/v1/agent/run",
            {"prompt": "usa OpenCode", "idempotency_key": "private-opencode-pi"},
            headers={"Authorization": f"Bearer {signup['api_key']}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["credits"]["charged"], 0.0)
        upstream_calls = [
            request for request in MockUpstream.requests
            if request[1] == "/v1/chat/completions"
        ]
        self.assertEqual(len(upstream_calls), 1)
        self.assertEqual(
            upstream_calls[0][2]["Authorization"], "Bearer sk-opencode-private"
        )

    def test_chat_completions_records_usage(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("POST", "/v1/chat/completions",
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "hola")
        # uso registrado
        status, usage = ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(usage["windows"]["5h"]["requests"], 1)
        self.assertGreater(usage["windows"]["5h"]["spent_usd"], 0)
        self.assertIn("deepseek-v4-flash", usage["by_model"])
        # evento en admin
        status, all_usage = ws.req("GET", "/admin/usage", headers=ws.admin_headers)
        self.assertGreater(len(all_usage["events"]), 0)
        ev = all_usage["events"][0]
        self.assertEqual(ev["input_tokens"], 10)
        self.assertEqual(ev["output_tokens"], 5)
        self.assertEqual(ev["cached_read_tokens"], 4)

    def test_streaming_passthrough(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("POST", "/v1/chat/completions",
                              {"model": "deepseek-v4-flash", "stream": True,
                               "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers, raw=True)
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("data:", text)
        self.assertIn("\n\ndata:", text)
        self.assertIn("[DONE]", text)

    def test_chunked_request_body(self):
        signup = self.new_user()
        host, port = self.ws.httpd.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        payload = json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "chunked"}],
        }).encode()
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=iter((payload[:20], payload[20:])),
            headers={
                "Authorization": f"Bearer {signup['api_key']}",
                "Content-Type": "application/json",
            },
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "hola")

    def test_shadow_credits_replace_rolling_usage_block(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        # El historial en dólares sigue visible, pero shadow no bloquea por ventanas.
        sub_id = ws.backend.store.get_user_by_api_key(signup["api_key"])["subscription_id"]
        for _ in range(3):
            ws.backend.store.record_usage(
                signup["user_id"], sub_id, "deepseek-v4-flash", "/chat/completions",
                1000000, 1000000, 0, 0, 6.0, 200,
            )
        status, body = ws.req("POST", "/v1/chat/completions",
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers)
        self.assertEqual(status, 200)
        status, credits = ws.req("GET", "/v1/credits", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(credits["mode"], "shadow")
        self.assertEqual(credits["credits"]["available"], 30.0)

    def test_auth_required(self):
        status, _ = self.ws.req("GET", "/v1/models")
        self.assertEqual(status, 401)
        status, _ = self.ws.req("GET", "/v1/usage", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_rejected_model_post_closes_connection_before_unread_body_can_be_reused(self):
        status, body, headers = self.ws.req(
            "POST",
            "/v1/chat/completions",
            {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer wrong"},
            include_headers=True,
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["type"], "unauthorized")
        self.assertEqual(headers.get("Connection"), "close")

    def test_json_responses_are_never_cacheable(self):
        status, body, headers = self.ws.req(
            "POST", "/v1/signup", {}, include_headers=True
        )
        self.assertEqual(status, 201)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(headers["Pragma"], "no-cache")
        self.assertTrue(headers["X-Request-Id"].startswith("req_"))
        self.assertIn("api_key", body)

    def test_structured_json_endpoints_reject_oversized_bodies(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.ws.httpd.server_address[1], timeout=10
        )
        connection.putrequest("POST", "/v1/signup")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str((1024 * 1024) + 1))
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 413)
        self.assertEqual(body["error"]["type"], "body_too_large")

    def test_structured_endpoints_reject_non_object_json(self):
        status, body = self.ws.req("POST", "/v1/signup", "not-an-object")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "bad_body")
        status, body = self.ws.req("POST", "/v1/signup", [], raw=False)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "bad_body")

    def test_unhandled_errors_are_private_and_have_request_id(self):
        with patch.object(self.ws.backend.store, "health", side_effect=RuntimeError("secret SQL table")):
            status, body = self.ws.req("GET", "/readyz")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["message"], "Internal server error")
        self.assertNotIn("secret SQL table", json.dumps(body))
        self.assertTrue(body["error"]["request_id"].startswith("req_"))

    def test_liveness_never_waits_for_dependency_readiness(self):
        with patch.object(self.ws.backend.store, "health", side_effect=RuntimeError("database unavailable")):
            status, body = self.ws.req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["liveness"])
        self.assertNotIn("database", body)

    def test_platform_health_checks_the_database_without_third_party_providers(self):
        with (
            patch.object(self.ws.backend.store, "health", return_value={"ready": True}),
            patch.object(self.ws.backend.pi, "status", side_effect=AssertionError("must not run")),
            patch.object(
                self.ws.backend.connector_gateway,
                "health",
                side_effect=AssertionError("must not run"),
            ),
            patch.object(
                self.ws.backend.computers,
                "health",
                side_effect=AssertionError("must not run"),
            ),
        ):
            status, body = self.ws.req("GET", "/platformz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["database_ready"])
        self.assertNotIn("database", body)

    def test_production_readiness_fails_closed_for_every_required_runtime(self):
        self.ws.cfg.environment = "production"
        status, body = self.ws.req("GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertFalse(body["ready"])
        self.assertEqual(
            set(body["checks"]),
            {
                "database", "google_auth", "apple_auth", "stripe", "connectors",
                "computers", "pi", "pi_chrome", "model_provider",
            },
        )
        self.assertFalse(all(body["checks"].values()))
        self.assertNotIn("database", body)
        self.assertNotIn("pi", body)
        self.assertNotIn("connectors", body)
        self.assertNotIn("computers", body)

    def test_readiness_checks_accept_partial_connector_catalog_and_disabled_computers(self):
        self.ws.cfg.environment = "production"
        self.ws.cfg.computers_enabled = False
        with (
            patch.object(self.ws.backend.store, "health", return_value={"ready": True}),
            patch.object(
                self.ws.backend.pi,
                "status",
                return_value={
                    "enabled": True,
                    "available": True,
                    "node_available": True,
                    "connectors_available": True,
                    "browser_available": True,
                    "browser_auto_authorize": True,
                    "browser_isolation": "per_run",
                },
            ),
            patch.object(
                self.ws.backend.connector_gateway,
                "health",
                return_value={
                    "configured": True,
                    "available_connectors": 48,
                    "catalog_connectors": 49,
                    "all_connectors_available": False,
                    "unavailable_connectors": ["loom"],
                },
            ),
            patch.object(
                self.ws.backend.computers,
                "health",
                return_value={"configured": False},
            ),
        ):
            readiness = self.ws.backend.readiness()
        self.assertTrue(readiness["checks"]["connectors"])
        self.assertTrue(readiness["checks"]["computers"])

    def test_readiness_requires_computers_when_feature_is_enabled(self):
        self.ws.cfg.computers_enabled = True
        with patch.object(
            self.ws.backend.computers,
            "health",
            return_value={"configured": False},
        ):
            readiness = self.ws.backend.readiness()
        self.assertFalse(readiness["checks"]["computers"])

    def test_production_requires_database_master_secret_and_admin_token(self):
        cfg = self.ws.cfg
        cfg.environment = "production"
        cfg.database_url = None
        cfg.wrapper_secret = None
        cfg.admin_token = None
        cfg.deepseek_api_key = ""
        with self.assertRaisesRegex(
            UnsafeConfigurationError, "DATABASE_URL, WRAPPER_SECRET, ADMIN_TOKEN, DEEPSEEK_API_KEY"
        ):
            validate_runtime_security(cfg)

    def test_revoke_disables_account_and_private_resources(self):
        ws = self.ws
        status, signup = ws.req("POST", "/v1/signup", {})
        self.assertEqual(status, 201)
        status, _ = ws.req(
            "POST",
            f"/admin/users/{signup['user_id']}/tier",
            {"tier": "pro"},
            headers=ws.admin_headers,
        )
        self.assertEqual(status, 200)
        user = ws.backend.store.get_user_by_api_key(signup["api_key"])
        account_id = new_id("acct")
        now = time.time()
        ws.backend.store._exec(
            "INSERT INTO account_identities("
            "id,user_id,provider,subject,email,email_verified,name,picture,created_at,updated_at"
            ") VALUES(?,?,?,?,?,1,?,?,?,?)",
            (account_id, user["id"], "google", "revoked-subject", "revoked@example.com", "Revoked", "", now, now),
        )
        revoked_device_id = str(uuid.uuid4())
        ws.backend.store.create_account_session(
            account_id=account_id,
            device_id=revoked_device_id,
            access_token="aga_" + "a" * 50,
            refresh_token="agr_" + "r" * 70,
            access_expires_at=now + 3600,
            refresh_expires_at=now + 7200,
        )
        connector_blob = ws.backend.encrypt_secret("{\"token\":\"private\"}", "connector-test")
        ws.backend.store.upsert_connector_credentials(
            user_id=user["id"],
            connector_id="github",
            credentials_enc=connector_blob,
            key_id="connector-test",
            key_version=ws.cfg.wrapper_secret_version,
            account_label="test",
        )
        ws.backend.store.claim_bot_computer(user["id"], "revoked-bot", "daytona", 2)
        grant = ws.backend.connectors.issue(
            user_id=user["id"], connector_ids=("github",), computer_id="revoked-bot"
        )
        status, body = ws.req("POST", f"/admin/users/{user['id']}/revoke", headers=ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertTrue(body["revoked"])
        self.assertEqual(body["ephemeral_grants_revoked"], 1)
        self.assertEqual(ws.backend.store.get_user_by_id(user["id"])["account_status"], "disabled")
        self.assertIsNone(ws.backend.store.get_user_by_api_key(signup["api_key"]))
        self.assertIsNone(ws.backend.store.get_user_by_access_token("aga_" + "a" * 50))
        refresh_status, _ = ws.req(
            "POST",
            "/v1/account-auth/refresh",
            {"device_id": revoked_device_id},
            headers={"Authorization": "Bearer " + "agr_" + "r" * 70},
        )
        self.assertEqual(refresh_status, 401)
        self.assertIsNone(ws.backend.store.get_connector_credentials(user["id"], "github"))
        self.assertIsNone(ws.backend.store.get_bot_computer(user["id"], "revoked-bot"))
        with self.assertRaises(ConnectorBrokerError):
            ws.backend.connectors.catalog(grant)



    # ---------- tiers ----------
    def test_signup_free_without_provider_credentials(self):
        ws = self.ws
        # free no necesita key del pool
        status, signup = ws.req("POST", "/v1/signup", {"name": "free-user", "tier": "free"})
        self.assertEqual(status, 201)
        self.assertEqual(signup["tier"], "free")
        self.assertEqual(signup["tier_label"], "Free Trial")
        self.assertNotIn("subscription_id", signup)
        self.assertEqual(signup["limits"]["5h"], 15.0)
        self.assertEqual(signup["limits"]["week"], 30.0)
        self.assertEqual(signup["limits"]["month"], 30.0)
        # El trial free sí puede usar modelos mientras tenga créditos.
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("POST", "/v1/chat/completions",
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers)
        self.assertEqual(status, 200)
        # /v1/usage refleja los límites de créditos del tier.
        status, usage = ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(usage["tier"], "free")
        self.assertEqual(usage["windows"]["5h"]["limit_credits"], 15.0)

    def test_paid_tiers_require_verified_admin_transition(self):
        ws = self.ws

        status, basic = ws.req("POST", "/v1/signup", {"name": "b", "tier": "basic"})
        self.assertEqual(status, 201)
        self.assertEqual(basic["tier"], "free")
        self.assertNotIn("subscription_id", basic)
        status, basic_upgrade = ws.req(
            "POST",
            f"/admin/users/{basic['user_id']}/tier",
            {"tier": "basic"},
            headers=ws.admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(basic_upgrade["tier"], "basic")
        self.assertNotIn("subscription_id", basic_upgrade)

        status, pro = ws.req("POST", "/v1/signup", {"name": "p", "tier": "pro"})
        self.assertEqual(status, 201)
        self.assertEqual(pro["tier"], "free")
        self.assertNotIn("subscription_id", pro)
        status, pro_upgrade = ws.req(
            "POST",
            f"/admin/users/{pro['user_id']}/tier",
            {"tier": "pro"},
            headers=ws.admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(pro_upgrade["tier"], "pro")
        self.assertNotIn("subscription_id", pro_upgrade)

        for signup, expected_tier, expected_limit in (
            (basic, "basic", 60.0),
            (pro, "pro", 200.0),
        ):
            headers = {"Authorization": f"Bearer {signup['api_key']}"}
            status, me = ws.req("GET", "/v1/me", headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(me["tier"], expected_tier)
            self.assertEqual(me["limits"]["5h"], expected_limit)

    def test_existing_database_default_is_migrated_to_free(self):
        db_path = Path(self.tmp) / "legacy.sqlite"
        connection = sqlite3.connect(db_path)
        connection.execute(
            """CREATE TABLE users (
              id TEXT PRIMARY KEY,
              name TEXT,
              email TEXT,
              api_key_hash TEXT UNIQUE NOT NULL,
              subscription_id TEXT,
              tier TEXT NOT NULL DEFAULT 'basic',
              created_at REAL NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO users VALUES('legacy', NULL, NULL, 'hash', NULL, 'basic', 1)"
        )
        connection.commit()
        connection.close()

        store = Store(db_path)
        tier_column = next(
            row for row in store._q("PRAGMA table_info(users)") if row["name"] == "tier"
        )
        self.assertEqual(tier_column["dflt_value"], "'free'")
        self.assertEqual(store.get_user_by_id("legacy")["tier"], "basic")

    def test_schema_v11_retires_provider_assignments_and_adds_run_cost_fields(self):
        db_path = Path(self.tmp) / "provider-migration.sqlite"
        store = Store(db_path)
        user = store.create_user("legacy-provider-user", "Legacy", None)
        store.add_subscription(
            b"encrypted-legacy-key",
            "legacy-key-id",
            "legacy",
            user_id=user["id"],
            sub_id="sub_legacy",
        )
        store.record_usage(
            user["id"], "sub_legacy", "deepseek-v4-flash", "/chat/completions",
            10, 5, 4, 0, 0.01, 200,
        )
        store.close()

        connection = sqlite3.connect(db_path)
        connection.execute("UPDATE kv SET v='9' WHERE k='schema_version'")
        connection.execute("ALTER TABLE usage_events RENAME TO usage_events_nullable")
        connection.execute(
            """CREATE TABLE usage_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL,
              subscription_id TEXT NOT NULL,
              model TEXT,
              endpoint TEXT,
              input_tokens INTEGER,
              output_tokens INTEGER,
              cached_read_tokens INTEGER,
              cached_write_tokens INTEGER,
              estimated_cost_usd REAL NOT NULL DEFAULT 0,
              status INTEGER,
              created_at REAL NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO usage_events(id,user_id,subscription_id,model,endpoint,input_tokens,"
            "output_tokens,cached_read_tokens,cached_write_tokens,estimated_cost_usd,status,created_at) "
            "SELECT id,user_id,subscription_id,model,endpoint,input_tokens,output_tokens,"
            "cached_read_tokens,cached_write_tokens,estimated_cost_usd,status,created_at "
            "FROM usage_events_nullable"
        )
        connection.execute("DROP TABLE usage_events_nullable")
        connection.commit()
        connection.close()

        migrated = Store(db_path)
        subscription_column = next(
            row
            for row in migrated._q("PRAGMA table_info(usage_events)")
            if row["name"] == "subscription_id"
        )
        self.assertEqual(subscription_column["notnull"], 0)
        usage_columns = {
            row["name"] for row in migrated._q("PRAGMA table_info(usage_events)")
        }
        self.assertIn("run_id", usage_columns)
        self.assertIn("estimated_cost_microusd", usage_columns)
        self.assertEqual(migrated.health()["schema_version"], 13)
        migrated_user = migrated.get_user_by_id(user["id"])
        self.assertIsNone(migrated_user["model_provider_override"])
        self.assertEqual(migrated_user["unlimited_usage"], 0)
        self.assertIsNone(migrated.get_user_by_id(user["id"])["subscription_id"])
        legacy = migrated.get_subscription("sub_legacy")
        self.assertEqual(legacy["status"], "revoked")
        self.assertIsNone(legacy["assigned_user_id"])
        self.assertEqual(len(migrated.usage_all()["events"]), 1)
        migrated.record_usage(
            user["id"], None, "deepseek-v4-flash", "/chat/completions",
            1, 1, 0, 0, 0.0, 200,
        )
        self.assertEqual(len(migrated.usage_all()["events"]), 2)

    def test_plan_catalog_starter_pro_and_business(self):
        ws = self.ws
        basic = self.new_user(tier="basic")
        pro = self.new_user(tier="pro")
        business = self.new_user(tier="business")
        basic_headers = {"Authorization": f"Bearer {basic['api_key']}"}
        pro_headers = {"Authorization": f"Bearer {pro['api_key']}"}
        business_headers = {"Authorization": f"Bearer {business['api_key']}"}
        status, me = ws.req("GET", "/v1/me", headers=basic_headers)
        self.assertEqual(status, 200)
        self.assertEqual(me["tier"], "basic")
        self.assertEqual(me["plan"]["label"], "Starter")
        self.assertEqual(me["plan"]["monthly_credit_milli"], 300_000)
        self.assertEqual(me["plan"]["five_hour_credit_milli"], 60_000)
        self.assertEqual(me["plan"]["seven_day_credit_milli"], 150_000)
        self.assertEqual(me["plan"]["max_concurrent_runs"], 1)
        status, me = ws.req("GET", "/v1/me", headers=pro_headers)
        self.assertEqual(me["tier"], "pro")
        self.assertEqual(me["plan"]["monthly_credit_milli"], 1_000_000)
        self.assertEqual(me["limits"], {"5h": 200.0, "week": 500.0, "month": 1000.0})
        self.assertEqual(me["plan"]["max_concurrent_runs"], 2)
        status, me = ws.req("GET", "/v1/me", headers=business_headers)
        self.assertEqual(me["tier"], "business")
        self.assertEqual(me["plan"]["monthly_credit_milli"], 3_000_000)
        self.assertEqual(me["limits"], {"5h": 600.0, "week": 1500.0, "month": 3000.0})
        self.assertEqual(me["plan"]["max_concurrent_runs"], 4)

    def test_admin_set_tier_changes_entitlement_without_provider_key(self):
        ws = self.ws
        # free sin suscripcion
        status, signup = ws.req("POST", "/v1/signup", {"tier": "free"})
        self.assertEqual(status, 201)
        user = ws.backend.store.get_user_by_api_key(signup["api_key"])
        self.assertIsNone(user["subscription_id"])
        status, body = ws.req("POST", f"/admin/users/{user['id']}/tier",
                           {"tier": "pro"}, headers=ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["tier"], "pro")
        self.assertNotIn("subscription_id", body)
        status, body = ws.req("POST", f"/admin/users/{user['id']}/tier",
                              {"tier": "free"}, headers=ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertNotIn("subscription_id", body)
        # tier invalido -> 400
        status, _ = ws.req("POST", f"/admin/users/{user['id']}/tier",
                           {"tier": "mega"}, headers=ws.admin_headers)
        self.assertEqual(status, 400)
        # admin listado incluye tier
        status, users = ws.req("GET", "/admin/users", headers=ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertTrue(any(u["id"] == user["id"] and u["tier"] == "free" for u in users["users"]))

    # ---------- Pi harness ----------
    def test_pi_disabled_by_default(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, info = self.ws.req("GET", "/v1/agent/status", headers=headers)
        self.assertEqual(status, 200)
        self.assertFalse(info["enabled"])
        self.assertFalse(info["image_input"])
        self.assertTrue(info["connectors_available"])
        self.assertEqual(info["connector_tool_loading"], "dynamic")
        self.assertEqual(info["connector_auth_scope"], "ephemeral_run")
        self.assertNotIn("vision", info)
        self.assertNotIn("binary", info)
        status, body = self.ws.req(
            "POST", "/v1/agent/run",
            {"prompt": "haz una tarea", "idempotency_key": "disabled-run"},
            headers=headers
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "pi_disabled")

    def test_pi_rpc_uses_wrapper_auth_and_server_deepseek_key(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()

        status, result = self.ws.req(
            "POST", "/v1/agent/run",
            {"prompt": "prueba end to end", "idempotency_key": "pi-e2e-run"},
            headers=headers
        )
        self.assertEqual(status, 200)
        self.assertIn("fake-pi uso deepseek-v4-flash: hola", result["answer"])
        self.assertEqual(result["usage"]["input_tokens"], 11)
        self.assertEqual(result["usage"]["cached_read_tokens"], 3)
        self.assertEqual(result["usage"]["llm_cost_microusd"], 2)
        self.assertEqual(result["usage"]["llm_cost_usd"], 0.000002)
        self.assertGreaterEqual(result["usage"]["duration_seconds"], 0)
        self.assertEqual(result["credits"]["charged"], 0.1)

        upstream_calls = [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(len(upstream_calls), 1)
        self.assertEqual(upstream_calls[0][2]["Authorization"], "Bearer sk-deepseek-server")

        status, usage = self.ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(usage["windows"]["5h"]["requests"], 1)
        status, all_usage = self.ws.req("GET", "/admin/usage", headers=self.ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(all_usage["events"][0]["input_tokens"], 10)
        self.assertEqual(all_usage["events"][0]["output_tokens"], 5)

    def test_pi_agent_run_streams_visible_text_before_final_payload(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()

        status, body, response_headers = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "__stream_json__",
                "bot_id": "streaming-bot",
                "stream": True,
                "idempotency_key": "pi-streaming-run",
            },
            headers=headers,
            raw=True,
            include_headers=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", response_headers["Content-Type"])
        self.assertEqual(response_headers["Transfer-Encoding"], "chunked")
        self.assertNotEqual(response_headers.get("Connection", "").lower(), "close")
        text = body.decode("utf-8")
        self.assertIn("event: start", text)
        self.assertIn("event: delta", text)
        self.assertIn("event: done64", text)
        frames = [frame for frame in text.split("\n\n") if frame]
        deltas = []
        final = None
        for frame in frames:
            lines = frame.splitlines()
            event = lines[0].removeprefix("event: ")
            if event == "delta":
                payload = json.loads(lines[1].removeprefix("data: "))
                deltas.append(payload["text"])
            elif event == "done64":
                final = base64.b64decode(
                    lines[1].removeprefix("data: ")
                ).decode("utf-8")
        self.assertEqual("".join(deltas), "hola rápido")
        self.assertEqual(
            json.loads(final),
            {"text": "hola rápido", "widget": None},
        )
        run_id = next(
            json.loads(frame.splitlines()[1].removeprefix("data: "))["run_id"]
            for frame in frames
            if frame.splitlines()[0] == "event: start"
        )
        saved_run = self.ws.backend.store.get_agent_run(run_id)
        timings = json.loads(saved_run["warnings_json"])[0]
        self.assertTrue(timings.startswith("timing:"))
        timing_payload = json.loads(timings.removeprefix("timing:"))
        for name in (
            "run_reserved_ms", "pi_dispatch_ms", "proxy_received_ms",
            "upstream_request_ms", "upstream_complete_ms", "pi_first_text_ms",
            "pi_complete_ms",
        ):
            self.assertIn(name, timing_payload)
        self.assertLessEqual(timing_payload["upstream_request_ms"], timing_payload["upstream_complete_ms"])
        self.assertLessEqual(timing_payload["pi_first_text_ms"], timing_payload["pi_complete_ms"])
        warnings = json.loads(saved_run["warnings_json"])
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("timing:"))

    def test_partial_json_text_waits_for_complete_unicode_surrogate_pair(self):
        prefix = '{"text":"hola \\ud83d'
        self.assertEqual(_partial_json_text(prefix), "hola ")
        self.assertEqual(
            _partial_json_text(prefix + '\\ude80 mundo"}'),
            "hola 🚀 mundo",
        )

    def test_pi_reuses_one_isolated_rpc_session_per_user_bot(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        results = []
        process_ids = []
        for index, prompt in enumerate(("primer mensaje", "segundo mensaje")):
            status, result = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": prompt,
                    "bot_id": "bot-persistente",
                    "idempotency_key": f"warm-session-{index}",
                },
                headers=headers,
            )
            self.assertEqual(status, 200)
            results.append(result)
            sessions = list(self.ws.backend.pi._sessions.values())
            self.assertEqual(len(sessions), 1)
            self.assertIsNotNone(sessions[0].process)
            process_ids.append(sessions[0].process.pid)

        self.assertEqual(process_ids[0], process_ids[1])
        self.assertNotEqual(results[0]["run_id"], results[1]["run_id"])
        session = next(iter(self.ws.backend.pi._sessions.values()))
        command = self.ws.backend.pi._command(False, session_id=session.session_id)
        self.assertIn("--session-id", command)
        self.assertNotIn("--no-session", command)
        self.assertIn(str(RUNTIME_AUTH_EXTENSION.resolve()), command)
        credentials = json.loads(session.auth_file.read_text(encoding="utf-8"))
        self.assertEqual(credentials, {"run_api_key": "", "connector_run_token": ""})
        upstream_calls = [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(len(upstream_calls), 2)

    def test_pi_prewarm_starts_the_same_isolated_session_without_model_usage(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()

        status, warmed = self.ws.req(
            "POST",
            "/v1/agent/warm",
            {"bot_id": "bot-precalentado"},
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(warmed["ready"])
        self.assertTrue(warmed["started"])
        session = next(iter(self.ws.backend.pi._sessions.values()))
        process_id = session.process.pid
        self.assertEqual(
            json.loads(session.auth_file.read_text(encoding="utf-8")),
            {"run_api_key": "", "connector_run_token": ""},
        )
        self.assertEqual(
            [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"],
            [],
        )

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "hola",
                "bot_id": "bot-precalentado",
                "idempotency_key": "prewarmed-session-run",
            },
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertIn("fake-pi", result["answer"])
        self.assertEqual(next(iter(self.ws.backend.pi._sessions.values())).process.pid, process_id)

    def test_pi_sessions_are_separated_by_account_and_bot(self):
        first = self.new_user()
        second = self.new_user()
        self.ws.enable_fake_pi()
        for index, signup in enumerate((first, second)):
            status, _result = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": "hola",
                    "bot_id": "same-visible-bot-id",
                    "idempotency_key": f"isolated-session-{index}",
                },
                headers={"Authorization": f"Bearer {signup['api_key']}"},
            )
            self.assertEqual(status, 200)
        sessions = list(self.ws.backend.pi._sessions.values())
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len({item.key for item in sessions}), 2)
        self.assertEqual(len({item.process.pid for item in sessions}), 2)

    def test_account_deletion_stops_and_erases_warm_pi_sessions(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        status, _result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "hola",
                "bot_id": "bot-to-delete",
                "idempotency_key": "delete-warm-session",
            },
            headers=headers,
        )
        self.assertEqual(status, 200)
        session = next(iter(self.ws.backend.pi._sessions.values()))
        root = session.root
        self.assertTrue(root.is_dir())
        status, body = self.ws.req(
            "POST",
            "/v1/account/delete",
            {"confirmation": "DELETE"},
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["pi_sessions_deleted"], 1)
        self.assertFalse(root.exists())
        self.assertEqual(self.ws.backend.pi._sessions, {})

    def test_pi_uses_native_deepseek_thinking_compatibility(self):
        config_dir = Path(self.tmp) / "pi-model-config"
        config_dir.mkdir()
        self.ws.backend.pi._write_config(config_dir)
        payload = json.loads((config_dir / "models.json").read_text(encoding="utf-8"))
        model = payload["providers"]["wrapper-backend"]["models"][0]
        self.assertEqual(model["input"], ["text"])
        self.assertEqual(model["compat"]["thinkingFormat"], "deepseek")
        self.assertTrue(model["compat"]["supportsReasoningEffort"])
        self.assertTrue(
            model["compat"]["requiresReasoningContentOnAssistantMessages"]
        )
        self.assertNotIn("extraBody", model)

    def test_connector_broker_scopes_catalog_and_execution_to_run_grant(self):
        signup = self.new_user()
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        adapter = FakeGitHubAdapter(user["id"])
        self.ws.backend.connectors.register_adapter("github", adapter)
        token = self.ws.backend.connectors.issue(
            user_id=user["id"], connector_ids=("github", "google-workspace")
        )
        internal_headers = {"X-Connector-Run-Token": token}

        status, catalog = self.ws.req(
            "GET", "/v1/internal/connectors/catalog", headers=internal_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in catalog["connectors"]],
            ["github", "google-workspace"],
        )
        github = next(item for item in catalog["connectors"] if item["id"] == "github")
        google = next(
            item for item in catalog["connectors"] if item["id"] == "google-workspace"
        )
        self.assertTrue(github["connected"])
        self.assertFalse(google["connected"])

        status, result = self.ws.req(
            "POST",
            "/v1/internal/connectors/execute",
            {
                "connector_id": "github",
                "operation": "search_repositories",
                "arguments": {"query": "wrapper"},
            },
            headers=internal_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["result"]["items"][0]["name"], "wrapper-backend")
        self.assertEqual(adapter.calls, [(user["id"], "search_repositories", {"query": "wrapper"})])

        status, body = self.ws.req(
            "POST",
            "/v1/internal/connectors/execute",
            {"connector_id": "slack", "operation": "list_channels", "arguments": {}},
            headers=internal_headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["type"], "connector_forbidden")

        self.ws.backend.connectors.revoke(token)
        status, body = self.ws.req(
            "GET", "/v1/internal/connectors/catalog", headers=internal_headers
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["type"], "connector_token_invalid")

    def test_connector_grant_expires_without_server_restart(self):
        clock = [time.time()]
        broker = ConnectorBroker(default_ttl_seconds=5, now=lambda: clock[0])
        token = broker.issue(user_id="user-a", connector_ids=("github",))
        self.assertEqual(broker.catalog(token)[0]["id"], "github")
        clock[0] += 6.0
        with self.assertRaises(ConnectorBrokerError) as error:
            broker.catalog(token)
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(error.exception.code, "connector_token_invalid")

    def test_composio_gateway_owns_auth_status_and_execution_by_wrapper_user(self):
        client = FakeComposioClient()
        clock = [time.time()]
        alice_id = self.new_user("composio-alice")["user_id"]
        bob_id = self.new_user("composio-bob")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            auth_configs={"salesforce": "ac_agentgenia_salesforce"},
            now=lambda: clock[0],
            store=self.ws.backend.store,
        )
        self.assertTrue(gateway.describe("github")["available"])
        self.assertTrue(gateway.describe("salesforce")["available"])
        self.assertFalse(gateway.status(alice_id, "github")["connected"])

        started = gateway.start(alice_id, "github")
        self.assertRegex(started["attempt_id"], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(
            started["authorize_url"],
            "https://connect.composio.dev/link/ca_1",
        )
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            auth_configs={"salesforce": "ac_agentgenia_salesforce"},
            now=lambda: clock[0],
            store=self.ws.backend.store,
        )
        self.assertEqual(gateway.poll(alice_id, started["attempt_id"]), {"status": "pending"})
        client.connected_accounts.items["ca_1"].status = "ACTIVE"
        self.assertEqual(gateway.poll(alice_id, started["attempt_id"]), {"status": "pending"})
        self.assertEqual(client.connected_accounts.get_calls, ["ca_1"])
        clock[0] += 2.1
        completed = gateway.poll(alice_id, started["attempt_id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["session"]["connector_id"], "github")
        with self.assertRaises(ConnectorBrokerError) as replay:
            gateway.poll(alice_id, started["attempt_id"])
        self.assertEqual(replay.exception.code, "connector_auth_not_found")
        self.assertTrue(gateway.status(alice_id, "github")["connected"])
        self.assertFalse(gateway.status(bob_id, "github")["connected"])

        client.connected_accounts.list_calls.clear()
        snapshot = gateway.snapshot(alice_id)
        github = next(item for item in snapshot if item["connector_id"] == "github")
        slack = next(item for item in snapshot if item["connector_id"] == "slack")
        self.assertTrue(github["connected"])
        self.assertFalse(slack["connected"])
        self.assertEqual(len(client.connected_accounts.list_calls), 1)
        self.assertIsNone(client.connected_accounts.list_calls[0]["toolkit_slugs"])

        adapter = ComposioConnectorAdapter(gateway, "github")
        self.assertTrue(adapter.is_connected(alice_id))
        result = adapter.execute(
            alice_id, "search_repositories", {"query": "wrapper"}
        )
        self.assertEqual(result["items"][0]["name"], "wrapper-backend")
        self.assertEqual(
            client.executions,
            [("GITHUB_SEARCH", {"query": "wrapper"})],
        )
        self.assertEqual(client.session_options[-1]["user_id"], alice_id)
        self.assertEqual(client.session_options[-1]["toolkits"], ["github"])
        self.assertFalse(client.session_options[-1]["manage_connections"])
        self.assertEqual(client.session_options[-1]["workbench"], {"enable": False})

        gateway.disconnect(alice_id, "github")
        self.assertFalse(gateway.status(alice_id, "github")["connected"])

    def test_composio_gateway_fails_closed_without_private_auth_config(self):
        gateway = ComposioConnectorGateway(
            client=FakeComposioClient(), store=self.ws.backend.store
        )
        salesforce = gateway.describe("salesforce")
        self.assertFalse(salesforce["available"])
        self.assertIn("Auth Config", salesforce["reason"])
        with self.assertRaises(ConnectorBrokerError) as error:
            gateway.start("usr_alice", "salesforce")
        self.assertEqual(error.exception.code, "connector_not_configured")
        health = gateway.health()
        self.assertFalse(health["all_connectors_available"])
        self.assertIn("salesforce", health["unavailable_connectors"])
        self.assertLess(health["available_connectors"], health["catalog_connectors"])

    def test_composio_gateway_rate_limits_new_links_per_user(self):
        user_id = self.new_user("rate-user")["user_id"]
        gateway = ComposioConnectorGateway(
            client=FakeComposioClient(), now=time.time, store=self.ws.backend.store
        )
        for _ in range(12):
            gateway.start(user_id, "github")
        with self.assertRaises(ConnectorBrokerError) as error:
            gateway.start(user_id, "github")
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.code, "connector_rate_limit")

    def test_native_connector_fallback_is_encrypted_isolated_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "native.sqlite")
            alice = store.create_user("alice-key", "Alice", "alice@example.com")
            bob = store.create_user("bob-key", "Bob", "bob@example.com")
            native = NativeConnectorGateway(
                store=store,
                secret_env="native-test-secret",
                secret_path=Path(tmp) / "secret.key",
                public_base_url="https://agentgenia-api.onrender.com",
            )
            gateway = ComposioConnectorGateway(
                client=FakeComposioClient(),
                native_gateway=native,
                store=store,
            )

            self.assertEqual(gateway.describe("nooks")["driver"], "native")
            self.assertTrue(gateway.describe("nooks")["available"])
            self.assertFalse(gateway.describe("loom")["available"])

            started = gateway.start(alice["id"], "nooks")
            self.assertTrue(started["attempt_id"].startswith("nat_"))
            page = native.setup_html(started["attempt_id"]).decode()
            self.assertIn("Conectar Nooks", page)
            self.assertIn('type="password"', page)

            complete_page = native.submit(
                started["attempt_id"],
                {"access_token": "nooks-api-super-secret", "account_label": "Ventas"},
            ).decode()
            self.assertIn("Cuenta conectada", complete_page)
            row = store.get_connector_credentials(alice["id"], "nooks")
            self.assertIsNotNone(row)
            self.assertNotIn(b"nooks-api-super-secret", bytes(row["credentials_enc"]))
            self.assertTrue(gateway.status(alice["id"], "nooks")["connected"])
            self.assertFalse(gateway.status(bob["id"], "nooks")["connected"])
            self.assertEqual(gateway.poll(alice["id"], started["attempt_id"])["status"], "complete")

            with (
                patch(
                    "go_backend.native_connectors.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
                ),
                patch(
                    "go_backend.native_connectors._request_json",
                    return_value={"data": [{"id": "call-1"}]},
                ) as request_json,
            ):
                result = gateway.execute(alice["id"], "nooks", "list_calls", {"page[size]": 25})
            self.assertEqual(result["data"][0]["id"], "call-1")
            url = request_json.call_args.args[0]
            self.assertIn("partner-api.nooks.in/v1/calls", url)
            self.assertIn("page%5Bsize%5D=25", url)
            self.assertEqual(
                request_json.call_args.kwargs["headers"]["Authorization"],
                "Bearer nooks-api-super-secret",
            )

            gateway.disconnect(alice["id"], "nooks")
            self.assertFalse(gateway.status(alice["id"], "nooks")["connected"])

    def test_connector_account_routes_are_authenticated_and_execution_is_internal_only(self):
        status, body = self.ws.req("GET", "/v1/connectors")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["type"], "unauthorized")

        signup = self.new_user(tier="free")
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = self.ws.req("GET", "/v1/connectors", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["connectors"]), len(CONNECTOR_CATALOG))
        self.assertTrue(all(not item["connected"] for item in body["connectors"]))

        status, _body = self.ws.req(
            "POST",
            "/v1/connectors/execute",
            {"connector_id": "github", "operation": "search_repositories", "arguments": {}},
            headers=headers,
        )
        self.assertEqual(status, 404)

    def test_pi_connectors_are_passed_to_child_and_revoked_after_run(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        captured_tokens: list[str] = []
        original_issue = self.ws.backend.connectors.issue

        def issue_and_capture(**kwargs):
            token = original_issue(**kwargs)
            captured_tokens.append(token)
            return token

        self.ws.backend.connectors.issue = issue_and_capture
        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "consulta mis repositorios y calendario",
                "connector_ids": ["github", "google-workspace", "github"],
                "idempotency_key": "connector-run",
            },
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["connector_ids"], ["github", "google-workspace"])
        run_dir = self.ws.backend.pi.runs_dir / result["run_id"]
        catalog = json.loads((run_dir / "config" / "connector-catalog.json").read_text())
        self.assertEqual(
            [item["id"] for item in catalog["connectors"]],
            ["github", "google-workspace"],
        )
        self.assertEqual(len(captured_tokens), 1)
        status, body = self.ws.req(
            "GET",
            "/v1/internal/connectors/catalog",
            headers={"X-Connector-Run-Token": captured_tokens[0]},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["type"], "connector_token_invalid")

        command = self.ws.backend.pi._command(browser=False)
        extension_paths = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--extension"
        ]
        self.assertEqual(extension_paths, [str(Path("extensions/connectors/index.ts").resolve())])

    def test_agent_rejects_unknown_connector_before_starting_pi(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        status, body = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "haz algo",
                "connector_ids": ["unknown-provider"],
                "idempotency_key": "unknown-connector-run",
            },
            headers=headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "bad_connector")

    def test_pi_chrome_uses_a_fresh_profile_and_bridge_for_each_run(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi(browser=True)

        status, info = self.ws.req("GET", "/v1/agent/status", headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(info["browser_available"])
        self.assertTrue(info["browser_auto_authorize"])
        self.assertEqual(info["browser_isolation"], "per_run")
        self.assertEqual(info["browser_profile_scope"], "ephemeral_run")
        self.assertFalse(
            self.ws.backend.pi._supports_unpacked_extensions(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            )
        )
        self.assertTrue(
            self.ws.backend.pi._supports_unpacked_extensions(
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        command = self.ws.backend.pi._command(browser=True)
        extension_paths = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--extension"
        ]
        self.assertEqual(len(extension_paths), 2)
        self.assertEqual(extension_paths[0], str(Path("extensions/connectors/index.ts").resolve()))
        self.assertEqual(extension_paths[1], str(Path(self.ws.backend.pi.chrome_extension).resolve()))

        results = []
        for prompt in ("revisa la pagina", "abre otra sesion"):
            status, result = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": prompt,
                    "browser": True,
                    "idempotency_key": f"chrome-run-{len(results)}",
                },
                headers=headers,
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["browser"])
            self.assertIn("fake-pi uso deepseek-v4-flash: hola", result["answer"])
            results.append(result)

        run_dirs = [self.ws.backend.pi.runs_dir / result["run_id"] for result in results]
        self.assertNotEqual(run_dirs[0], run_dirs[1])
        bridge_ports = []
        for run_dir in run_dirs:
            self.assertFalse((run_dir / "chrome-profile").exists())
            worker = (run_dir / "chrome-extension" / "service_worker.js").read_text()
            self.assertNotIn("127.0.0.1:17318", worker)
            bridge_port = (run_dir / "config" / "chrome-bridge-port.txt").read_text().strip()
            self.assertIn(f"127.0.0.1:{bridge_port}", worker)
            manifest = json.loads(
                (run_dir / "chrome-extension" / "manifest.json").read_text()
            )
            self.assertIn(
                f"http://127.0.0.1:{bridge_port}/*", manifest["host_permissions"]
            )
            bridge_ports.append(bridge_port)

        launches = [
            json.loads(line)
            for line in self.ws.backend.pi.chrome_test_log.read_text().splitlines()
        ]
        self.assertEqual(len(launches), 2)
        profile_args = []
        for launch, run_dir in zip(launches, run_dirs):
            profile_arg = f"--user-data-dir={run_dir / 'chrome-profile'}"
            extension_arg = f"--load-extension={run_dir / 'chrome-extension'}"
            self.assertIn(profile_arg, launch["argv"])
            self.assertIn(extension_arg, launch["argv"])
            self.assertFalse(launch["has_admin_token"])
            profile_args.append(profile_arg)
        self.assertNotEqual(profile_args[0], profile_args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
