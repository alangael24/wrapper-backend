"""Tests de integracion del wrapper backend contra un upstream mock.

No se hace ninguna llamada real a OpenCode Go.
"""

from __future__ import annotations

import json
import http.client
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_backend.server import Config, Backend, Handler, serve  # noqa: E402
from go_backend.store import Store  # noqa: E402


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
        os.environ["PI_ENABLED"] = "0"
        os.environ.pop("WRAPPER_SECRET", None)
        self.cfg = Config()
        self.cfg.go_base_url = upstream_base + "/v1"
        self.backend = Backend(self.cfg)
        Handler.backend = self.backend
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.admin_headers = {"Authorization": "Bearer test-admin"}

    def enable_fake_pi(self):
        fake_pi = Path(__file__).resolve().parent / "fake_pi.py"
        self.backend.pi.enabled = True
        self.backend.pi.binary = str(fake_pi)
        self.backend.pi.backend_url = self.base
        self.backend.pi.runs_dir = Path(self.cfg.db_path).parent / "pi-runs"
        self.backend.pi.timeout_seconds = 5

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, headers=None, raw=False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                content = resp.read()
                return resp.status, (content if raw else (json.loads(content) if content else None))
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                parsed = json.loads(content) if content else None
            except Exception:
                parsed = content
            return e.code, parsed


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
    def add_pool_keys(self, n=1, prefix="sk-go-"):
        keys = [f"{prefix}{i:04d}" for i in range(n)]
        status, body = self.ws.req("POST", "/admin/subscriptions", {"keys": keys}, self.ws.admin_headers)
        self.assertEqual(status, 201)
        return body

    def new_user(self, name=None):
        self.add_pool_keys(1)
        status, signup = self.ws.req("POST", "/v1/signup", {"name": name} if name else {})
        self.assertEqual(status, 201)
        return signup

    # ---------- pool / signup ----------
    def test_signup_assigns_subscription(self):
        ws = self.ws
        # pool vacio -> 409
        status, body = ws.req("POST", "/v1/signup", {"name": "a"})
        self.assertEqual(status, 409)
        # agregar 2 keys al pool
        body = self.add_pool_keys(2, prefix="sk-go-x")
        self.assertEqual(len(body["created"]), 2)
        # primer signup -> asigna una
        status, body = ws.req("POST", "/v1/signup", {"name": "usuario-uno", "email": "u1@x.com"})
        self.assertEqual(status, 201)
        self.assertIn("api_key", body)
        self.assertTrue(body["api_key"].startswith("test") or len(body["api_key"]) == 64)
        self.assertEqual(body["subscription_status"], "assigned")
        self.assertEqual(body["available_left"], 1)
        # segundo signup -> asigna la segunda
        status2, body2 = ws.req("POST", "/v1/signup", {"name": "usuario-dos"})
        self.assertEqual(status2, 201)
        self.assertEqual(body2["available_left"], 0)
        # pool vacio otra vez -> 409
        status3, _ = ws.req("POST", "/v1/signup", {"name": "usuario-tres"})
        self.assertEqual(status3, 409)

    def test_keys_encrypted_at_rest(self):
        store = self.ws.backend.store
        for sub in store.list_subscriptions():
            blob = sub["api_key_enc"]
            self.assertTrue(blob.startswith(b"aes:"))
            self.assertNotIn(b"sk-go-", blob)

    def test_admin_auth_required(self):
        status, _ = self.ws.req("POST", "/admin/subscriptions", {"keys": ["x"]})
        self.assertEqual(status, 401)

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

    def test_signup_tier_invalid(self):
        status, body = self.ws.req("POST", "/v1/signup", {"tier": "ultra"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "bad_tier")

    def test_signup_basic_pro_require_pool(self):
        ws = self.ws
        # sin pool -> 409 para basic y pro
        status, _ = ws.req("POST", "/v1/signup", {"tier": "basic"})
        self.assertEqual(status, 409)
        status, _ = ws.req("POST", "/v1/signup", {"tier": "pro"})
        self.assertEqual(status, 409)
        # con pool -> se asignan y el tier queda registrado
        self.add_pool_keys(2)
        status, basic = ws.req("POST", "/v1/signup", {"name": "b", "tier": "basic"})
        self.assertEqual(status, 201)
        self.assertEqual(basic["tier"], "basic")
        self.assertIsNotNone(basic["subscription_id"])
        self.assertEqual(basic["limits"]["5h"], 6.0)
        self.assertEqual(basic["limits"]["week"], 15.0)
        self.assertEqual(basic["limits"]["month"], 30.0)
        status, pro = ws.req("POST", "/v1/signup", {"name": "p", "tier": "pro"})
        self.assertEqual(status, 201)
        self.assertEqual(pro["tier"], "pro")
        self.assertEqual(pro["limits"]["5h"], 12.0)
        self.assertEqual(pro["limits"]["week"], 30.0)
        self.assertEqual(pro["limits"]["month"], 60.0)

    def test_usage_limits_basic_vs_pro(self):
        ws = self.ws
        self.add_pool_keys(2)
        _, basic = ws.req("POST", "/v1/signup", {"tier": "basic"})
        _, pro = ws.req("POST", "/v1/signup", {"tier": "pro"})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
