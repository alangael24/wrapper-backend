"""Tests de integracion del wrapper backend contra un upstream mock.

No se hace ninguna llamada real a OpenCode Go.
"""

from __future__ import annotations

import json
import http.client
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_backend.server import (  # noqa: E402
    Backend,
    Config,
    Handler,
    UnsafeConfigurationError,
    serve,
)
from go_backend.connectors import ConnectorBroker, ConnectorBrokerError  # noqa: E402
from go_backend.store import NoSubscriptionAvailable, Store  # noqa: E402


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []
    fail_luna = False
    fail_mimo = False

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
            catalog = {"object": "list", "data": [{"id": "deepseek-v4-flash", "object": "model", "owned_by": "opencode"}]}
            self._send(200, json.dumps(catalog).encode())
        else:
            self._send(404, json.dumps({"error": "not found"}).encode())

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        payload = json.loads(body) if body else {}
        if self.path == "/v1/chat/completions":
            if payload.get("model") == "mimo-v2.5":
                if type(self).fail_mimo:
                    self._send(503, json.dumps({"error": {"message": "mimo unavailable"}}).encode())
                    return
                resp = {
                    "id": "vision-fallback", "model": "mimo-v2.5",
                    "choices": [{"message": {"role": "assistant", "content": "IMAGE 1: panel azul, texto OK"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                }
                self._send(200, json.dumps(resp).encode())
                return
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
                              "input_tokens_details": {"cached_tokens": 4}},
                }
                self._send(200, json.dumps(resp).encode())
        elif self.path == "/v1/responses":
            if payload.get("model") == "gpt-5.6-luna":
                if type(self).fail_luna:
                    self._send(503, json.dumps({"error": {"message": "luna unavailable"}}).encode())
                    return
                resp = {
                    "id": "vision-luna", "model": "gpt-5.6-luna",
                    "output_text": "IMAGE 1: panel azul, texto OK",
                    "output": [],
                    "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
                }
                self._send(200, json.dumps(resp).encode())
                return
            resp = {
                "id": "resp-test", "model": "deepseek-v4-flash",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hola"}]}],
                "usage": {"input_tokens": 20, "output_tokens": 7, "total_tokens": 27,
                          "input_tokens_details": {"cached_tokens": 3}},
            }
            self._send(200, json.dumps(resp).encode())
        elif self.path == "/v1/messages":
            resp = {
                "id": "msg-test", "model": "deepseek-v4-flash",
                "content": [{"type": "text", "text": "hola"}],
                "usage": {"input_tokens": 30, "output_tokens": 4,
                          "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
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
        os.environ["GO_BASE_URL"] = upstream_base
        os.environ["DB_PATH"] = os.path.join(tmp, "test.sqlite")
        os.environ["SECRET_FILE"] = os.path.join(tmp, "secret.key")
        os.environ["ADMIN_TOKEN"] = "test-admin"
        os.environ["ENFORCE_LIMITS"] = "1"
        os.environ["VISION_ENABLED"] = "1"
        os.environ["VISION_MODEL"] = "gpt-5.6-luna"
        os.environ["VISION_FALLBACK_MODEL"] = "mimo-v2.5"
        os.environ["VISION_TARGET_MODELS"] = "deepseek-v4"
        os.environ["PI_ENABLED"] = "0"
        os.environ.pop("WRAPPER_SECRET", None)
        os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", None)
        os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)
        os.environ.pop("GOOGLE_OAUTH_REDIRECT_URI", None)
        for name in (
            "STRIPE_ENABLED", "STRIPE_LIVE_MODE", "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET", "STRIPE_PLUS_PRICE_ID", "STRIPE_PRO_PRICE_ID",
            "STRIPE_SUCCESS_URL", "STRIPE_CANCEL_URL", "STRIPE_PORTAL_RETURN_URL",
        ):
            os.environ.pop(name, None)
        self.cfg = Config()
        self.cfg.go_base_url = upstream_base + "/v1"
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
        self.backend.pi.backend_url = self.base
        self.backend.pi.connector_broker_url = self.base
        self.backend.pi.runs_dir = Path(self.cfg.db_path).parent / "pi-runs"
        self.backend.pi.timeout_seconds = 5
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
        MockUpstream.fail_luna = False
        MockUpstream.fail_mimo = False

    def tearDown(self):
        self.ws.stop()
        MockUpstream.requests.clear()
        MockUpstream.fail_luna = False
        MockUpstream.fail_mimo = False


    # ---------- helpers ----------
    def add_pool_keys(self, n=1, prefix="sk-go-"):
        keys = [f"{prefix}{i:04d}" for i in range(n)]
        status, body = self.ws.req("POST", "/admin/subscriptions", {"keys": keys}, self.ws.admin_headers)
        self.assertEqual(status, 201)
        return body

    def new_user(self, name=None, tier="pro"):
        status, signup = self.ws.req("POST", "/v1/signup", {"name": name} if name else {})
        self.assertEqual(status, 201)
        self.assertEqual(signup["tier"], "free")
        if tier != "free":
            self.add_pool_keys(1)
            status, upgraded = self.ws.req(
                "POST",
                f"/admin/users/{signup['user_id']}/tier",
                {"tier": tier},
                headers=self.ws.admin_headers,
            )
            self.assertEqual(status, 200)
            signup["tier"] = tier
            signup["subscription_id"] = upgraded["subscription_id"]
        return signup

    def image_data_url(self):
        # Firma PNG suficiente para probar normalizacion, deduplicacion y routing.
        return "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"

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

        status, completed = self.ws.req(
            "GET",
            f"/v1/account-auth/status/{started['attempt_id']}?device_id={device_id}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "complete")
        self.assertTrue(completed["token"].startswith("aga_"))
        self.assertTrue(completed["refresh_token"].startswith("agr_"))
        self.assertTrue(completed["account"]["id"].startswith("acct_"))

        status, me = self.ws.req(
            "GET", "/v1/me", headers={"Authorization": f"Bearer {completed['token']}"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(me["email"], "alan@example.com")
        self.assertEqual(me["tier"], "free")

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
            "GET", f"/v1/account-auth/status/{started['attempt_id']}?device_id={device_id}"
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
            "GET", f"/v1/account-auth/status/{started['attempt_id']}?device_id={device_id}"
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("verificada", result["message"])

    def test_public_signup_always_free_and_never_consumes_pool(self):
        ws = self.ws
        created = self.add_pool_keys(2, prefix="sk-go-x")
        self.assertEqual(len(created["created"]), 2)

        for requested_tier in (None, "basic", "pro", "ultra"):
            payload = {"name": f"usuario-{requested_tier or 'default'}"}
            if requested_tier is not None:
                payload["tier"] = requested_tier
            status, body = ws.req("POST", "/v1/signup", payload)
            self.assertEqual(status, 201)
            self.assertIn("api_key", body)
            self.assertEqual(len(body["api_key"]), 64)
            self.assertEqual(body["tier"], "free")
            self.assertIsNone(body["subscription_id"])
            self.assertEqual(body["subscription_status"], "none")
            self.assertEqual(body["available_left"], 2)

        self.assertEqual(ws.backend.store.available_count(), 2)

    def test_keys_encrypted_at_rest(self):
        self.add_pool_keys(1)
        store = self.ws.backend.store
        for sub in store.list_subscriptions():
            blob = sub["api_key_enc"]
            self.assertTrue(blob.startswith((b"aes:", b"kc:")))
            self.assertNotIn(b"sk-go-", blob)

    def test_admin_auth_required(self):
        status, _ = self.ws.req("POST", "/admin/subscriptions", {"keys": ["x"]})
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

    def test_models_proxy(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("GET", "/v1/models", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["id"], "deepseek-v4-flash")

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

    def test_responses_and_messages(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("POST", "/v1/responses",
                              {"model": "deepseek-v4-flash", "input": "hi"}, headers=headers)
        self.assertEqual(status, 200)
        status, body = ws.req("POST", "/v1/messages",
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("cache_read_input_tokens", body["usage"])
        status, usage = ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(usage["windows"]["5h"]["requests"], 2)

    def test_responses_images_are_analyzed_by_luna_for_deepseek(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        payload = {
            "model": "deepseek-v4-flash",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Que texto aparece?"},
                    {"type": "input_image", "image_url": self.image_data_url()},
                ],
            }],
        }

        status, body, response_headers = self.ws.req(
            "POST", "/v1/responses", payload, headers=headers, include_headers=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-Wrapper-Vision-Model"], "gpt-5.6-luna")
        self.assertEqual(body["output"][0]["content"][0]["text"], "hola")

        luna_calls = self.upstream_payloads("/v1/responses", "gpt-5.6-luna")
        deepseek_calls = self.upstream_payloads("/v1/responses", "deepseek-v4-flash")
        self.assertEqual(len(luna_calls), 1)
        self.assertEqual(len(deepseek_calls), 1)
        self.assertIn("input_image", json.dumps(luna_calls[0]))
        forwarded = json.dumps(deepseek_calls[0])
        self.assertNotIn("input_image", forwarded)
        self.assertIn("VISION_SUBSYSTEM_REPORT", forwarded)
        self.assertIn("untrusted visual evidence", forwarded)

        _, usage = self.ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(usage["windows"]["5h"]["requests"], 2)
        self.assertIn("gpt-5.6-luna", usage["by_model"])
        self.assertIn("deepseek-v4-flash", usage["by_model"])
        _, admin_usage = self.ws.req("GET", "/admin/usage", headers=self.ws.admin_headers)
        self.assertEqual(
            {event["endpoint"] for event in admin_usage["events"]},
            {"/vision/responses", "/responses"},
        )

    def test_chat_images_are_converted_for_pi_protocol(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        payload = {
            "model": "deepseek-v4-flash",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe el panel"},
                    {"type": "image_url", "image_url": {"url": self.image_data_url()}},
                ],
            }],
        }

        status, _body, response_headers = self.ws.req(
            "POST", "/v1/chat/completions", payload, headers=headers, include_headers=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-Wrapper-Vision-Model"], "gpt-5.6-luna")
        forwarded = json.dumps(
            self.upstream_payloads("/v1/chat/completions", "deepseek-v4-flash")[0]
        )
        self.assertNotIn("image_url", forwarded)
        self.assertIn("VISION_SUBSYSTEM_REPORT", forwarded)

    def test_anthropic_message_images_are_converted(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        encoded_image = self.image_data_url().split(",", 1)[1]
        payload = {
            "model": "deepseek-v4-pro",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Que ves?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": encoded_image,
                        },
                    },
                ],
            }],
        }

        status, _body, response_headers = self.ws.req(
            "POST", "/v1/messages", payload, headers=headers, include_headers=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-Wrapper-Vision-Model"], "gpt-5.6-luna")
        forwarded = json.dumps(self.upstream_payloads("/v1/messages", "deepseek-v4-pro")[0])
        self.assertNotIn('"type": "image"', forwarded)
        self.assertIn("VISION_SUBSYSTEM_REPORT", forwarded)

    def test_streaming_with_images_preserves_sse_and_vision_header(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        payload = {
            "model": "deepseek-v4-flash",
            "stream": True,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Lee la captura"},
                    {"type": "image_url", "image_url": {"url": self.image_data_url()}},
                ],
            }],
        }
        status, body, response_headers = self.ws.req(
            "POST",
            "/v1/chat/completions",
            payload,
            headers=headers,
            raw=True,
            include_headers=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-Wrapper-Vision-Model"], "gpt-5.6-luna")
        self.assertIn(b"data:", body)
        self.assertIn(b"[DONE]", body)

    def test_luna_failure_falls_back_to_mimo(self):
        MockUpstream.fail_luna = True
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        payload = {
            "model": "deepseek-v4-flash",
            "input": [{
                "role": "user",
                "content": [{"type": "input_image", "image_url": self.image_data_url()}],
            }],
        }

        status, _body, response_headers = self.ws.req(
            "POST", "/v1/responses", payload, headers=headers, include_headers=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-Wrapper-Vision-Model"], "mimo-v2.5")
        self.assertEqual(len(self.upstream_payloads("/v1/responses", "gpt-5.6-luna")), 1)
        self.assertEqual(len(self.upstream_payloads("/v1/chat/completions", "mimo-v2.5")), 1)
        forwarded = json.dumps(
            self.upstream_payloads("/v1/responses", "deepseek-v4-flash")[0]
        )
        self.assertIn('model=\\"mimo-v2.5\\"', forwarded)

    def test_vision_cache_includes_the_user_prompt(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}

        def request(prompt):
            return self.ws.req(
                "POST",
                "/v1/responses",
                {
                    "model": "deepseek-v4-flash",
                    "input": [{
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": self.image_data_url()},
                        ],
                    }],
                },
                headers=headers,
            )

        self.assertEqual(request("Lee el titulo")[0], 200)
        self.assertEqual(request("Lee el titulo")[0], 200)
        self.assertEqual(request("Lee el precio")[0], 200)
        self.assertEqual(len(self.upstream_payloads("/v1/responses", "gpt-5.6-luna")), 2)
        self.assertEqual(len(self.upstream_payloads("/v1/responses", "deepseek-v4-flash")), 3)

    def test_vision_failure_does_not_send_images_to_deepseek(self):
        MockUpstream.fail_luna = True
        MockUpstream.fail_mimo = True
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = self.ws.req(
            "POST",
            "/v1/responses",
            {
                "model": "deepseek-v4-flash",
                "input": [{
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": self.image_data_url()}],
                }],
            },
            headers=headers,
        )
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["type"], "vision_error")
        self.assertEqual(len(self.upstream_payloads("/v1/responses", "deepseek-v4-flash")), 0)

    def test_visual_request_limit_prevents_unbounded_auxiliary_calls(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        images = [
            {"type": "input_image", "image_url": f"https://example.test/image-{index}.png"}
            for index in range(13)
        ]
        status, body = self.ws.req(
            "POST",
            "/v1/responses",
            {
                "model": "deepseek-v4-flash",
                "input": [{"role": "user", "content": images}],
            },
            headers=headers,
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["type"], "vision_limit")
        self.assertEqual(len(self.upstream_payloads("/v1/responses")), 0)

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

    def test_usage_limit_429(self):
        ws = self.ws
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        # inyectar uso que supera el limite de 5h ($12)
        sub_id = ws.backend.store.get_user_by_api_key(signup["api_key"])["subscription_id"]
        for _ in range(3):
            ws.backend.store.record_usage(
                signup["user_id"], sub_id, "deepseek-v4-flash", "/chat/completions",
                1000000, 1000000, 0, 0, 6.0, 200,
            )
        status, body = ws.req("POST", "/v1/chat/completions",
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers)
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["type"], "usage_limit")

    def test_auth_required(self):
        status, _ = self.ws.req("GET", "/v1/models")
        self.assertEqual(status, 401)
        status, _ = self.ws.req("GET", "/v1/usage", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_byok(self):
        ws = self.ws
        signup = self.new_user("byok-user")
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        # agrega su propia key de Go
        status, body = ws.req("POST", "/v1/byok", {"apiKey": "sk-go-personal"}, headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(body["source"], "byok")
        # el usuario ahora usa SU key (no la del pool)
        status, models = ws.req("GET", "/v1/models", headers=headers)
        self.assertEqual(status, 200)
        auths = [r[2]["Authorization"] for r in MockUpstream.requests]
        self.assertIn("Bearer sk-go-personal", auths)

    def test_revoke_returns_to_pool(self):
        ws = self.ws
        ws.req("POST", "/admin/subscriptions", {"keys": ["sk-go-zzz"]}, ws.admin_headers)
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
        status, body = ws.req("POST", f"/admin/users/{user['id']}/revoke", headers=ws.admin_headers)
        self.assertEqual(status, 200)
        sub = ws.backend.store.get_subscription(user["subscription_id"])
        self.assertEqual(sub["status"], "available")
        self.assertEqual(ws.backend.store.available_count(), 1)



    # ---------- tiers ----------
    def test_signup_free_without_pool(self):
        ws = self.ws
        # free no necesita key del pool
        status, signup = ws.req("POST", "/v1/signup", {"name": "free-user", "tier": "free"})
        self.assertEqual(status, 201)
        self.assertEqual(signup["tier"], "free")
        self.assertEqual(signup["tier_label"], "Free")
        self.assertIsNone(signup["subscription_id"])
        self.assertEqual(signup["subscription_status"], "none")
        self.assertEqual(signup["limits"]["5h"], 0.0)
        self.assertEqual(signup["limits"]["week"], 0.0)
        self.assertEqual(signup["limits"]["month"], 0.0)
        # free no puede llamar modelos
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, body = ws.req("POST", "/v1/chat/completions",
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                              headers=headers)
        self.assertEqual(status, 402)
        self.assertEqual(body["error"]["type"], "tier_requires_upgrade")
        # /v1/usage refleja el tier y limites en cero
        status, usage = ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(usage["tier"], "free")
        self.assertEqual(usage["windows"]["5h"]["limit_usd"], 0.0)

    def test_paid_tiers_require_verified_admin_transition(self):
        ws = self.ws
        self.add_pool_keys(2)

        status, basic = ws.req("POST", "/v1/signup", {"name": "b", "tier": "basic"})
        self.assertEqual(status, 201)
        self.assertEqual(basic["tier"], "free")
        self.assertIsNone(basic["subscription_id"])
        status, basic_upgrade = ws.req(
            "POST",
            f"/admin/users/{basic['user_id']}/tier",
            {"tier": "basic"},
            headers=ws.admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(basic_upgrade["tier"], "basic")
        self.assertIsNotNone(basic_upgrade["subscription_id"])

        status, pro = ws.req("POST", "/v1/signup", {"name": "p", "tier": "pro"})
        self.assertEqual(status, 201)
        self.assertEqual(pro["tier"], "free")
        self.assertIsNone(pro["subscription_id"])
        status, pro_upgrade = ws.req(
            "POST",
            f"/admin/users/{pro['user_id']}/tier",
            {"tier": "pro"},
            headers=ws.admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(pro_upgrade["tier"], "pro")
        self.assertIsNotNone(pro_upgrade["subscription_id"])

        for signup, expected_tier, expected_limit in (
            (basic, "basic", 6.0),
            (pro, "pro", 12.0),
        ):
            headers = {"Authorization": f"Bearer {signup['api_key']}"}
            status, me = ws.req("GET", "/v1/me", headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(me["tier"], expected_tier)
            self.assertEqual(me["limits"]["5h"], expected_limit)

    def test_atomic_tier_transition_cannot_double_assign_subscription(self):
        db_path = Path(self.tmp) / "race.sqlite"
        store_a = Store(db_path)
        store_b = Store(db_path)
        user_a = store_a.create_user("race-api-a", "a", None)
        user_b = store_a.create_user("race-api-b", "b", None)
        store_a.add_subscription(b"encrypted", "race-key", "race", sub_id="sub_race")
        barrier = threading.Barrier(3)
        outcomes: list[tuple[str, str]] = []

        def upgrade(store, user_id):
            barrier.wait()
            try:
                store.transition_user_tier(user_id, "pro", needs_subscription=True)
                outcomes.append((user_id, "assigned"))
            except NoSubscriptionAvailable:
                outcomes.append((user_id, "no_capacity"))

        threads = [
            threading.Thread(target=upgrade, args=(store_a, user_a["id"])),
            threading.Thread(target=upgrade, args=(store_b, user_b["id"])),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sorted(outcome for _, outcome in outcomes), ["assigned", "no_capacity"])
        assigned_users = [
            user for user in store_a.list_users() if user["subscription_id"] == "sub_race"
        ]
        self.assertEqual(len(assigned_users), 1)
        subscription = store_a.get_subscription("sub_race")
        self.assertEqual(subscription["assigned_user_id"], assigned_users[0]["id"])
        index_sql = store_a._one(  # noqa: SLF001 - verifica la defensa del esquema
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uniq_user_subscription'"
        )["sql"]
        self.assertIn("WHERE subscription_id IS NOT NULL", index_sql)

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

    def test_usage_limits_basic_vs_pro(self):
        ws = self.ws
        basic = self.new_user(tier="basic")
        pro = self.new_user(tier="pro")
        basic_headers = {"Authorization": f"Bearer {basic['api_key']}"}
        pro_headers = {"Authorization": f"Bearer {pro['api_key']}"}
        basic_user = ws.backend.store.get_user_by_api_key(basic["api_key"])
        pro_user = ws.backend.store.get_user_by_api_key(pro["api_key"])
        payload = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}

        def inject(user, n):
            for _ in range(n):
                ws.backend.store.record_usage(
                    user["id"], user["subscription_id"], "deepseek-v4-flash", "/chat/completions",
                    1000000, 1000000, 0, 0, 3.0, 200,
                )

        # basic: $6 de uso -> alcanza su limite 5h ($6) -> 429
        inject(basic_user, 2)
        status, body = ws.req("POST", "/v1/chat/completions", payload, headers=basic_headers)
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["type"], "usage_limit")
        # pro: mismo $6 de uso -> sigue por debajo de $12 -> 200
        inject(pro_user, 2)
        status, _ = ws.req("POST", "/v1/chat/completions", payload, headers=pro_headers)
        self.assertEqual(status, 200)
        # pro: sube a $15 -> 429
        inject(pro_user, 3)
        status, body = ws.req("POST", "/v1/chat/completions", payload, headers=pro_headers)
        self.assertEqual(status, 429)
        # /v1/me muestra tier y limites correctos
        status, me = ws.req("GET", "/v1/me", headers=basic_headers)
        self.assertEqual(status, 200)
        self.assertEqual(me["tier"], "basic")
        self.assertEqual(me["limits"]["5h"], 6.0)
        status, me = ws.req("GET", "/v1/me", headers=pro_headers)
        self.assertEqual(me["tier"], "pro")
        self.assertEqual(me["limits"]["5h"], 12.0)

    def test_admin_set_tier_assigns_and_releases_subscription(self):
        ws = self.ws
        # free sin suscripcion
        status, signup = ws.req("POST", "/v1/signup", {"tier": "free"})
        self.assertEqual(status, 201)
        user = ws.backend.store.get_user_by_api_key(signup["api_key"])
        self.assertIsNone(user["subscription_id"])
        # subir a pro sin pool -> 409
        status, _ = ws.req("POST", f"/admin/users/{user['id']}/tier",
                           {"tier": "pro"}, headers=ws.admin_headers)
        self.assertEqual(status, 409)
        # agregar pool y subir a pro -> asigna suscripcion
        self.add_pool_keys(1, prefix="sk-tier-")
        status, body = ws.req("POST", f"/admin/users/{user['id']}/tier",
                              {"tier": "pro"}, headers=ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["tier"], "pro")
        self.assertIsNotNone(body["subscription_id"])
        self.assertEqual(ws.backend.store.available_count(), 0)
        # bajar a free -> libera la suscripcion al pool
        status, body = ws.req("POST", f"/admin/users/{user['id']}/tier",
                              {"tier": "free"}, headers=ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertIsNone(body["subscription_id"])
        self.assertEqual(ws.backend.store.available_count(), 1)
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
        self.assertTrue(info["image_input"])
        self.assertTrue(info["connectors_available"])
        self.assertEqual(info["connector_tool_loading"], "dynamic")
        self.assertEqual(info["connector_auth_scope"], "ephemeral_run")
        self.assertTrue(info["vision"]["enabled"])
        self.assertEqual(info["vision"]["primary_model"], "gpt-5.6-luna")
        self.assertNotIn("binary", info)
        status, body = self.ws.req(
            "POST", "/v1/agent/run", {"prompt": "haz una tarea"}, headers=headers
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "pi_disabled")

    def test_pi_rpc_uses_wrapper_key_and_assigned_go_key(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()

        status, result = self.ws.req(
            "POST", "/v1/agent/run", {"prompt": "prueba end to end"}, headers=headers
        )
        self.assertEqual(status, 200)
        self.assertIn("fake-pi uso deepseek-v4-flash: hola", result["answer"])
        self.assertEqual(result["usage"]["input_tokens"], 11)
        self.assertEqual(result["usage"]["cached_read_tokens"], 3)

        upstream_calls = [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(len(upstream_calls), 1)
        self.assertEqual(upstream_calls[0][2]["Authorization"], "Bearer sk-go-0000")

        status, usage = self.ws.req("GET", "/v1/usage", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(usage["windows"]["5h"]["requests"], 1)
        status, all_usage = self.ws.req("GET", "/admin/usage", headers=self.ws.admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(all_usage["events"][0]["input_tokens"], 10)
        self.assertEqual(all_usage["events"][0]["output_tokens"], 5)

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
        clock = [100.0]
        broker = ConnectorBroker(default_ttl_seconds=5, now=lambda: clock[0])
        token = broker.issue(user_id="user-a", connector_ids=("github",))
        self.assertEqual(broker.catalog(token)[0]["id"], "github")
        clock[0] = 106.0
        with self.assertRaises(ConnectorBrokerError) as error:
            broker.catalog(token)
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(error.exception.code, "connector_token_invalid")

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
            {"prompt": "haz algo", "connector_ids": ["unknown-provider"]},
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
                {"prompt": prompt, "browser": True},
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
