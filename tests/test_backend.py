"""Integration tests against a mock DeepSeek-compatible upstream."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import http.client
import os
import re
import sqlite3
import socket
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
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_backend.server import (  # noqa: E402
    Backend,
    Config,
    Handler,
    UnsafeConfigurationError,
    _bounded_agent_envelope,
    _connector_operation_is_read_only,
    _partial_json_text,
    serve,
    validate_runtime_security,
)
from go_backend.connectors import (  # noqa: E402
    CONNECTOR_CATALOG,
    ConnectorBroker,
    ConnectorBrokerError,
    canonical_arguments_hash,
)
from go_backend.connector_adapters import (  # noqa: E402
    ComposioConnectorAdapter,
    ComposioConnectorGateway,
    _compact_connector_result,
)
from go_backend.native_connectors import NativeConnectorGateway  # noqa: E402
from go_backend.google_auth import GoogleAccountAuth  # noqa: E402
from go_backend.pi_harness import RUNTIME_AUTH_EXTENSION  # noqa: E402
from go_backend.store import Store, new_id  # noqa: E402
from go_backend.whatsapp import (  # noqa: E402
    WhatsAppCloudAPI,
    WhatsAppConfig,
    parse_webhook_messages,
    verify_webhook_signature,
)
from go_backend.whatsapp_agent import (  # noqa: E402
    connector_command,
    create_bot_from_request,
    extract_link_code,
    requested_bot,
    wants_bot_list,
)


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
                content = "\n".join(
                    str(message.get("content", ""))
                    for message in payload.get("messages", [])
                    if isinstance(message, dict)
                )
                if "__empty_stream_retry__" in content:
                    self._send(
                        200,
                        b'data: {"id":"cmpl-empty","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                        b'data: {"id":"cmpl-empty","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":0,"total_tokens":3}}\n\n'
                        b'data: [DONE]\n\n',
                        ctype="text/event-stream",
                    )
                    return
                if "__partial_structured__" in content:
                    partial = '{"text":"Saludo visible","widget":{"prompt":"¿Qué deseas hacer?"'
                    frame = {
                        "id": "cmpl-partial",
                        "object": "chat.completion.chunk",
                        "model": "deepseek-v4-flash",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": partial},
                            "finish_reason": "length",
                        }],
                    }
                    self._send(
                        200,
                        (
                            f"data: {json.dumps(frame)}\n\n"
                            "data: [DONE]\n\n"
                        ).encode(),
                        ctype="text/event-stream",
                    )
                    return
                self._send(
                    200,
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"FINAL: hola"},"finish_reason":null}]}\n\n'
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                    b'data: {"id":"cmpl-stream","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
                    b'data: [DONE]\n\n',
                    ctype="text/event-stream",
                )
            else:
                content = "\n".join(
                    str(message.get("content", ""))
                    for message in payload.get("messages", [])
                    if isinstance(message, dict)
                )
                expects_agent_envelope = (
                    "Devuelve exclusivamente JSON válido" in content
                    and '"text":"respuesta visible"' in content
                    and '"widget"' in content
                )
                resp = {
                    "id": "cmpl-test", "model": "deepseek-v4-flash",
                    "choices": [{"message": {
                        "role": "assistant",
                        "content": (
                            "respuesta recuperada"
                            if "__empty_stream_retry__" in content
                            else '{"text":"Saludo reparado","widget":{"prompt":"¿Qué deseas hacer?","options":[{"label":"Empezar"}]}}'
                            if "__partial_structured__" in content
                            else '{"text":"hola","widget":null}'
                            if expects_agent_envelope
                            else "hola"
                        ),
                    }}],
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
            "COMPOSIO_DIRECT_AUTH_CONFIGS_JSON",
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
        for name in (
            "WHATSAPP_ENABLED", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
            "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_PUBLIC_NUMBER",
            "WHATSAPP_GRAPH_VERSION", "WHATSAPP_LINK_TTL_SECONDS",
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


class FakeContractAdapter:
    """Provider double used to exercise every public connector operation."""

    def __init__(self, connected_user_id: str):
        self.connected_user_id = connected_user_id
        self.validations: list[tuple[str, str, dict]] = []
        self.calls: list[tuple[str, str, dict]] = []

    def is_connected(self, user_id: str) -> bool:
        return user_id == self.connected_user_id

    def normalize_arguments(self, operation: str, arguments: dict) -> dict:
        prepared = dict(arguments)
        provider_alias = prepared.pop("provider_alias", None)
        if provider_alias is not None:
            prepared["contract_case"] = provider_alias
        return prepared

    def validate_arguments(
        self, user_id: str, operation: str, arguments: dict
    ) -> dict:
        self.validations.append((user_id, operation, arguments))
        if arguments.get("__invalid__"):
            raise ConnectorBrokerError(
                400, f"Argumentos inválidos para {operation}", "bad_connector_arguments"
            )
        return self.normalize_arguments(operation, arguments)

    def execute(self, user_id: str, operation: str, arguments: dict):
        self.calls.append((user_id, operation, arguments))
        return {"ok": True, "operation": operation, "arguments": arguments}


class FakeComposioAccounts:
    def __init__(self):
        self.items: dict[str, SimpleNamespace] = {}
        self.list_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.initiate_calls: list[dict] = []
        self.link_calls: list[dict] = []

    def initiate(self, user_id, auth_config_id, **options):
        account_id = f"ca_{len(self.items) + 1}"
        toolkit = {
            "ac_agentgenia_salesforce": "salesforce",
        }.get(auth_config_id, "custom")
        self.initiate_calls.append({
            "user_id": user_id,
            "auth_config_id": auth_config_id,
            **options,
        })
        self.items[account_id] = SimpleNamespace(
            id=account_id,
            user_id=user_id,
            toolkit=toolkit,
            status="INITIATED",
            alias="",
            data={},
            status_reason="",
        )
        return SimpleNamespace(
            id=account_id,
            redirect_url=(
                "https://login.salesforce.com/services/oauth2/authorize"
                f"?state={account_id}"
            ),
        )

    def link(self, user_id, auth_config_id, **options):
        account_id = f"ca_{len(self.items) + 1}"
        toolkit = {
            "ac_agentgenia_salesforce": "salesforce",
        }.get(auth_config_id, "custom")
        self.link_calls.append({
            "user_id": user_id,
            "auth_config_id": auth_config_id,
            **options,
        })
        self.items[account_id] = SimpleNamespace(
            id=account_id,
            user_id=user_id,
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
        match = re.search(r"operation '([a-z0-9_]+)'", query)
        operation = (match.group(1) if match else "search").upper()
        slug = f"{self.toolkit.upper()}_{operation}"
        return SimpleNamespace(
            results=[SimpleNamespace(primary_tool_slugs=[slug])],
            tool_schemas=self.client.tool_schemas,
        )

    def execute(self, slug, *, arguments):
        self.client.executions.append((slug, arguments))
        if slug in self.client.execution_results:
            return SimpleNamespace(
                data=self.client.execution_results[slug], error=None
            )
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


class FakeComposioAuthConfigs:
    def __init__(self):
        self.items = {
            "ac_agentgenia_salesforce": SimpleNamespace(
                is_composio_managed=False,
                type="custom",
            ),
        }
        self.get_calls: list[str] = []

    def get(self, auth_config_id):
        self.get_calls.append(auth_config_id)
        return self.items[auth_config_id]


class FakeComposioClient:
    def __init__(self):
        self.connected_accounts = FakeComposioAccounts()
        self.auth_configs = FakeComposioAuthConfigs()
        self.sessions = FakeComposioSessions(self)
        self.session_options: list[dict] = []
        self.searches: list[tuple[str, str]] = []
        self.executions: list[tuple[str, dict]] = []
        self.tool_schemas: dict[str, dict] = {}
        self.execution_results: dict[str, dict] = {}


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

    def assign_bot_connectors(self, signup, connector_ids, *, bot_id=None, messages=None):
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        status, current = self.ws.req("GET", "/v1/account-state", headers=headers)
        self.assertEqual(status, 200)
        bot_id = bot_id or str(uuid.uuid4())
        now = "2026-08-17T12:00:00Z"
        state = current["state"]
        state["bots"] = [{
            "id": bot_id,
            "name": "Test bot",
            "color": "#2f91f5",
            "shape": "circle",
            "connectorIds": list(connector_ids),
            "messages": list(messages or []),
            "workflows": [],
            "createdAt": now,
            "updatedAt": now,
        }]
        state["activeBotId"] = bot_id
        state["selectedConnectorIds"] = list(connector_ids)
        status, saved = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": current["revision"], "device_id": str(uuid.uuid4()), "state": state},
            headers=headers,
        )
        self.assertEqual(status, 200)
        return saved["state"]["bots"][0]["id"]

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

    def configure_fake_whatsapp(self):
        config = WhatsAppConfig(
            enabled=True,
            verify_token="verify-token-for-agentgenia-tests",
            app_secret="app-secret-for-agentgenia-tests",
            access_token="access-token-for-agentgenia-tests",
            phone_number_id="123456789012345",
            public_number="15551234567",
            graph_version="v23.0",
            link_ttl_seconds=600,
        )
        self.ws.backend.whatsapp = WhatsAppCloudAPI(config)
        sent: list[dict] = []

        def send_text(*, to, text, reply_to_message_id=None):
            sent.append({
                "to": to,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
            })
            return f"wamid.outbound.{len(sent)}"

        self.ws.backend.whatsapp.send_text = send_text
        return config, sent

    def whatsapp_payload(self, message_id: str, text: str, *, sender="15557654321"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "waba-test",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "+1 555 123 4567",
                            "phone_number_id": "123456789012345",
                        },
                        "contacts": [{
                            "profile": {"name": "Alan WhatsApp"},
                            "wa_id": sender,
                        }],
                        "messages": [{
                            "from": sender,
                            "id": message_id,
                            "timestamp": "1786680000",
                            "text": {"body": text},
                            "type": "text",
                        }],
                    },
                }],
            }],
        }

    def send_whatsapp_webhook(self, payload, app_secret):
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(
            app_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return self.ws.req(
            "POST",
            "/v1/whatsapp/webhook",
            payload,
            headers={"X-Hub-Signature-256": signature},
        )

    # ---------- pool / signup ----------
    def test_whatsapp_helpers_verify_and_route_without_a_public_ai_endpoint(self):
        config, _sent = self.configure_fake_whatsapp()
        payload = self.whatsapp_payload("wamid.helper", "Mis agentes")
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(
            config.app_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(verify_webhook_signature(body, signature, config.app_secret))
        self.assertFalse(verify_webhook_signature(body + b" ", signature, config.app_secret))
        messages = parse_webhook_messages(payload)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Mis agentes")
        bsuid_payload = self.whatsapp_payload("wamid.bsuid", "hola")
        bsuid_message = bsuid_payload["entry"][0]["changes"][0]["value"]["messages"][0]
        bsuid_message["from"] = ""
        bsuid_message["user_id"] = "bsuid_test_contact"
        self.assertEqual(
            parse_webhook_messages(bsuid_payload)[0]["wa_user_id"],
            "bsuid_test_contact",
        )
        self.assertTrue(wants_bot_list("¿Cuáles son mis bots?"))
        self.assertEqual(extract_link_code("Vincular Agentgenia ag-abcd-2345"), "AG-ABCD-2345")
        created = create_bot_from_request("Crea un agente para preparar cotizaciones")
        self.assertIsNotNone(created)
        self.assertIn("cotizaciones", created["description"])
        self.assertEqual(
            requested_bot({"bots": [{"id": "sales", "name": "Ventas"}]}, "usa Ventas"),
            {"id": "sales", "name": "Ventas"},
        )
        self.assertEqual(connector_command("Conecta Gmail"), ("connect", "google-workspace"))
        self.assertEqual(connector_command("desconecta Salesforce"), ("disconnect", "salesforce"))
        self.assertEqual(connector_command("¿Cuáles son mis conexiones?"), ("list", None))
        self.assertEqual(connector_command("listo"), ("refresh", None))

    def test_whatsapp_can_manage_connectors_and_assign_them_to_the_active_bot(self):
        class FakeConnectorGateway:
            def __init__(self, store):
                self.store = store
                self.connected: set[str] = set()
                self.started: list[tuple[str, str]] = []
                self.disconnected: list[tuple[str, str]] = []

            def status(self, user_id, connector_id):
                return {"connector_id": connector_id, "connected": connector_id in self.connected}

            def start(self, user_id, connector_id):
                self.started.append((user_id, connector_id))
                self.store.create_connector_auth_attempt(
                    attempt_id="attempt-whatsapp",
                    user_id=user_id,
                    connector_id=connector_id,
                    driver="composio",
                    connected_account_id="account-whatsapp",
                    expires_at=time.time() + 600,
                )
                return {
                    "attempt_id": "attempt-whatsapp",
                    "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?state=secure",
                }

            def snapshot(self, _user_id):
                return [
                    {"connector_id": connector_id, "connected": connector_id in self.connected}
                    for connector_id in CONNECTOR_CATALOG
                ]

            def disconnect(self, user_id, connector_id):
                self.disconnected.append((user_id, connector_id))
                self.connected.discard(connector_id)
                return {"disconnected": True}

        config, sent = self.configure_fake_whatsapp()
        user = self.new_user(tier="pro")
        auth = {"Authorization": f"Bearer {user['api_key']}"}
        status, started = self.ws.req("POST", "/v1/whatsapp/link", {}, headers=auth)
        self.assertEqual(status, 201)
        self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.connector.link", f"Vincular Agentgenia {started['code']}"),
            config.app_secret,
        )
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.connector.bot", "Crea un agente para ventas"),
            config.app_secret,
        )
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )

        gateway = FakeConnectorGateway(self.ws.backend.store)
        self.ws.backend.connector_gateway = gateway
        self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.connector.start", "Conecta Gmail"),
            config.app_secret,
        )
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        self.assertEqual(gateway.started, [(user["user_id"], "google-workspace")])
        self.assertIn("accounts.google.com", sent[-1]["text"])
        self.assertIn("escribe “listo”", sent[-1]["text"])
        self.assertNotIn("Composio", sent[-1]["text"])

        gateway.connected.update({"google-workspace", "canva"})
        self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.connector.ready", "listo"),
            config.app_secret,
        )
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        self.assertIn("Google Workspace", sent[-1]["text"])
        self.assertIn("Canva", sent[-1]["text"])
        status, state = self.ws.req("GET", "/v1/account-state", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(
            set(state["state"]["selectedConnectorIds"]),
            {"google-workspace", "canva"},
        )
        self.assertEqual(
            set(state["state"]["bots"][0]["connectorIds"]),
            {"google-workspace", "canva"},
        )

        self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.connector.remove", "Desconecta Gmail"),
            config.app_secret,
        )
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        self.assertEqual(gateway.disconnected, [(user["user_id"], "google-workspace")])
        status, state = self.ws.req("GET", "/v1/account-state", headers=auth)
        self.assertEqual(state["state"]["selectedConnectorIds"], ["canva"])
        self.assertEqual(state["state"]["bots"][0]["connectorIds"], ["canva"])

        captured: dict = {}

        def fake_agent_run(internal):
            captured.update(json.loads(internal.rfile.read()))
            internal.send_response(200)
            internal.wfile.write(json.dumps({"answer": '{"text":"hecho","widget":null}'}).encode())

        with patch.object(self.ws.backend, "handle_agent_run", fake_agent_run):
            self.send_whatsapp_webhook(
                self.whatsapp_payload("wamid.connector.task", "Haz una presentación"),
                config.app_secret,
            )
            self.ws.backend._process_whatsapp_message(
                self.ws.backend.store.claim_whatsapp_message()
            )
        self.assertEqual(captured["connector_ids"], ["canva"])
        self.assertEqual(sent[-1]["text"], "hecho")

    def test_whatsapp_link_is_one_time_and_messages_share_account_state_and_fast_path(self):
        config, sent = self.configure_fake_whatsapp()
        self.ws.enable_fake_pi()
        user = self.new_user(tier="pro")
        auth = {"Authorization": f"Bearer {user['api_key']}"}

        status, initial = self.ws.req("GET", "/v1/whatsapp/status", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(initial["configured"])
        self.assertFalse(initial["connected"])

        status, started = self.ws.req("POST", "/v1/whatsapp/link", {}, headers=auth)
        self.assertEqual(status, 201)
        self.assertRegex(started["code"], r"^AG-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertTrue(started["url"].startswith("https://wa.me/15551234567?"))

        # Issuing a new code atomically invalidates the previous one and keeps
        # a single live record even with a durable multi-replica store.
        first_code = started["code"]
        status, started = self.ws.req("POST", "/v1/whatsapp/link", {}, headers=auth)
        self.assertEqual(status, 201)
        self.assertNotEqual(started["code"], first_code)
        codes = self.ws.backend.store._q(
            "SELECT code_hash FROM whatsapp_link_codes WHERE user_id=?", (user["user_id"],)
        )
        self.assertEqual(len(codes), 1)

        status, accepted = self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.link", f"Vincular Agentgenia {started['code']}"),
            config.app_secret,
        )
        self.assertEqual(status, 200)
        self.assertEqual(accepted["accepted"], 1)
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        self.assertIn("quedó vinculado", sent[-1]["text"])

        # Meta retries the same event; the durable inbox must not process it twice.
        status, duplicate = self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.link", f"Vincular Agentgenia {started['code']}"),
            config.app_secret,
        )
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["accepted"], 0)
        self.assertIsNone(self.ws.backend.store.claim_whatsapp_message())

        status, linked = self.ws.req("GET", "/v1/whatsapp/status", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(linked["connected"])
        self.assertEqual(linked["display_name"], "Alan WhatsApp")
        self.assertEqual(linked["phone_hint"], "••••4321")

        # Creating from WhatsApp mutates the exact account state consumed by the apps.
        status, queued = self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.create", "Crea un agente para preparar cotizaciones"),
            config.app_secret,
        )
        self.assertEqual(status, 200)
        self.assertEqual(queued["accepted"], 1)
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        status, state = self.ws.req("GET", "/v1/account-state", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(len(state["state"]["bots"]), 1)
        bot = state["state"]["bots"][0]
        self.assertIn("cotizaciones", bot["description"])

        status, queued = self.send_whatsapp_webhook(
            self.whatsapp_payload("wamid.task", "Prepara el resumen de hoy"),
            config.app_secret,
        )
        self.assertEqual(status, 200)
        self.assertEqual(queued["accepted"], 1)
        self.ws.backend._process_whatsapp_message(
            self.ws.backend.store.claim_whatsapp_message()
        )
        self.assertEqual(sent[-1]["text"], "hola")
        self.assertEqual(self.ws.backend.pi._sessions, {})
        status, state = self.ws.req("GET", "/v1/account-state", headers=auth)
        messages = state["state"]["bots"][0]["messages"]
        self.assertEqual([item["role"] for item in messages[-2:]], ["user", "assistant"])
        self.assertEqual(messages[-2]["text"], "Prepara el resumen de hoy")

        status, result = self.ws.req("POST", "/v1/whatsapp/unlink", {}, headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(result["disconnected"])

    def test_whatsapp_webhook_rejects_invalid_signature_and_ignores_unlinked_chat(self):
        config, sent = self.configure_fake_whatsapp()
        payload = self.whatsapp_payload("wamid.unlinked", "hola")
        status, result = self.ws.req(
            "POST",
            "/v1/whatsapp/webhook",
            payload,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )
        self.assertEqual(status, 401)
        self.assertEqual(result["error"]["type"], "invalid_signature")

        status, accepted = self.send_whatsapp_webhook(payload, config.app_secret)
        self.assertEqual(status, 200)
        self.assertEqual(accepted["accepted"], 1)
        message = self.ws.backend.store.claim_whatsapp_message()
        self.ws.backend._process_whatsapp_message(message)
        self.assertEqual(sent, [])
        stored = self.ws.backend.store._one(
            "SELECT status FROM whatsapp_messages WHERE message_id=?", ("wamid.unlinked",)
        )
        self.assertEqual(stored["status"], "ignored")

    def test_whatsapp_uncertain_outbound_delivery_is_never_retried_automatically(self):
        config, _sent = self.configure_fake_whatsapp()
        payload = self.whatsapp_payload("wamid.uncertain", "hola")
        status, accepted = self.send_whatsapp_webhook(payload, config.app_secret)
        self.assertEqual(status, 200)
        self.assertEqual(accepted["accepted"], 1)
        message = self.ws.backend.store.claim_whatsapp_message()
        self.assertEqual(message["status"], "processing")

        # Simulate a process/network failure after the delivery was claimed,
        # when Meta may already have accepted the outbound message.
        self.ws.backend.store.prepare_whatsapp_outbound(
            message_id="wamid.uncertain",
            result_text="respuesta final",
        )
        self.ws.backend.store.retry_whatsapp_message(
            message_id="wamid.uncertain",
            error="socket closed",
        )
        stored = self.ws.backend.store._one(
            "SELECT status,last_error FROM whatsapp_messages WHERE message_id=?",
            ("wamid.uncertain",),
        )
        self.assertEqual(stored["status"], "failed")
        self.assertIn("outbound_delivery_uncertain", stored["last_error"])
        self.assertIsNone(self.ws.backend.store.claim_whatsapp_message())

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
                "workflows": [{
                    "id": "broken-recording",
                    "title": "Broken",
                    "steps": ["Open the app"],
                    "createdAt": "not-a-date",
                    "updatedAt": "not-a-date",
                }, {
                    "id": "valid-recording",
                    "title": "Daily report",
                    "steps": ["Open the report", "Send it"],
                    "createdAt": "2026-08-13T20:00:00Z",
                    "updatedAt": "2026-08-13T20:01:00Z",
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
        self.assertEqual(saved["state"]["version"], 2)
        self.assertEqual(saved["state"]["selectedConnectorIds"], ["github"])
        self.assertEqual(saved["state"]["bots"][0]["name"], "Research bot")
        # Legacy desktop/WhatsApp ids are migrated at the server boundary so
        # every native client receives the same UUID-safe identity.
        uuid.UUID(saved["state"]["bots"][0]["id"])
        uuid.UUID(saved["state"]["bots"][0]["messages"][0]["id"])
        self.assertEqual(
            [item["id"] for item in saved["state"]["bots"][0]["workflows"]],
            ["valid-recording"],
        )

        canonical_bot_id = saved["state"]["bots"][0]["id"]
        deleted_state = {
            **saved["state"],
            "deletedBotIds": [canonical_bot_id],
        }
        status, deleted = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 1, "device_id": device_id, "state": deleted_state},
            headers=first_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(deleted["state"]["deletedBotIds"], [canonical_bot_id])
        self.assertEqual(deleted["state"]["bots"], [])

        status, other = self.ws.req("GET", "/v1/account-state", headers=second_headers)
        self.assertEqual(status, 200)
        self.assertEqual(other["revision"], 0)

        stale_state = {**saved["state"], "activeBotId": None}
        status, conflict = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 1, "device_id": device_id, "state": stale_state},
            headers=first_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["type"], "account_state_conflict")
        self.assertEqual(conflict["current"]["revision"], 2)

        status, invalid = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 2, "device_id": "not-a-device", "state": state},
            headers=first_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["type"], "invalid_account_state")

    def test_account_state_collapses_duplicate_completion_only_within_one_turn(self):
        signup = self.new_user(tier="free")
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        bot_id = str(uuid.uuid4())
        messages = [
            {
                "id": str(uuid.uuid4()), "role": "user", "text": "Hola",
                "createdAt": "2026-08-17T12:00:00Z",
            },
            {
                "id": str(uuid.uuid4()), "role": "assistant", "text": "Listo",
                "createdAt": "2026-08-17T12:00:01Z",
            },
            {
                "id": str(uuid.uuid4()), "role": "assistant", "text": "Listo",
                "createdAt": "2026-08-17T12:00:02Z",
            },
            {
                "id": str(uuid.uuid4()), "role": "user", "text": "Otra vez",
                "createdAt": "2026-08-17T12:00:03Z",
            },
            {
                "id": str(uuid.uuid4()), "role": "assistant", "text": "Listo",
                "createdAt": "2026-08-17T12:00:04Z",
            },
        ]
        state = {
            "version": 2,
            "onboardingCompleted": True,
            "selectedConnectorIds": [],
            "activeBotId": bot_id,
            "bots": [{
                "id": bot_id,
                "name": "Asistente",
                "color": "#2f91f5",
                "shape": "circle",
                "connectorIds": [],
                "messages": messages,
                "workflows": [],
                "createdAt": "2026-08-17T12:00:00Z",
                "updatedAt": "2026-08-17T12:00:04Z",
            }],
        }

        status, saved = self.ws.req(
            "POST", "/v1/account-state",
            {
                "base_revision": 0,
                "device_id": str(uuid.uuid4()),
                "state": state,
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        saved_messages = saved["state"]["bots"][0]["messages"]
        self.assertEqual(
            [(message["role"], message["text"]) for message in saved_messages],
            [("user", "Hola"), ("assistant", "Listo"),
             ("user", "Otra vez"), ("assistant", "Listo")],
        )

    def test_server_tombstone_rejects_a_bot_resurrected_by_stale_device(self):
        signup = self.new_user(tier="free")
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        device_id = str(uuid.uuid4())
        bot_id = str(uuid.uuid4())
        created = {
            "version": 2,
            "onboardingCompleted": True,
            "selectedConnectorIds": [],
            "activeBotId": bot_id,
            "deletedBotIds": [],
            "bots": [{
                "id": bot_id,
                "name": "Offline bot",
                "color": "#2f91f5",
                "shape": "circle",
                "connectorIds": [],
                "messages": [],
                "workflows": [],
                "createdAt": "2026-08-17T12:00:00Z",
                "updatedAt": "2026-08-17T12:00:00Z",
            }],
        }
        status, first = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 0, "device_id": device_id, "state": created},
            headers=headers,
        )
        self.assertEqual(status, 200)
        deleted = {
            **first["state"],
            "bots": [],
            "activeBotId": None,
            "deletedBotIds": [bot_id],
        }
        status, removed = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 1, "device_id": device_id, "state": deleted},
            headers=headers,
        )
        self.assertEqual(status, 200)

        # A device that was offline before deletion no longer carries the
        # bounded client tombstone. The durable server record still wins.
        status, replayed = self.ws.req(
            "POST", "/v1/account-state",
            {
                "base_revision": removed["revision"],
                "device_id": str(uuid.uuid4()),
                "state": created,
            },
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(replayed["state"]["bots"], [])
        self.assertIsNone(replayed["state"]["activeBotId"])

    def test_account_state_rejects_oversized_child_and_persists_pending_run(self):
        signup = self.new_user(tier="free")
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        bot_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        base = {
            "version": 2,
            "onboardingCompleted": True,
            "selectedConnectorIds": [],
            "activeBotId": bot_id,
            "deletedBotIds": [],
            "bots": [{
                "id": bot_id, "name": "Recoverable", "color": "#2f91f5", "shape": "circle",
                "connectorIds": [], "workflows": [],
                "messages": [{
                    "id": turn_id, "role": "user", "text": "Hazlo",
                    "createdAt": "2026-08-17T20:00:00Z",
                }],
                "createdAt": "2026-08-17T19:00:00Z",
                "updatedAt": "2026-08-17T20:00:00Z",
            }],
            "pendingRuns": [{
                "turnId": turn_id, "idempotencyKey": turn_id, "runId": "",
                "botId": bot_id, "status": "pending",
                "submittedAt": "2026-08-17T20:00:00Z", "lastRecoveryAt": None,
            }],
        }
        status, saved = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": 0, "device_id": str(uuid.uuid4()), "state": base},
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["state"]["pendingRuns"][0]["idempotencyKey"], turn_id)

        oversized = json.loads(json.dumps(saved["state"]))
        oversized["bots"][0]["messages"] = [
            {"id": str(uuid.uuid4()), "role": "user", "text": str(index),
             "createdAt": "2026-08-17T20:00:00Z"}
            for index in range(201)
        ]
        status, invalid = self.ws.req(
            "POST", "/v1/account-state",
            {"base_revision": saved["revision"], "device_id": str(uuid.uuid4()), "state": oversized},
            headers=headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["type"], "invalid_account_state")
        self.assertIn("state.bots[0]", invalid["error"]["message"])

        status, unchanged = self.ws.req("GET", "/v1/account-state", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(unchanged["state"]["bots"][0]["id"], bot_id)

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

        # Every replica checks shared session storage so logout/revocation is
        # immediately visible across the fleet.
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
            self.assertEqual(lookup.call_count, 2)

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

    def test_production_desktop_runtime_requires_a_public_https_wrapper_url(self):
        cfg = Config()
        cfg.environment = "production"
        cfg.database_url = "postgresql://example.invalid/agentgenia"
        cfg.wrapper_secret = "w" * 32
        cfg.admin_token = "a" * 32
        cfg.deepseek_api_key = "sk-production-test"
        cfg.deepseek_base_url = "https://api.deepseek.com/v1"
        cfg.opencode_base_url = "https://opencode.ai/zen/v1"
        cfg.google_oauth_client_id = "google-client"
        cfg.google_oauth_client_secret = "google-secret"
        cfg.google_oauth_redirect_uri = "https://agentgenia.example/oauth/google"
        cfg.apple_client_id = "com.agentgenia.app"
        cfg.apple_team_id = "TEAMID"
        cfg.apple_key_id = "KEYID"
        cfg.apple_private_key_base64 = "cHJpdmF0ZS1rZXk="
        cfg.pi_enabled = True
        cfg.desktop_runtime_public_url = "http://127.0.0.1:8787"
        with self.assertRaisesRegex(
            UnsafeConfigurationError, "DESKTOP_RUNTIME_PUBLIC_URL debe ser una URL HTTPS pública"
        ):
            validate_runtime_security(cfg)

    def test_browser_concurrency_can_be_disabled_on_the_server(self):
        cfg = Config()
        cfg.pi_browser_max_concurrent = 0
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

        MockUpstream.requests.clear()
        status, result = ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "legacy agent prompt",
                "chat_prompt": "Reply briefly: hola",
                "user_message": "hola",
                "execution_mode": "auto",
                "bot_id": "bot-opencode-chat",
                "idempotency_key": "private-opencode-chat",
            },
            headers=opencode_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["execution_path"], "direct_chat")
        opencode_request = next(
            request
            for request in MockUpstream.requests
            if request[1] == "/v1/chat/completions"
        )
        self.assertEqual(
            opencode_request[2]["Authorization"],
            "Bearer sk-opencode-private",
        )
        self.assertEqual(
            json.loads(opencode_request[3])["thinking"],
            {"type": "disabled"},
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
                              {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "user_id": "client-controlled"},
                              headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "hola")
        upstream_payload = json.loads(MockUpstream.requests[-1][3])
        self.assertRegex(upstream_payload["user_id"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(upstream_payload["user_id"], "client-controlled")
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

    def test_free_agent_run_cannot_allocate_a_computer(self):
        signup = self.new_user(tier="free")
        status, body = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "abre una computadora",
                "computer": True,
                "bot_id": "bot_free",
                "connector_ids": [],
                "max_credits": 1,
                "idempotency_key": "free-computer-denied",
            },
            headers={"Authorization": f"Bearer {signup['api_key']}"},
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["error"]["type"], "computer_upgrade_required")
        self.assertIsNone(self.ws.backend.store.get_bot_computer(signup["user_id"], "bot_free"))

    def test_invalid_content_length_closes_connection_without_processing_followup(self):
        host, port = self.ws.httpd.server_address
        with socket.create_connection((host, port), timeout=5) as connection:
            connection.sendall(
                b"POST /v1/signup HTTP/1.1\r\n"
                + f"Host: {host}:{port}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: invalid\r\n\r\n"
                + b"{}GET /healthz HTTP/1.1\r\n"
                + f"Host: {host}:{port}\r\n\r\n".encode()
            )
            received = bytearray()
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)
        response = bytes(received)
        self.assertIn(b" 400 ", response)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)

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
        with (
            patch.dict(
                os.environ,
                {"RENDER_GIT_COMMIT": "0123456789abcdef0123456789abcdef01234567"},
            ),
            patch.object(
                self.ws.backend.store,
                "health",
                side_effect=RuntimeError("database unavailable"),
            ),
        ):
            status, body = self.ws.req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["liveness"])
        self.assertEqual(
            body["build_commit"],
            "0123456789abcdef0123456789abcdef01234567",
        )
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
                "computers", "pi", "desktop_relay", "model_provider", "whatsapp",
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

    def test_readiness_does_not_require_chromium_memory_on_render(self):
        self.ws.cfg.environment = "production"
        self.ws.cfg.pi_browser_min_memory_mb = 1024
        with (
            patch("go_backend.server.runtime_memory_limit_mb", return_value=512),
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
        ):
            readiness = self.ws.backend.readiness()

        self.assertTrue(readiness["checks"]["desktop_relay"])
        self.assertEqual(readiness["pi"]["browser_execution"], "authenticated_desktop")

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
        self.assertEqual(migrated.health()["schema_version"], 21)
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

        status, replay = self.ws.req(
            "POST", "/v1/agent/run",
            {"prompt": "prueba end to end", "idempotency_key": "pi-e2e-run"},
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(replay["run_id"], result["run_id"])
        self.assertEqual(replay["answer"], result["answer"])

        status, recovered = self.ws.req(
            "GET", f"/v1/agent/runs/{result['run_id']}", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["result"]["answer"], result["answer"])

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
        self.assertEqual(response_headers["X-Agent-Run-Id"], run_id)
        saved_run = self.ws.backend.store.get_agent_run(run_id)
        timings = json.loads(saved_run["warnings_json"])[0]
        self.assertTrue(timings.startswith("timing:"))
        timing_payload = json.loads(timings.removeprefix("timing:"))
        for name in (
            "auth_complete_ms", "rate_limit_complete_ms",
            "connector_assignment_complete_ms", "pre_reservation_complete_ms",
            "run_reserved_ms", "pi_dispatch_ms", "proxy_received_ms",
            "upstream_request_ms", "upstream_complete_ms", "pi_first_text_ms",
            "upstream_1_request_ms", "upstream_1_complete_ms", "pi_complete_ms",
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

    def test_large_agent_envelope_is_truncated_without_corrupting_json(self):
        original = json.dumps(
            {"text": "a" * 30_000, "widget": None},
            ensure_ascii=False,
        )

        bounded = _bounded_agent_envelope(original)

        self.assertIsNotNone(bounded)
        self.assertLessEqual(len(bounded), 20_000)
        decoded = json.loads(bounded)
        self.assertIsNone(decoded["widget"])
        self.assertTrue(decoded["text"].endswith("[Respuesta truncada por límite de sincronización]"))

    def test_agent_envelope_removes_explicit_deliberation_and_duplicate_sections(self):
        original = json.dumps({
            "text": (
                "The email search returned no actual results.\n\n"
                "For calendar, every event is in the past.\n\n"
                "Let me report accordingly.\n\n"
                "Calendario: no hay próximos eventos.\n\n"
                "Correo: sin_resultados."
                "Calendario: sin_resultados.\n\nCorreo: sin_resultados."
            ),
            "widget": None,
        })

        bounded = _bounded_agent_envelope(original)

        self.assertEqual(
            json.loads(bounded)["text"],
            "Calendario: sin_resultados.\n\nCorreo: sin_resultados.",
        )

    def test_agent_envelope_removes_honest_report_deliberation_boundary(self):
        original = json.dumps({
            "text": (
                "The connector omitted its result list.\n\n"
                "Let me be honest about what I can determine.\n\n"
                "Let me report honestly what I found.\n\n"
                "Correos revisados: sin_resultados\n\nEventos próximos: 0"
            ),
            "widget": None,
        })

        bounded = _bounded_agent_envelope(original)

        self.assertEqual(
            json.loads(bounded)["text"],
            "Correos revisados: sin_resultados\n\nEventos próximos: 0",
        )

    def test_agent_envelope_uses_explicit_final_sentinel_for_arbitrary_deliberation(self):
        original = json.dumps({
            "text": (
                "I need to inspect several tool results and reconcile them.\n\n"
                "Given the requested format I should avoid guessing.\n\n"
                "FINAL: Correos revisados: no confirmado\nEventos próximos: 0"
            ),
            "widget": None,
        })

        bounded = _bounded_agent_envelope(original)

        self.assertEqual(
            json.loads(bounded)["text"],
            "Correos revisados: no confirmado\nEventos próximos: 0",
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
        self.assertIn("fake-pi sesion 1", results[0]["answer"])
        self.assertIn("fake-pi sesion 2", results[1]["answer"])
        session = next(iter(self.ws.backend.pi._sessions.values()))
        command = self.ws.backend.pi._command(False, session_id=session.session_id)
        self.assertIn("--session-id", command)
        self.assertIn("--no-session", command)
        self.assertIn(str(RUNTIME_AUTH_EXTENSION.resolve()), command)
        credentials = json.loads(session.auth_file.read_text(encoding="utf-8"))
        self.assertEqual(credentials, {
            "run_api_key": "",
            "connector_run_token": "",
            "connector_ids": [],
            "computer_enabled": False,
        })
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
        self.assertFalse(warmed["warming"])
        deadline = time.monotonic() + 2
        session = None
        while time.monotonic() < deadline:
            session = next(iter(self.ws.backend.pi._sessions.values()), None)
            if session is not None and session.process is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(session)
        self.assertIsNotNone(session.process)
        process_id = session.process.pid
        self.assertEqual(
            json.loads(session.auth_file.read_text(encoding="utf-8")),
            {
                "run_api_key": "",
                "connector_run_token": "",
                "connector_ids": [],
                "computer_enabled": False,
            },
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

    def test_auto_execution_uses_direct_chat_for_ordinary_conversation(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "legacy full agent prompt",
                "chat_prompt": "Reply briefly to the user: hola",
                "user_message": "hola",
                "execution_mode": "auto",
                "bot_id": "bot-chat-rapido",
                "idempotency_key": "direct-chat-run",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result["answer"]), {"text": "hola", "widget": None})
        self.assertEqual(result["execution_path"], "direct_chat")
        self.assertEqual(result["connector_ids"], [])
        self.assertEqual(self.ws.backend.pi._sessions, {})
        self.assertIn("direct_dispatch_ms", result["timings"])
        upstream = [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(len(upstream), 1)
        sent = json.loads(upstream[0][3])
        self.assertEqual(sent["messages"][-1]["content"], "Reply briefly to the user: hola")
        self.assertIn("una a tres frases", sent["messages"][0]["content"])
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(sent["max_tokens"], 1024)

    def test_compact_routing_context_skips_connector_provider_for_chat(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        bot_id = self.assign_bot_connectors(signup, ["google-workspace"])

        def unexpected_connector_lookup(_user_id):
            raise AssertionError("ordinary chat must not query Composio")

        self.ws.backend.connector_gateway.connected_connector_ids = unexpected_connector_lookup
        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "legacy full agent prompt",
                "chat_prompt": "Responde brevemente: de nada",
                "routing_context": "Usuario: gracias\nAgente: de nada",
                "user_message": "perfecto",
                "execution_mode": "auto",
                "bot_id": bot_id,
                "connector_ids": ["google-workspace"],
                "idempotency_key": "direct-chat-no-composio",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["execution_path"], "direct_chat")
        self.assertEqual(result["connector_ids"], [])

    def test_direct_chat_streams_first_visible_model_delta(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}

        status, body = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "legacy full agent prompt",
                "chat_prompt": "Reply directly: hola",
                "user_message": "hello",
                "execution_mode": "auto",
                "stream": True,
                "bot_id": "bot-chat-stream",
                "idempotency_key": "direct-chat-stream",
            },
            headers=headers,
            raw=True,
        )

        self.assertEqual(status, 200)
        frames = [frame for frame in body.decode("utf-8").split("\n\n") if frame]
        self.assertTrue(any(frame.startswith("event: delta\n") for frame in frames))
        self.assertTrue(any(frame.startswith("event: done64\n") for frame in frames))
        run_id = next(
            json.loads(frame.splitlines()[1].removeprefix("data: "))["run_id"]
            for frame in frames
            if frame.startswith("event: start\n")
        )
        saved = self.ws.backend.store.get_agent_run(run_id)
        timing = json.loads(json.loads(saved["warnings_json"])[0].removeprefix("timing:"))
        self.assertIn("first_visible_delta_ms", timing)
        self.assertLessEqual(timing["upstream_first_content_ms"], timing["first_visible_delta_ms"])

    def test_direct_chat_recovers_from_empty_stream_with_json_retry(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "legacy full agent prompt",
                "chat_prompt": "__empty_stream_retry__",
                "user_message": "hola",
                "execution_mode": "auto",
                "bot_id": "bot-empty-stream",
                "idempotency_key": "direct-chat-empty-stream",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(result["answer"]),
            {"text": "respuesta recuperada", "widget": None},
        )
        upstream = [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(len(upstream), 2)
        self.assertTrue(json.loads(upstream[0][3])["stream"])
        self.assertFalse(json.loads(upstream[1][3])["stream"])

    def test_direct_chat_repairs_partial_structured_envelope(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        structured_prompt = (
            '__partial_structured__ Devuelve exclusivamente JSON válido con esta forma: '
            '{"text":"respuesta visible","widget":null}. '
            'Cuando haga falta una elección, incluye "widget" con prompt y options.'
        )

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "legacy full agent prompt",
                "chat_prompt": structured_prompt,
                "user_message": "hola",
                "execution_mode": "auto",
                "bot_id": "bot-partial-structured",
                "idempotency_key": "direct-chat-partial-structured",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        envelope = json.loads(result["answer"])
        self.assertEqual(envelope["text"], "Saludo reparado")
        self.assertEqual(envelope["widget"]["prompt"], "¿Qué deseas hacer?")
        upstream = [r for r in MockUpstream.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(len(upstream), 2)
        self.assertTrue(json.loads(upstream[0][3])["stream"])
        self.assertFalse(json.loads(upstream[1][3])["stream"])

    def test_auto_execution_keeps_tool_requests_on_pi(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "Use the configured tools and review GitHub.",
                "chat_prompt": "Do not use this direct prompt.",
                "user_message": "revisa mis issues de GitHub",
                "execution_mode": "auto",
                "bot_id": "bot-con-herramientas",
                "idempotency_key": "pi-tool-run",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["execution_path"], "pi")
        self.assertIn("fake-pi", result["answer"])
        self.assertEqual(len(self.ws.backend.pi._sessions), 1)

    def test_auto_execution_keeps_tool_followups_on_pi_from_recent_context(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        bot_id = self.assign_bot_connectors(
            signup,
            ["google-workspace"],
            messages=[{
                "id": str(uuid.uuid4()),
                "role": "user",
                "text": "Crea un evento en mi calendario el 20 de agosto a las 7 am",
                "createdAt": "2026-08-17T12:00:00Z",
            }],
        )
        self.ws.backend.connector_gateway.connected_connector_ids = lambda _user_id: ("google-workspace",)

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "Continúa la acción pendiente con Google Calendar.",
                "chat_prompt": (
                    "Conversación reciente:\nAgente: ¿Hasta qué hora será el evento del calendario?\n"
                    "Usuario: No necesitas ponerle hasta qué hora será"
                ),
                "routing_context": (
                    "Agente: ¿Hasta qué hora será el evento del calendario?\n"
                    "Usuario: No necesitas ponerle hasta qué hora será"
                ),
                "user_message": "No necesitas ponerle hasta qué hora será",
                "execution_mode": "auto",
                "bot_id": bot_id,
                "connector_ids": ["google-workspace"],
                "idempotency_key": "pi-calendar-followup",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["execution_path"], "pi")

    def test_auto_execution_routes_natural_spanish_email_requests_to_pi(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()

        for index, message in enumerate((
            "¿Puedes checar mis correos?",
            "Muéstrame qué llegó a mi bandeja",
            "Dime cuáles son mis emails recientes",
        )):
            status, result = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": f"Usa las herramientas configuradas. Usuario: {message}",
                    "chat_prompt": "No uses esta ruta directa.",
                    "user_message": message,
                    "execution_mode": "auto",
                    "bot_id": "bot-correo-natural",
                    "idempotency_key": f"pi-natural-email-{index}",
                },
                headers=headers,
            )

            self.assertEqual(status, 200)
            self.assertEqual(result["execution_path"], "pi")

    def test_google_connector_catalog_contains_spanish_search_terms(self):
        keywords = CONNECTOR_CATALOG["google-workspace"]["keywords"]
        self.assertIn("correo", keywords)
        self.assertIn("correos", keywords)
        self.assertIn("calendario", keywords)

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
        self.assertEqual(model["thinkingLevelMap"]["off"], "off")
        self.assertNotIn("extraBody", model)
        command = self.ws.backend.pi._command(False, thinking_level="off")
        self.assertEqual(command[command.index("--thinking") + 1], "off")

    def test_single_connector_run_uses_fast_thinking_without_weakening_complex_runs(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        bot_id = self.assign_bot_connectors(signup, ["google-workspace"])
        self.ws.backend.connector_gateway.connected_connector_ids = (
            lambda _user_id: ("google-workspace",)
        )
        captured: list[str | None] = []
        original_run = self.ws.backend.pi.run

        def capture_thinking(**kwargs):
            captured.append(kwargs.get("thinking_level"))
            return original_run(**kwargs)

        self.ws.backend.pi.run = capture_thinking
        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "Busca mis correos recientes",
                "user_message": "Busca mis correos recientes",
                "execution_mode": "agent",
                "bot_id": bot_id,
                "connector_ids": ["google-workspace"],
                "idempotency_key": "single-connector-fast-thinking",
            },
            headers=headers,
        )

        self.assertEqual(status, 200, result)
        self.assertEqual(captured, ["off"])

    def test_connector_broker_scopes_catalog_and_execution_to_run_grant(self):
        signup = self.new_user()
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        adapter = FakeGitHubAdapter(user["id"])
        self.ws.backend.connectors.register_adapter("github", adapter)
        prepared = self.ws.backend.store.create_unmetered_agent_run(
            user_id=user["id"],
            idempotency_key="connector-scope-run",
            model="deepseek-chat",
            browser=False,
            max_credit_milli=1_000,
            max_concurrent_runs=4,
            token_hash="connector-scope-token",
            token_expires_at=time.time() + 600,
        )
        run_id = prepared["run"]["id"]
        bot_id = str(uuid.uuid4())
        token = self.ws.backend.connectors.issue(
            user_id=user["id"],
            run_id=run_id,
            bot_id=bot_id,
            connector_ids=("github", "google-workspace"),
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
            {
                "connector_id": "github",
                "operation": "create_issue",
                "arguments": {"title": "must require approval"},
            },
            headers=internal_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "operation_approval_required")

        approvals_before = self.ws.backend.store.pending_approvals_for_run(
            user["id"], run_id
        )
        self.ws.backend.connectors.register_adapter(
            "google-workspace", FakeContractAdapter(user["id"])
        )
        status, body = self.ws.req(
            "POST",
            "/v1/internal/connectors/execute",
            {
                "connector_id": "google-workspace",
                "operation": "send_email",
                "arguments": {"__invalid__": True},
            },
            headers=internal_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "bad_connector_arguments")
        self.assertIn("send_email", body["error"]["message"])
        self.assertEqual(
            self.ws.backend.store.pending_approvals_for_run(user["id"], run_id),
            approvals_before,
        )

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

    def test_structured_approval_is_exact_one_shot_and_argument_bound(self):
        signup = self.new_user()
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        adapter = FakeGitHubAdapter(user["id"])
        arguments = {
            "summary": "Inicio de trabajo",
            "start_datetime": "2026-08-20T07:00:00",
            "timezone": "America/Denver",
        }
        prepared = self.ws.backend.store.create_unmetered_agent_run(
            user_id=user["id"],
            idempotency_key="structured-approval-run",
            model="deepseek-chat",
            browser=False,
            max_credit_milli=1_000,
            max_concurrent_runs=4,
            token_hash="structured-approval-token",
            token_expires_at=time.time() + 600,
        )
        run_id = prepared["run"]["id"]
        approval = self.ws.backend.store.create_pending_approval(
            user_id=user["id"], bot_id=str(uuid.uuid4()), run_id=run_id,
            target_type="connector", connector_id="google-workspace",
            operation="create_calendar_event", arguments=arguments,
            arguments_hash=canonical_arguments_hash(arguments),
            human_summary="Crear evento Inicio de trabajo",
        )
        approved = self.ws.backend.store.approve_pending_approval(
            user_id=user["id"], bot_id=approval["bot_id"], approval_id=approval["id"],
        )
        broker = ConnectorBroker(operation_store=self.ws.backend.store)
        broker.register_adapter("google-workspace", adapter)
        token = broker.issue(
            user_id=user["id"], run_id=run_id, connector_ids=("google-workspace",),
            bot_id=approval["bot_id"], approved_action=approved,
        )
        with self.assertRaises(ConnectorBrokerError) as mismatch:
            broker.execute(
                token=token, connector_id="google-workspace",
                operation="create_calendar_event", arguments={**arguments, "summary": "Otro"},
                approval_id=approval["id"], action_id=approval["action_id"],
            )
        self.assertEqual(mismatch.exception.code, "approval_arguments_mismatch")
        result = broker.execute(
            token=token, connector_id="google-workspace",
            operation="create_calendar_event", arguments=arguments,
            operation_id=approval["action_id"], approval_id=approval["id"],
            action_id=approval["action_id"],
        )
        self.assertEqual(result["operation"], "create_calendar_event")
        replay = broker.execute(
            token=token, connector_id="google-workspace",
            operation="create_calendar_event", arguments=arguments,
            operation_id=approval["action_id"], approval_id=approval["id"],
            action_id=approval["action_id"],
        )
        self.assertEqual(replay, result)
        self.assertEqual(len(adapter.calls), 1)

    def test_every_catalog_operation_obeys_read_or_exact_write_contract(self):
        """Synthetic end-to-end matrix for the whole connector marketplace."""
        signup = self.new_user()
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        prepared = self.ws.backend.store.create_unmetered_agent_run(
            user_id=user["id"],
            idempotency_key="all-connector-interactions",
            model="deepseek-chat",
            browser=False,
            max_credit_milli=1_000,
            max_concurrent_runs=4,
            token_hash="all-connector-interactions-token",
            token_expires_at=time.time() + 600,
        )
        run_id = prepared["run"]["id"]
        bot_id = str(uuid.uuid4())
        adapter = FakeContractAdapter(user["id"])
        connector_ids = tuple(CONNECTOR_CATALOG)
        for connector_id in connector_ids:
            self.ws.backend.connectors.register_adapter(connector_id, adapter)
        token = self.ws.backend.connectors.issue(
            user_id=user["id"],
            run_id=run_id,
            bot_id=bot_id,
            connector_ids=connector_ids,
        )
        headers = {"X-Connector-Run-Token": token}
        read_count = 0
        write_count = 0

        for connector_id, item in CONNECTOR_CATALOG.items():
            for operation in item["operations"]:
                case_id = f"{connector_id}:{operation}"
                arguments = {"contract_case": case_id}
                if _connector_operation_is_read_only(operation):
                    status, body = self.ws.req(
                        "POST",
                        "/v1/internal/connectors/execute",
                        {
                            "connector_id": connector_id,
                            "operation": operation,
                            "arguments": arguments,
                            "operation_id": f"read:{case_id}",
                        },
                        headers=headers,
                    )
                    self.assertEqual((status, body.get("operation")), (200, operation), case_id)
                    read_count += 1
                    continue

                # Model/provider aliases may be canonicalized by an adapter.
                # The approved retry deliberately reuses the original shape.
                arguments = {"provider_alias": case_id}

                approvals_before = len(
                    self.ws.backend.store.pending_approvals_for_run(user["id"], run_id)
                )
                status, body = self.ws.req(
                    "POST",
                    "/v1/internal/connectors/execute",
                    {
                        "connector_id": connector_id,
                        "operation": operation,
                        "arguments": {"__invalid__": True, "contract_case": case_id},
                    },
                    headers=headers,
                )
                self.assertEqual(status, 400, case_id)
                self.assertEqual(body["error"]["type"], "bad_connector_arguments", case_id)
                self.assertEqual(
                    len(self.ws.backend.store.pending_approvals_for_run(user["id"], run_id)),
                    approvals_before,
                    case_id,
                )

                status, body = self.ws.req(
                    "POST",
                    "/v1/internal/connectors/execute",
                    {
                        "connector_id": connector_id,
                        "operation": operation,
                        "arguments": arguments,
                    },
                    headers=headers,
                )
                self.assertEqual(status, 409, case_id)
                self.assertEqual(
                    body["error"]["type"], "operation_approval_required", case_id
                )
                approval = self.ws.backend.store.approve_pending_approval(
                    user_id=user["id"],
                    bot_id=bot_id,
                    approval_id=body["approval"]["approval_id"],
                )
                self.assertIsNotNone(approval, case_id)
                approved_token = self.ws.backend.connectors.issue(
                    user_id=user["id"],
                    run_id=run_id,
                    bot_id=bot_id,
                    connector_ids=(connector_id,),
                    approved_action=approval,
                )
                approved_headers = {"X-Connector-Run-Token": approved_token}
                call_count = len(adapter.calls)
                payload = {
                    "connector_id": connector_id,
                    "operation": operation,
                    "arguments": arguments,
                    "operation_id": body["approval"]["action_id"],
                }
                status, executed = self.ws.req(
                    "POST", "/v1/internal/connectors/execute", payload,
                    headers=approved_headers,
                )
                self.assertEqual((status, executed.get("operation")), (200, operation), case_id)
                status, replayed = self.ws.req(
                    "POST", "/v1/internal/connectors/execute", payload,
                    headers=approved_headers,
                )
                self.assertEqual((status, replayed), (200, executed), case_id)
                self.assertEqual(len(adapter.calls), call_count + 1, case_id)
                write_count += 1

        self.assertEqual(read_count + write_count, 222)
        self.assertGreater(read_count, write_count)
        self.assertEqual(len(adapter.validations), write_count * 2)

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

    def test_connector_operation_replays_durably_after_broker_restart(self):
        signup = self.new_user()
        user_id = signup["user_id"]
        prepared = self.ws.backend.store.create_unmetered_agent_run(
            user_id=user_id,
            idempotency_key="durable-connector-operation-run",
            model="deepseek-chat",
            browser=False,
            max_credit_milli=1_000,
            max_concurrent_runs=4,
            token_hash="test-token-hash",
            token_expires_at=time.time() + 600,
        )
        run_id = prepared["run"]["id"]
        adapter = FakeGitHubAdapter(user_id)

        first = ConnectorBroker(operation_store=self.ws.backend.store)
        first.register_adapter("github", adapter)
        first_token = first.issue(
            user_id=user_id,
            run_id=run_id,
            connector_ids=("github",),
        )
        initial = first.execute(
            token=first_token,
            connector_id="github",
            operation="search_repositories",
            arguments={"query": "wrapper"},
            operation_id="tool-call-1",
        )

        restarted = ConnectorBroker(operation_store=self.ws.backend.store)
        restarted.register_adapter("github", adapter)
        restarted_token = restarted.issue(
            user_id=user_id,
            run_id=run_id,
            connector_ids=("github",),
        )
        replay = restarted.execute(
            token=restarted_token,
            connector_id="github",
            operation="search_repositories",
            arguments={"query": "wrapper"},
            operation_id="tool-call-1",
        )
        self.assertEqual(replay, initial)
        self.assertEqual(len(adapter.calls), 1)

        with self.assertRaises(ConnectorBrokerError) as conflict:
            restarted.execute(
                token=restarted_token,
                connector_id="github",
                operation="search_repositories",
                arguments={"query": "different"},
                operation_id="tool-call-1",
            )
        self.assertEqual(conflict.exception.code, "connector_operation_uncertain")

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
            [("GITHUB_SEARCH_REPOSITORIES", {"query": "wrapper"})],
        )
        self.assertEqual(client.session_options[-1]["user_id"], alice_id)
        self.assertEqual(client.session_options[-1]["toolkits"], ["github"])
        self.assertFalse(client.session_options[-1]["manage_connections"])
        self.assertEqual(client.session_options[-1]["workbench"], {"enable": False})

        client.tool_schemas = {
            "GITHUB_SEARCH_REPOSITORIES": {
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                }
            }
        }
        with self.assertRaises(ConnectorBrokerError) as invalid_arguments:
            adapter.execute(alice_id, "search_repositories", {"unexpected": True})
        self.assertEqual(invalid_arguments.exception.status, 400)
        self.assertEqual(invalid_arguments.exception.code, "bad_connector_arguments")

        gateway.disconnect(alice_id, "github")
        self.assertFalse(gateway.status(alice_id, "github")["connected"])

    def test_google_workspace_uses_current_composio_toolkit_slug(self):
        client = FakeComposioClient()
        user_id = self.new_user("google-workspace-user")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        started = gateway.start(user_id, "google-workspace")

        self.assertEqual(client.session_options[-1]["toolkits"], ["googlesuper"])
        self.assertEqual(
            client.connected_accounts.items["ca_1"].toolkit,
            "googlesuper",
        )
        self.assertEqual(
            started["authorize_url"],
            "https://connect.composio.dev/link/ca_1",
        )

        client.connected_accounts.items["ca_nested"] = SimpleNamespace(
            id="ca_nested",
            user_id=user_id,
            toolkit={"slug": "Google_Super"},
            status="ACTIVE",
            alias="alan@example.com",
            data={},
            status_reason="",
        )
        snapshot = gateway.snapshot(user_id)
        google = next(
            item for item in snapshot if item["connector_id"] == "google-workspace"
        )
        self.assertTrue(google["connected"])
        self.assertEqual(google["account"], "alan@example.com")

    def test_google_calendar_create_uses_pinned_tool_and_normalized_schema(self):
        client = FakeComposioClient()
        user_id = self.new_user("google-calendar-create")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        result = gateway.execute(
            user_id,
            "google-workspace",
            "create_calendar_event",
            {
                "title": "Demo de Agent Genia",
                "startTime": "2026-08-18T15:00:00",
                "timeZone": "America/Denver",
                "duration_minutes": 90,
            },
        )

        self.assertEqual(result["items"][0]["name"], "wrapper-backend")
        self.assertEqual(client.searches, [(
            "googlesuper",
            "Use Google Workspace to perform the operation 'create_calendar_event'.",
        )])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_CREATE_EVENT",
            {
                "summary": "Demo de Agent Genia",
                "start_datetime": "2026-08-18T15:00:00",
                "timezone": "America/Denver",
                "event_duration_hour": 1,
                "event_duration_minutes": 30,
                "calendar_id": "primary",
            },
        )])

    def test_google_workspace_send_email_uses_pinned_tool_and_normalized_schema(
        self,
    ):
        client = FakeComposioClient()
        user_id = self.new_user("google-email-send")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        gateway.execute(
            user_id,
            "google-workspace",
            "send_email",
            {
                "to": "cliente@example.com",
                "title": "Costo promedio por tarea",
                "message": "¿Cuál es el costo promedio por tarea?",
            },
        )

        self.assertEqual(client.searches, [(
            "googlesuper",
            "Use Google Workspace to perform the operation 'send_email'.",
        )])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_SEND_EMAIL",
            {
                "recipient_email": "cliente@example.com",
                "subject": "Costo promedio por tarea",
                "body": "¿Cuál es el costo promedio por tarea?",
            },
        )])

    def test_provider_schema_rejects_any_plugin_write_before_approval(self):
        client = FakeComposioClient()
        user_id = self.new_user("provider-schema-preflight")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        client.tool_schemas = {
            "SLACK_POST_MESSAGE": {
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel_id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "required": ["channel_id", "text"],
                    "additionalProperties": False,
                }
            }
        }

        with self.assertRaises(ConnectorBrokerError) as incomplete:
            gateway.validate_arguments(
                user_id, "slack", "post_message", {"channel_id": "C123"}
            )
        self.assertEqual(incomplete.exception.code, "bad_connector_arguments")
        self.assertEqual(client.executions, [])
        self.assertEqual(
            gateway.validate_arguments(
                user_id,
                "slack",
                "post_message",
                {"channel_id": "C123", "text": "Hola"},
            ),
            {"channel_id": "C123", "text": "Hola"},
        )
        self.assertEqual(client.executions, [])

    def test_google_workspace_catalog_keeps_every_verified_pinned_operation(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        operations = tuple(CONNECTOR_CATALOG["google-workspace"]["operations"])
        self.assertEqual(
            gateway.resolvable_operations(
                self.new_user("google-pinned-catalog")["user_id"],
                "google-workspace",
                operations,
            ),
            operations,
        )
        self.assertEqual(client.searches, [])

    def test_google_email_search_uses_verified_fetch_tool(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        gateway.execute(
            self.new_user("google-live-search-routing")["user_id"],
            "google-workspace",
            "search_email",
            {"query": "CDL OR \"commercial driver's license\"", "max_results": 10},
        )
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_FETCH_EMAILS",
            {
                "query": "CDL OR \"commercial driver's license\"",
                "max_results": 10,
                "include_payload": False,
                "verbose": False,
            },
        )])
        self.assertEqual(client.searches, [])

    def test_google_email_search_is_bounded_and_normalizes_aliases(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        gateway.execute(
            self.new_user("google-bounded-email-search")["user_id"],
            "google-workspace",
            "search_email",
            {"q": "CDL", "limit": 100},
        )
        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_FETCH_EMAILS",
            {
                "query": "CDL",
                "max_results": 10,
                "include_payload": False,
                "verbose": False,
            },
        )])

    def test_google_email_content_search_decodes_body_without_mime_noise(self):
        client = FakeComposioClient()
        session = FakeComposioSession(client, "google-content", "googlesuper")
        encoded_body = base64.urlsafe_b64encode(
            b"Flight 4521 departs DEN at 10:15 and arrives BUR at 11:42."
        ).decode("ascii").rstrip("=")

        def execute(slug, *, arguments):
            client.executions.append((slug, arguments))
            return SimpleNamespace(
                data={
                    "messages": [{
                        "id": "msg_flight",
                        "threadId": "thread_flight",
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Flight receipt"},
                                {"name": "From", "value": "airline@example.com"},
                            ],
                            "mimeType": "multipart/alternative",
                            "parts": [{
                                "mimeType": "text/plain",
                                "body": {"data": encoded_body},
                            }, {
                                "filename": "ticket.pdf",
                                "mimeType": "application/pdf",
                                "body": {"data": "private-attachment"},
                            }],
                        },
                    }],
                },
                error=None,
            )

        session.execute = execute
        client.sessions.create = lambda **_options: session
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        result = gateway.execute(
            self.new_user("google-content-search")["user_id"],
            "google-workspace",
            "search_email",
            {
                "query": 'subject:(flight OR itinerary)',
                "max_results": 20,
                "include_content": True,
            },
        )

        self.assertEqual(client.executions, [(
            "GOOGLESUPER_FETCH_EMAILS",
            {
                "query": 'subject:(flight OR itinerary)',
                "max_results": 3,
                "include_payload": True,
                "verbose": True,
            },
        )])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("Flight 4521 departs DEN", serialized)
        self.assertIn("Flight receipt", serialized)
        self.assertIn("msg_flight", serialized)
        self.assertNotIn("private-attachment", serialized)
        self.assertNotIn(encoded_body, serialized)
        self.assertNotIn('"payload"', serialized)

    def test_google_calendar_search_uses_find_event_with_exact_filters(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        gateway.execute(
            self.new_user("google-calendar-find-routing")["user_id"],
            "google-workspace",
            "list_calendar_events",
            {
                "title": "AgentGenia E2E",
                "timeMin": "2026-08-25T00:00:00-06:00",
                "timeMax": "2026-08-26T00:00:00-06:00",
                "calendarId": "primary",
                "limit": 500,
                "timezone": "America/Denver",
            },
        )

        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_FIND_EVENT",
            {
                "calendar_id": "primary",
                "max_results": 50,
                "single_events": True,
                "query": "AgentGenia E2E",
                "time_min": "2026-08-25T00:00:00-06:00",
                "time_max": "2026-08-26T00:00:00-06:00",
            },
        )])

    def test_connector_provider_false_success_is_rejected(self):
        client = FakeComposioClient()
        session = FakeComposioSession(client, "provider-false", "googlesuper")
        session.execute = lambda _slug, *, arguments: SimpleNamespace(
            data={"successful": False, "error": "mutation rejected"},
            error=None,
        )
        client.sessions.create = lambda **_options: session
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        with self.assertRaises(ConnectorBrokerError) as rejected:
            gateway.execute(
                self.new_user("provider-false-success")["user_id"],
                "google-workspace",
                "create_calendar_event",
                {
                    "summary": "No debe confirmarse",
                    "start_datetime": "2026-08-25T14:00:00-06:00",
                    "timezone": "America/Denver",
                },
            )
        self.assertEqual(rejected.exception.code, "connector_upstream_error")

    def test_connected_plugin_reads_use_verified_tools_without_dynamic_search(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        user_id = self.new_user("verified-connected-plugin-reads")["user_id"]

        gateway.execute(user_id, "notion", "search", {"query": "ads"})
        gateway.execute(
            user_id,
            "microsoft-365",
            "list_calendar_events",
            {"limit": 100, "timezone": "America/Denver"},
        )
        gateway.execute(
            user_id,
            "canva",
            "search_designs",
            {"search": "Agent Genia", "max_results": 50},
        )
        gateway.execute(
            user_id,
            "microsoft-365",
            "search_email",
            {"search_query": "Microsoft", "max_results": 50},
        )
        gateway.execute(user_id, "microsoft-365", "read_email", {"id": "message_1"})
        gateway.execute(user_id, "canva", "get_design", {"design_id": "design_1"})

        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [
            ("NOTION_SEARCH_NOTION_PAGE", {"query": "ads"}),
            ("OUTLOOK_LIST_EVENTS", {"top": 10, "timezone": "America/Denver"}),
            ("CANVA_LIST_USER_DESIGNS", {"query": "Agent Genia"}),
            ("OUTLOOK_SEARCH_MESSAGES", {"query": "Microsoft", "size": 10}),
            ("OUTLOOK_GET_MESSAGE", {"message_id": "message_1"}),
            ("CANVA_FETCH_DESIGN_METADATA_AND_ACCESS_INFORMATION", {"designId": "design_1"}),
        ])

    def test_outlook_search_falls_back_when_tenant_rejects_search_endpoint(self):
        client = FakeComposioClient()
        session = FakeComposioSession(client, "outlook-fallback", "outlook")

        def execute(slug, *, arguments):
            client.executions.append((slug, arguments))
            if slug == "OUTLOOK_SEARCH_MESSAGES":
                return SimpleNamespace(data=None, error="Search endpoint unavailable")
            return SimpleNamespace(
                data={"items": [{"id": "message_1", "subject": "Microsoft"}]},
                error=None,
            )

        session.execute = execute
        client.sessions.create = lambda **_options: session
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        result = gateway.execute(
            self.new_user("outlook-search-fallback")["user_id"],
            "microsoft-365",
            "search_email",
            {"query": "Microsoft", "size": 100},
        )

        self.assertEqual(client.executions, [
            ("OUTLOOK_SEARCH_MESSAGES", {"query": "Microsoft", "size": 10}),
            ("OUTLOOK_LIST_MESSAGES", {"subject_contains": "Microsoft", "top": 10}),
        ])
        self.assertIn("message_1", json.dumps(result))

    def test_connected_plugin_collections_are_compact_but_keep_ids_and_titles(self):
        private_body = "contenido privado " * 4_000
        cases = (
            (
                "notion", "search",
                {"results": [{"id": "page_1", "title": "Plan", "properties": private_body}]},
                ("page_1", "Plan"),
            ),
            (
                "microsoft-365", "list_calendar_events",
                {"items": [{"id": "event_1", "subject": "Reunión", "body": private_body}]},
                ("event_1", "Reunión"),
            ),
            (
                "canva", "search_designs",
                {"items": [{"id": "design_1", "title": "Campaña", "content": private_body}]},
                ("design_1", "Campaña"),
            ),
        )
        for connector_id, operation, payload, expected in cases:
            with self.subTest(connector_id=connector_id):
                compacted = json.dumps(
                    _compact_connector_result(connector_id, operation, payload),
                    ensure_ascii=False,
                )
                for value in expected:
                    self.assertIn(value, compacted)
                self.assertNotIn("contenido privado", compacted)
                self.assertLess(len(compacted), 1_000)

    def test_google_collection_results_drop_wire_noise_but_keep_actionable_ids(self):
        huge_body = "contenido privado " * 4_000
        email = _compact_connector_result(
            "google-workspace",
            "search_email",
            {
                "messages": [{
                    "id": "msg_123",
                    "threadId": "thread_456",
                    "snippet": "Oferta de entrenamiento CDL",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Entrenamiento CDL pagado"},
                            {"name": "From", "value": "Recruiting <jobs@example.com>"},
                        ],
                        "body": {"data": huge_body},
                        "parts": [{"body": {"data": huge_body}}],
                    },
                    "raw": huge_body,
                }],
                "nextPageToken": "page_2",
            },
        )
        serialized_email = json.dumps(email, ensure_ascii=False)
        self.assertIn("msg_123", serialized_email)
        self.assertIn("thread_456", serialized_email)
        self.assertIn("Entrenamiento CDL pagado", serialized_email)
        self.assertIn("jobs@example.com", serialized_email)
        self.assertIn("page_2", serialized_email)
        self.assertNotIn("contenido privado", serialized_email)
        self.assertLess(len(serialized_email), 2_000)

        calendar = _compact_connector_result(
            "google-workspace",
            "list_calendar_events",
            {"items": [{
                "id": "evt_123",
                "summary": "Reunión de ventas",
                "start": {"dateTime": "2026-08-25T14:00:00-06:00"},
                "end": {"dateTime": "2026-08-25T15:00:00-06:00"},
                "description": "d" * 5_000,
                "conferenceData": {"entryPoints": [{"uri": "https://meet.example"}]},
            }]},
        )
        serialized_calendar = json.dumps(calendar, ensure_ascii=False)
        self.assertIn("evt_123", serialized_calendar)
        self.assertIn("Reunión de ventas", serialized_calendar)
        self.assertIn("2026-08-25T14:00:00-06:00", serialized_calendar)
        self.assertLess(len(serialized_calendar), 2_000)

    def test_google_sheet_read_uses_exact_id_range_without_tool_search(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        gateway.execute(
            self.new_user("google-sheet-read-routing")["user_id"],
            "google-workspace",
            "read_sheet",
            {"file_id": "sheet_123", "a1_range": "Sheet1!A1:C3"},
        )
        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_VALUES_GET",
            {"spreadsheet_id": "sheet_123", "range": "Sheet1!A1:C3"},
        )])

    def test_google_sheet_names_use_exact_id_without_tool_search(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        gateway.execute(
            self.new_user("google-sheet-names-routing")["user_id"],
            "google-workspace",
            "list_sheet_names",
            {"file_id": "sheet_123"},
        )
        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_GET_SHEET_NAMES",
            {"spreadsheet_id": "sheet_123"},
        )])

    def test_google_sheet_update_normalizes_friendly_arguments_for_provider_schema(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        gateway.execute(
            self.new_user("google-sheet-update-routing")["user_id"],
            "google-workspace",
            "update_sheet",
            {
                "file_id": "sheet_123",
                "a1_range": "Sheet1!C100",
                "value": "AgentGenia E2E",
            },
        )
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_VALUES_UPDATE",
            {
                "spreadsheet_id": "sheet_123",
                "range": "Sheet1!C100",
                "value_input_option": "USER_ENTERED",
                "values": [["AgentGenia E2E"]],
            },
        )])

    def test_google_sheet_update_matches_provider_schema_before_approval(self):
        client = FakeComposioClient()
        client.tool_schemas = {
            "GOOGLESUPER_VALUES_UPDATE": {
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "minLength": 1},
                        "range": {"type": "string", "minLength": 1},
                        "value_input_option": {"enum": ["RAW", "USER_ENTERED"]},
                        "values": {"type": "array", "minItems": 1},
                    },
                    "required": ["spreadsheet_id", "range", "value_input_option", "values"],
                    "additionalProperties": False,
                }
            }
        }
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        self.assertEqual(
            gateway.validate_arguments(
                self.new_user("google-sheet-update-schema")["user_id"],
                "google-workspace",
                "update_sheet",
                {"spreadsheetId": "sheet_123", "range": "C100", "values": ["ok"]},
            ),
            {
                "spreadsheet_id": "sheet_123",
                "range": "C100",
                "value_input_option": "USER_ENTERED",
                "values": [["ok"]],
            },
        )

    def test_approved_write_reuses_the_schema_resolved_tool_for_execution(self):
        client = FakeComposioClient()
        client.tool_schemas = {
            "GOOGLESUPER_SEND_EMAIL": {
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "recipient_email": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["recipient_email", "subject", "body"],
                    "additionalProperties": False,
                }
            }
        }
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        user_id = self.new_user("google-write-cache")['user_id']
        arguments = {
            "recipient_email": "self@example.com",
            "subject": "Prueba",
            "body": "Mensaje",
        }

        self.assertEqual(
            gateway.validate_arguments(
                user_id, "google-workspace", "send_email", arguments
            ),
            arguments,
        )
        gateway.execute(user_id, "google-workspace", "send_email", arguments)

        self.assertEqual(len(client.searches), 1)
        self.assertEqual(
            client.executions,
            [("GOOGLESUPER_SEND_EMAIL", arguments)],
        )

    def test_plugin_write_fails_closed_when_provider_schema_is_missing(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        with self.assertRaises(ConnectorBrokerError) as missing:
            gateway.validate_arguments(
                self.new_user("missing-provider-schema")["user_id"],
                "slack",
                "post_message",
                {"channel": "C123", "text": "Prueba"},
            )
        self.assertEqual(missing.exception.code, "connector_schema_unavailable")
        self.assertEqual(missing.exception.status, 503)
        self.assertEqual(client.executions, [])

    def test_notion_create_page_uses_static_approval_contract(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        arguments = gateway.validate_arguments(
            self.new_user("notion-create-contract")["user_id"],
            "notion",
            "create_page",
            {"parentPageId": "parent_123", "name": "Auditoría E2E"},
        )
        self.assertEqual(
            arguments,
            {"parent_id": "parent_123", "title": "Auditoría E2E"},
        )
        self.assertEqual(client.searches, [])

    def test_canva_create_design_normalizes_preset_before_approval(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        arguments = gateway.validate_arguments(
            self.new_user("canva-create-contract")["user_id"],
            "canva",
            "create_design",
            {"name": "Auditoría E2E", "designType": "presentation"},
        )
        self.assertEqual(
            arguments,
            {
                "type": "type_and_asset",
                "design_type": {"type": "preset", "name": "presentation"},
                "title": "Auditoría E2E",
            },
        )
        self.assertEqual(client.searches, [])

    def test_outlook_create_event_normalizes_google_style_aliases(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        arguments = gateway.validate_arguments(
            self.new_user("outlook-create-contract")["user_id"],
            "microsoft-365",
            "create_calendar_event",
            {
                "summary": "Auditoría E2E",
                "start": "2026-08-20T09:00:00-06:00",
                "timezone": "America/Denver",
            },
        )
        self.assertEqual(arguments["subject"], "Auditoría E2E")
        self.assertEqual(arguments["body"], "")
        self.assertEqual(arguments["time_zone"], "America/Denver")
        self.assertEqual(arguments["end_datetime"], "2026-08-20T10:00:00-06:00")
        self.assertEqual(client.searches, [])

    def test_calendly_lists_with_authenticated_user_scope(self):
        client = FakeComposioClient()
        client.execution_results["CALENDLY_GET_CURRENT_USER"] = {
            "resource": {
                "uri": "https://api.calendly.com/users/user_123",
                "current_organization": "https://api.calendly.com/organizations/org_123",
            }
        }
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        gateway.execute(
            self.new_user("calendly-scope")["user_id"],
            "calendly",
            "list_scheduled_events",
            {"max_results": 5},
        )
        self.assertEqual(
            client.executions,
            [
                ("CALENDLY_GET_CURRENT_USER", {}),
                (
                    "CALENDLY_LIST_SCHEDULED_EVENTS",
                    {
                        "count": 5,
                        "user": "https://api.calendly.com/users/user_123",
                    },
                ),
            ],
        )

    def test_figma_search_requires_real_resource_locator(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        with self.assertRaises(ConnectorBrokerError) as missing:
            gateway.execute(
                self.new_user("figma-resource-required")["user_id"],
                "figma",
                "search_files",
                {"query": "mi archivo reciente"},
            )
        self.assertEqual(missing.exception.code, "bad_connector_arguments")
        gateway.execute(
            self.new_user("figma-resource-url")["user_id"],
            "figma",
            "search_files",
            {"url": "https://www.figma.com/design/abc123/Auditoria"},
        )
        self.assertEqual(
            client.executions,
            [
                (
                    "FIGMA_DISCOVER_FIGMA_RESOURCES",
                    {"figma_url": "https://www.figma.com/design/abc123/Auditoria"},
                )
            ],
        )

    def test_google_calendar_create_rejects_natural_language_before_upstream(self):
        client = FakeComposioClient()
        user_id = self.new_user("google-calendar-invalid")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        with self.assertRaises(ConnectorBrokerError) as error:
            gateway.execute(
                user_id,
                "google-workspace",
                "create_calendar_event",
                {
                    "start_datetime": "manana a las 3",
                    "timezone": "MST",
                },
            )

        self.assertEqual(error.exception.code, "bad_connector_arguments")
        self.assertIn("ISO 8601", str(error.exception))
        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [])

    def test_google_calendar_delete_uses_pinned_tool_and_exact_event_id(self):
        client = FakeComposioClient()
        user_id = self.new_user("google-calendar-delete")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        gateway.execute(
            user_id,
            "google-workspace",
            "delete_calendar_event",
            {"eventId": "evt_abc123", "calendarId": "primary"},
        )

        self.assertEqual(client.searches, [(
            "googlesuper",
            "Use Google Workspace to perform the operation 'delete_calendar_event'.",
        )])
        self.assertEqual(client.executions, [(
            "GOOGLESUPER_DELETE_EVENT",
            {"event_id": "evt_abc123", "calendar_id": "primary"},
        )])

    def test_google_calendar_delete_requires_exact_event_id(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        with self.assertRaises(ConnectorBrokerError) as error:
            gateway.execute(
                self.new_user("google-calendar-delete-invalid")["user_id"],
                "google-workspace",
                "delete_calendar_event",
                {"event_title": "Comienzo a trabajar"},
            )

        self.assertEqual(error.exception.code, "bad_connector_arguments")
        self.assertEqual(client.searches, [])
        self.assertEqual(client.executions, [])

    def test_github_read_file_uses_pinned_tool_and_normalizes_aliases(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )

        gateway.execute(
            self.new_user("github-read-file")["user_id"],
            "github",
            "read_file",
            {
                "repository_owner": "alangael24",
                "repository_name": "wrapper-backend",
                "file_path": "README.md",
                "branch": "main",
            },
        )

        self.assertEqual(client.executions, [(
            "GITHUB_GET_REPOSITORY_CONTENT",
            {
                "owner": "alangael24",
                "repo": "wrapper-backend",
                "path": "README.md",
                "branch": "main",
            },
        )])

    def test_snowflake_select_query_is_pinned_and_rejects_mutation(self):
        client = FakeComposioClient()
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            auth_configs={"snowflake": "ac_agentgenia_snowflake"},
            store=self.ws.backend.store,
        )
        user_id = self.new_user("snowflake-select")["user_id"]

        gateway.execute(
            user_id,
            "snowflake",
            "select_query",
            {"query": "SELECT id, total FROM orders WHERE total > 100;"},
        )

        self.assertEqual(client.executions, [(
            "SNOWFLAKE_EXECUTE_SQL",
            {"statement": "SELECT id, total FROM orders WHERE total > 100"},
        )])
        prior_searches = list(client.searches)
        with self.assertRaises(ConnectorBrokerError) as unsafe:
            gateway.execute(
                user_id,
                "snowflake",
                "select_query",
                {"statement": "DELETE FROM orders"},
            )
        self.assertEqual(unsafe.exception.code, "unsafe_select_query")
        self.assertEqual(client.searches, prior_searches)

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

    def test_composio_gateway_skips_connect_link_for_private_auth_config(self):
        client = FakeComposioClient()
        user_id = self.new_user("direct-oauth-user")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            auth_configs={"salesforce": "ac_agentgenia_salesforce"},
            direct_auth_configs={"salesforce": "ac_agentgenia_salesforce"},
            store=self.ws.backend.store,
        )

        started = gateway.start(user_id, "salesforce")

        self.assertTrue(started["authorize_url"].startswith("https://login.salesforce.com/"))
        self.assertNotIn("composio", started["authorize_url"])
        self.assertEqual(client.session_options, [])
        self.assertEqual(client.connected_accounts.initiate_calls, [{
            "user_id": user_id,
            "auth_config_id": "ac_agentgenia_salesforce",
            "callback_url": "https://agentgenia-api.onrender.com/connections/complete",
        }])

    def test_composio_gateway_uses_connect_link_for_managed_auth_config(self):
        client = FakeComposioClient()
        user_id = self.new_user("managed-oauth-user")["user_id"]
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            auth_configs={"salesforce": "ac_agentgenia_salesforce"},
            store=self.ws.backend.store,
        )

        started = gateway.start(user_id, "salesforce")

        self.assertTrue(
            started["authorize_url"].startswith("https://connect.composio.dev/")
        )
        self.assertEqual(client.connected_accounts.initiate_calls, [])
        self.assertEqual(client.connected_accounts.link_calls, [{
            "user_id": user_id,
            "auth_config_id": "ac_agentgenia_salesforce",
            "callback_url": "https://agentgenia-api.onrender.com/connections/complete",
        }])

    def test_composio_gateway_rejects_unregistered_direct_auth_config(self):
        with self.assertRaisesRegex(ValueError, "mismo valor"):
            ComposioConnectorGateway(
                client=FakeComposioClient(),
                auth_configs={"salesforce": "ac_agentgenia_salesforce"},
                direct_auth_configs={"salesforce": "ac_other_salesforce"},
                store=self.ws.backend.store,
            )

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

    def test_native_plugin_preflight_rejects_missing_resource_identifiers(self):
        gateway = self.ws.backend.native_connector_gateway
        with self.assertRaises(ConnectorBrokerError) as missing:
            gateway.validate_arguments("salesloft", "update_person", {"name": "Ana"})
        self.assertEqual(missing.exception.code, "bad_connector_arguments")
        self.assertIn("id", str(missing.exception))
        self.assertEqual(
            gateway.validate_arguments(
                "salesloft", "update_person", {"id": "person_123", "name": "Ana"}
            ),
            {"id": "person_123", "name": "Ana"},
        )

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

            with (
                patch(
                    "go_backend.native_connectors.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
                ),
                patch(
                    "go_backend.native_connectors._request_json",
                    return_value={"data": []},
                ) as credential_probe,
            ):
                complete_page = native.submit(
                    started["attempt_id"],
                    {"access_token": "nooks-api-super-secret", "account_label": "Ventas"},
                ).decode()
            credential_probe.assert_called_once()
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
        bot_id = self.assign_bot_connectors(
            signup, ["github", "google-workspace"]
        )
        self.ws.backend.connector_gateway.connected_connector_ids = (
            lambda _user_id: ("github", "google-workspace")
        )
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
                "bot_id": bot_id,
                "connector_ids": ["github", "google-workspace", "github"],
                "idempotency_key": "connector-run",
            },
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["connector_ids"], ["github", "google-workspace"])
        # Warm sessions keep the Pi process under ``runs/sessions`` and
        # rotate only the per-run credentials. Cold sessions use the run
        # directory directly. In both cases the child itself writes the
        # catalog after redeeming the ephemeral capability.
        catalogs = list(
            self.ws.backend.pi.runs_dir.rglob("connector-catalog.json")
        )
        self.assertEqual(len(catalogs), 1)
        catalog = json.loads(catalogs[0].read_text())
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

    def test_agent_run_does_not_inherit_connected_accounts_missing_from_bot_scope(self):
        signup = self.new_user()
        user_id = signup["user_id"]
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        client = FakeComposioClient()
        client.connected_accounts.items["ca_google"] = SimpleNamespace(
            id="ca_google",
            user_id=user_id,
            toolkit=SimpleNamespace(slug="googlesuper"),
            status="ACTIVE",
            alias="alan@example.com",
            data={},
            status_reason="",
        )
        gateway = ComposioConnectorGateway(
            client=client,
            public_base_url="https://agentgenia-api.onrender.com",
            store=self.ws.backend.store,
        )
        self.ws.backend.connector_gateway = gateway
        self.ws.backend.connectors.register_adapter(
            "google-workspace",
            ComposioConnectorAdapter(gateway, "google-workspace"),
        )

        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": (
                    "Eres un agente.\nNo hay conectores seleccionados.\n"
                    "Usuario: lee mis correos recientes"
                ),
                "user_message": "lee mis correos recientes",
                "execution_mode": "auto",
                "connector_ids": [],
                "idempotency_key": "recover-connected-google",
            },
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["connector_ids"], [])
        prompt = self.upstream_payloads("/v1/chat/completions")[-1]["messages"][0]["content"]
        self.assertNotIn("Google Workspace (google-workspace)", prompt)
        self.assertIn("No hay conectores seleccionados.", prompt)
        status, account_state = self.ws.req(
            "GET", "/v1/account-state", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            account_state["state"]["selectedConnectorIds"], []
        )

    def test_agent_run_does_not_infer_calendar_consent_from_text(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        bot_id = self.assign_bot_connectors(
            signup,
            ["google-workspace"],
            messages=[{
                "id": str(uuid.uuid4()),
                "role": "user",
                "text": "Crea un evento en mi calendario el 20 de agosto a las 7 am",
                "createdAt": "2026-08-17T12:00:00Z",
            }],
        )
        self.ws.backend.connector_gateway.connected_connector_ids = lambda _user_id: ("google-workspace",)
        issued: list[dict] = []
        original_issue = self.ws.backend.connectors.issue

        def issue_and_capture(**kwargs):
            issued.append(kwargs)
            return original_issue(**kwargs)

        self.ws.backend.connectors.issue = issue_and_capture
        status, _result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "Usuario: Quiero crear un evento nuevo\nUsuario: Sin hora final",
                "chat_prompt": "Usuario: Quiero crear un evento nuevo",
                "user_message": "hazlo",
                "execution_mode": "auto",
                "bot_id": bot_id,
                "connector_ids": ["google-workspace"],
                "idempotency_key": "calendar-write-grant",
            },
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(issued), 1, _result)
        self.assertIsNone(issued[0]["approved_action"])
        prompt = self.upstream_payloads("/v1/chat/completions")[-1]["messages"][0]["content"]
        self.assertIn("No pidas que el usuario responda 'apruebo'", prompt)
        self.assertIn("aprobación estructurada de un solo uso", prompt)

    def test_agent_run_confirmation_does_not_recover_prior_email_scope(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        bot_id = self.assign_bot_connectors(
            signup,
            ["google-workspace"],
            messages=[{
                "id": str(uuid.uuid4()),
                "role": "user",
                "text": "Envía un correo a cliente@example.com preguntando el costo por tarea",
                "createdAt": "2026-08-17T19:00:00Z",
            }],
        )
        self.ws.backend.connector_gateway.connected_connector_ids = (
            lambda _user_id: ("google-workspace",)
        )
        issued: list[dict] = []
        original_issue = self.ws.backend.connectors.issue

        def issue_and_capture(**kwargs):
            issued.append(kwargs)
            return original_issue(**kwargs)

        self.ws.backend.connectors.issue = issue_and_capture
        status, result = self.ws.req(
            "POST",
            "/v1/agent/run",
            {
                "prompt": "Usuario: Envía el correo\nUsuario: Hazlo tú directamente",
                "chat_prompt": (
                    "Usuario: Envía un correo a cliente@example.com preguntando "
                    "el costo por tarea\nUsuario: Hazlo tú directamente"
                ),
                "user_message": "Hazlo tú directamente",
                "execution_mode": "auto",
                "bot_id": bot_id,
                "connector_ids": ["google-workspace"],
                "idempotency_key": "gmail-send-confirmation-grant",
            },
            headers=headers,
        )

        self.assertEqual(status, 200, result)
        self.assertEqual(len(issued), 1)
        self.assertIsNone(issued[0]["approved_action"])

    def test_approved_connector_action_executes_without_a_second_model_round(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        bot_id = self.assign_bot_connectors(signup, ["google-workspace"])
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        adapter = FakeGitHubAdapter(user["id"])
        self.ws.backend.connectors.register_adapter("google-workspace", adapter)
        self.ws.backend.connector_gateway.connected_connector_ids = (
            lambda _user_id: ("google-workspace",)
        )
        prior = self.ws.backend.store.create_unmetered_agent_run(
            user_id=user["id"],
            idempotency_key="approved-direct-prior",
            model="deepseek-v4-flash",
            browser=False,
            max_credit_milli=1_000,
            max_concurrent_runs=4,
            token_hash="approved-direct-prior-token",
            token_expires_at=time.time() + 600,
        )
        arguments = {
            "recipient_email": "self@example.com",
            "subject": "Prueba directa",
            "body": "Contenido",
        }
        approval = self.ws.backend.store.create_pending_approval(
            user_id=user["id"],
            bot_id=bot_id,
            run_id=prior["run"]["id"],
            target_type="connector",
            connector_id="google-workspace",
            operation="send_email",
            arguments=arguments,
            arguments_hash=canonical_arguments_hash(arguments),
            human_summary="Enviar correo de prueba",
        )

        with patch.object(
            self.ws.backend.pi,
            "run",
            side_effect=AssertionError("Pi must not run after exact approval"),
        ) as pi_run:
            status, result = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": "Autorizar esta acción",
                    "bot_id": bot_id,
                    "connector_ids": ["google-workspace"],
                    "execution_mode": "agent",
                    "idempotency_key": "approved-direct-execution",
                    "approval": {
                        "approval_id": approval["id"],
                        "decision": "approve",
                    },
                },
                headers=headers,
            )

        self.assertEqual(status, 200, result)
        self.assertIn("Envié el correo", result["answer"])
        self.assertEqual(result["usage"]["input_tokens"], 0)
        self.assertEqual(adapter.calls, [
            (user["id"], "send_email", arguments),
        ])
        pi_run.assert_not_called()

    def test_rejected_connector_action_finishes_without_reprompting_or_model_round(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi()
        bot_id = self.assign_bot_connectors(signup, ["google-workspace"])
        user = self.ws.backend.store.get_user_by_api_key(signup["api_key"])
        adapter = FakeGitHubAdapter(user["id"])
        self.ws.backend.connectors.register_adapter("google-workspace", adapter)
        self.ws.backend.connector_gateway.connected_connector_ids = Mock(
            side_effect=AssertionError(
                "connector provider must not be checked after rejection"
            )
        )
        prior = self.ws.backend.store.create_unmetered_agent_run(
            user_id=user["id"],
            idempotency_key="rejected-direct-prior",
            model="deepseek-v4-flash",
            browser=False,
            max_credit_milli=1_000,
            max_concurrent_runs=4,
            token_hash="rejected-direct-prior-token",
            token_expires_at=time.time() + 600,
        )
        arguments = {
            "recipient_email": "self@example.com",
            "subject": "No enviar",
            "body": "Contenido",
        }
        approval = self.ws.backend.store.create_pending_approval(
            user_id=user["id"],
            bot_id=bot_id,
            run_id=prior["run"]["id"],
            target_type="connector",
            connector_id="google-workspace",
            operation="send_email",
            arguments=arguments,
            arguments_hash=canonical_arguments_hash(arguments),
            human_summary="Enviar correo que será cancelado",
        )

        with patch.object(
            self.ws.backend.pi,
            "run",
            side_effect=AssertionError("Pi must not run after rejection"),
        ) as pi_run:
            status, result = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": "Cancelar esta acción",
                    "bot_id": bot_id,
                    "connector_ids": ["google-workspace"],
                    "execution_mode": "agent",
                    "idempotency_key": "rejected-direct-execution",
                    "approval": {
                        "approval_id": approval["id"],
                        "decision": "reject",
                    },
                },
                headers=headers,
            )

        self.assertEqual(status, 200, result)
        self.assertEqual(
            json.loads(result["answer"])["text"],
            "Acción cancelada. No se realizó ningún cambio.",
        )
        self.assertEqual(result["usage"]["input_tokens"], 0)
        self.assertEqual(adapter.calls, [])
        pi_run.assert_not_called()

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

    def test_browser_run_requires_online_authenticated_desktop_and_never_starts_server_chrome(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        self.ws.enable_fake_pi(browser=True)

        with (
            patch("go_backend.server.runtime_memory_limit_mb", return_value=512),
            patch.object(
                self.ws.backend.pi,
                "run",
                side_effect=AssertionError("Render must never start browser Pi"),
            ),
        ):
            status, body = self.ws.req(
                "POST",
                "/v1/agent/run",
                {
                    "prompt": "navega una tienda pública",
                    "browser": True,
                    "idempotency_key": "browser-memory-admission",
                },
                headers=headers,
            )

        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "desktop_runtime_offline")

    def test_local_runtime_intent_keeps_connector_only_work_remote_and_routes_explicit_desktop_work(self):
        self.assertFalse(self.ws.backend._local_browser_intent("revisa mis correos de Gmail"))
        self.assertFalse(self.ws.backend._local_computer_intent("crea un evento en Calendar"))
        self.assertTrue(self.ws.backend._local_browser_intent("abre el marketplace en Chrome"))
        self.assertTrue(self.ws.backend._local_computer_intent("abre Excel en mi computadora"))

    def test_browser_run_with_connectors_is_relayed_to_same_account_desktop_with_one_time_run_key(self):
        signup = self.new_user()
        headers = {"Authorization": f"Bearer {signup['api_key']}"}
        bot_id = self.assign_bot_connectors(signup, ["google-workspace"])
        self.ws.backend.connector_gateway.connected_connector_ids = (
            lambda _user_id: ("google-workspace",)
        )
        device_id = str(uuid.uuid4())
        status, heartbeat = self.ws.req(
            "POST", "/v1/desktop-runtime/heartbeat",
            {
                "device_id": device_id,
                "platform": "darwin",
                "app_version": "1.1.1-test",
                "capabilities": {"browser": True, "computer": True},
            },
            headers=headers,
        )
        self.assertEqual(status, 200, heartbeat)
        self.assertTrue(heartbeat["online"])

        worker_errors: list[BaseException] = []
        claimed_payloads: list[dict] = []

        def desktop_worker():
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    claim_status, claimed = self.ws.req(
                        "POST", "/v1/desktop-runtime/jobs/claim",
                        {
                            "device_id": device_id,
                            "capabilities": {"browser": True, "computer": True},
                        },
                        headers=headers,
                    )
                    self.assertEqual(claim_status, 200, claimed)
                    if claimed["job"] is None:
                        time.sleep(0.05)
                        continue
                    job = claimed["job"]
                    claimed_payloads.append(job["payload"])
                    complete_status, complete = self.ws.req(
                        "POST", f"/v1/desktop-runtime/jobs/{job['id']}/complete",
                        {
                            "device_id": device_id,
                            "status": "succeeded",
                            "result": {
                                "answer": '{"text":"Abrí pi.dev en tu Chrome local.","widget":null}',
                                "model": "deepseek-v4-flash",
                                "duration_seconds": 0.2,
                                "usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 8,
                                    "cached_read_tokens": 0,
                                    "cached_write_tokens": 0,
                                },
                            },
                        },
                        headers=headers,
                    )
                    self.assertEqual(complete_status, 200, complete)
                    return
                raise AssertionError("desktop never received browser job")
            except BaseException as exc:
                worker_errors.append(exc)

        worker = threading.Thread(target=desktop_worker, daemon=True)
        worker.start()
        with patch.object(
            self.ws.backend.pi,
            "run",
            side_effect=AssertionError("Render must never execute browser Pi"),
        ):
            status, result = self.ws.req(
                "POST", "/v1/agent/run",
                {
                    "prompt": "revisa Gmail y abre https://pi.dev en Chrome",
                    "user_message": "revisa Gmail y abre https://pi.dev en Chrome",
                    "bot_id": bot_id,
                    "connector_ids": ["google-workspace"],
                    "browser": False,
                    "computer": False,
                    "idempotency_key": "desktop-native-chrome-relay",
                },
                headers=headers,
            )
        worker.join(timeout=2)

        self.assertEqual(worker_errors, [])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["execution_path"], "desktop_pi")
        self.assertTrue(result["browser"])
        self.assertEqual(len(claimed_payloads), 1)
        self.assertTrue(claimed_payloads[0]["run_api_key"].startswith("agrn_"))
        self.assertNotEqual(claimed_payloads[0]["run_api_key"], signup["api_key"])
        self.assertEqual(
            claimed_payloads[0]["backend_url"],
            self.ws.cfg.desktop_runtime_public_url,
        )
        self.assertTrue(claimed_payloads[0]["browser"])
        self.assertEqual(claimed_payloads[0]["connector_ids"], ["google-workspace"])
        self.assertTrue(claimed_payloads[0]["connector_run_token"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
