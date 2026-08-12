"""Unit tests for the external Pi launcher; Bubblewrap is not required."""

from __future__ import annotations

import json
import random
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from go_backend.pi_sandbox import (
    API_KEY_ENV,
    CONNECTOR_TOKEN_ENV,
    CONNECTOR_URL_ENV,
    CapabilityHTTPServer,
    EndpointPolicy,
    ParsedLoopbackURL,
    SandboxError,
    SandboxPaths,
    StreamingMasker,
    TMPFS_BYTES,
    build_bwrap_command,
    build_endpoint_policies,
    normalize_models_config,
    parse_loopback_http_url,
    write_entrypoint,
)


class RecordingUpstream(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    response_body = b"ok"
    response_headers: dict[str, str] = {}
    response_encoding: str | None = None

    def log_message(self, _format, *_args):
        return

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "connector_token": self.headers.get("X-Connector-Run-Token"),
                "sandbox_header": self.headers.get("X-AgentGenia-Sandbox"),
                "body": body,
            }
        )
        response = type(self).response_body
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(response)))
        if type(self).response_encoding:
            self.send_header("Content-Encoding", type(self).response_encoding)
        for name, value in type(self).response_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        self._handle()


def reset_upstream() -> None:
    RecordingUpstream.requests = []
    RecordingUpstream.response_body = b"ok"
    RecordingUpstream.response_headers = {}
    RecordingUpstream.response_encoding = None


def unix_request(socket_path: Path, request: bytes) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(socket_path))
        client.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        client.close()


BWRAP_HELP = " ".join(
    (
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-cgroup-try",
        "--cap-drop",
        "--disable-userns",
        "--clearenv",
        "--remount-ro",
        "--size",
        "--perms",
    )
)


def fake_paths(root: Path) -> SandboxPaths:
    repo = root / "repo"
    run = root / "0123456789abcdef0123456789abcdef"
    for path in (
        repo / "node_modules" / ".bin",
        repo / "extensions",
        run / "workspace",
        run / "home",
        run / "config" / "sessions",
        run / ".sandbox-runtime",
        root / "tools" / "bin",
    ):
        path.mkdir(parents=True, exist_ok=True)
    binaries = {
        "pi": repo / "node_modules" / ".bin" / "pi",
        "node": root / "tools" / "bin" / "node",
        "socat": root / "tools" / "bin" / "socat",
        "prlimit": root / "tools" / "bin" / "prlimit",
        "bwrap": root / "tools" / "bin" / "bwrap",
    }
    for binary in binaries.values():
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    return SandboxPaths(
        repo_root=repo,
        run_dir=run,
        workspace=run / "workspace",
        home=run / "home",
        config=run / "config",
        runtime=run / ".sandbox-runtime",
        audit=run / "sandbox-audit.json",
        real_pi=binaries["pi"],
        node=binaries["node"],
        socat=binaries["socat"],
        prlimit=binaries["prlimit"],
        bwrap=binaries["bwrap"],
    )


class PiSandboxTests(unittest.TestCase):
    def test_loopback_url_is_normalized_and_unsafe_urls_are_rejected(self):
        parsed = parse_loopback_http_url("http://localhost:8787/v1/", label="backend")
        self.assertEqual(parsed.port, 8787)
        self.assertEqual(parsed.base_path, "/v1")
        self.assertEqual(parsed.normalized_url, "http://127.0.0.1:8787/v1")
        for unsafe in (
            "https://api.example.com/v1",
            "http://10.0.0.5:8787/v1",
            "http://localhost:80/v1",
            "http://localhost:8787/v1/../admin",
            "http://localhost:8787/v1/%2e%2e/admin",
            "http://localhost:8787/v1//admin",
            "http://localhost:8787/v1\\admin",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(SandboxError):
                parse_loopback_http_url(unsafe, label="backend")

    def test_models_config_is_rewritten_only_to_loopback_ipv4(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            models = {
                "providers": {
                    "wrapper-backend": {
                        "baseUrl": "http://localhost:8787/v1/",
                        "apiKey": "$WRAPPER_PI_API_KEY",
                    }
                }
            }
            (config / "models.json").write_text(json.dumps(models), encoding="utf-8")
            parsed = normalize_models_config(config)
            self.assertEqual(parsed.port, 8787)
            payload = json.loads((config / "models.json").read_text(encoding="utf-8"))
            self.assertEqual(
                payload["providers"]["wrapper-backend"]["baseUrl"],
                "http://127.0.0.1:8787/v1",
            )
            self.assertEqual(
                payload["providers"]["wrapper-backend"]["apiKey"],
                "$WRAPPER_PI_API_KEY",
            )
            self.assertEqual((config / "models.json").stat().st_mode & 0o777, 0o600)

    def test_child_env_contains_sentinels_not_real_credentials(self):
        model_secret = "wrap_live_0123456789"
        connector_secret = "run_0123456789abcdef"
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp/run/home",
            "PI_CODING_AGENT_DIR": "/tmp/run/config",
            "PI_CODING_AGENT_SESSION_DIR": "/tmp/run/config/sessions",
            "LANG": "C.UTF-8",
            API_KEY_ENV: model_secret,
            CONNECTOR_URL_ENV: "http://localhost:8787",
            CONNECTOR_TOKEN_ENV: connector_secret,
            "ADMIN_TOKEN": "must-not-enter",
        }
        endpoints, child = build_endpoint_policies(
            env,
            model_url=ParsedLoopbackURL(
                port=8787,
                base_path="/v1",
                normalized_url="http://127.0.0.1:8787/v1",
            ),
        )
        self.assertNotEqual(child[API_KEY_ENV], model_secret)
        self.assertEqual(len(child[API_KEY_ENV]), len(model_secret))
        self.assertNotEqual(child[CONNECTOR_TOKEN_ENV], connector_secret)
        self.assertEqual(len(child[CONNECTOR_TOKEN_ENV]), len(connector_secret))
        self.assertNotIn("ADMIN_TOKEN", child)
        self.assertEqual(child[CONNECTOR_URL_ENV], "http://127.0.0.1:8787")
        policy = endpoints[8787]
        self.assertIn("/v1/chat/completions", policy.model_paths)
        self.assertIn("/v1/internal/connectors/", policy.connector_prefixes)

    def test_streaming_masker_handles_all_random_chunk_boundaries(self):
        actual = b"REAL_SECRET_12345"
        masked = b"MASKED_VALUE_1234"
        self.assertEqual(len(actual), len(masked))
        data = b"prefix " + actual + b" middle " + actual + b" suffix"
        expected = data.replace(actual, masked)
        generator = random.Random(7)
        for _ in range(3000):
            cuts = sorted(
                set(
                    generator.sample(
                        range(1, len(data)),
                        generator.randint(0, min(15, len(data) - 1)),
                    )
                )
            )
            masker = StreamingMasker(((actual, masked),))
            output = b""
            start = 0
            for cut in cuts:
                output += masker.feed(data[start:cut])
                start = cut
            output += masker.feed(data[start:])
            output += masker.feed(b"", final=True)
            self.assertEqual(output, expected)
            self.assertNotIn(actual, output)

    def test_model_proxy_injects_header_keeps_body_and_masks_response(self):
        model_secret = "wrap_live_0123456789"
        sentinel = "SBX_MODEL_0123456789"
        self.assertEqual(len(model_secret), len(sentinel))
        reset_upstream()
        RecordingUpstream.response_body = b"answer:" + model_secret.encode()
        RecordingUpstream.response_headers = {"X-Reflected-Token": model_secret}
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                socket_path = Path(tmp) / "proxy.sock"
                policy = EndpointPolicy(
                    sandbox_port=8787,
                    target_port=upstream.server_address[1],
                    model_paths={"/v1/chat/completions"},
                    model_secret=model_secret,
                    model_sentinel=sentinel,
                )
                proxy = CapabilityHTTPServer(str(socket_path), policy)
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    body = b'{"prompt":"' + sentinel.encode() + b'"}'
                    response = unix_request(
                        socket_path,
                        (
                            b"POST /v1/chat/completions HTTP/1.1\r\n"
                            b"Host: attacker.invalid\r\n"
                            + f"Authorization: Bearer {sentinel}\r\n".encode()
                            + f"Content-Length: {len(body)}\r\n".encode()
                            + b"Connection: close\r\n\r\n"
                            + body
                        ),
                    )
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2)
            self.assertEqual(len(RecordingUpstream.requests), 1)
            captured = RecordingUpstream.requests[0]
            self.assertEqual(captured["authorization"], f"Bearer {model_secret}")
            self.assertEqual(captured["body"], body)
            self.assertEqual(captured["sandbox_header"], "1")
            self.assertNotIn(model_secret.encode(), response)
            self.assertIn(sentinel.encode(), response)
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)

    def test_connector_proxy_replaces_child_token_only_in_header(self):
        connector_secret = "run_0123456789abcdef"
        sentinel = "SBX_0123456789ABCDEF"
        self.assertEqual(len(connector_secret), len(sentinel))
        reset_upstream()
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                socket_path = Path(tmp) / "proxy.sock"
                proxy = CapabilityHTTPServer(
                    str(socket_path),
                    EndpointPolicy(
                        sandbox_port=8787,
                        target_port=upstream.server_address[1],
                        connector_prefixes={"/v1/internal/connectors/"},
                        connector_secret=connector_secret,
                        connector_sentinel=sentinel,
                    ),
                )
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    body = b'{"token":"' + sentinel.encode() + b'"}'
                    response = unix_request(
                        socket_path,
                        (
                            b"POST /v1/internal/connectors/execute HTTP/1.1\r\n"
                            b"Host: attacker.invalid\r\n"
                            + f"X-Connector-Run-Token: {sentinel}\r\n".encode()
                            + f"Content-Length: {len(body)}\r\n".encode()
                            + b"Connection: close\r\n\r\n"
                            + body
                        ),
                    )
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2)
            self.assertIn(b" 200 ", response)
            self.assertEqual(len(RecordingUpstream.requests), 1)
            captured = RecordingUpstream.requests[0]
            self.assertEqual(captured["connector_token"], connector_secret)
            self.assertEqual(captured["body"], body)
            self.assertIsNone(captured["authorization"])
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)

    def test_proxy_denies_unlisted_and_ambiguous_paths(self):
        reset_upstream()
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                socket_path = Path(tmp) / "proxy.sock"
                proxy = CapabilityHTTPServer(
                    str(socket_path),
                    EndpointPolicy(
                        sandbox_port=8787,
                        target_port=upstream.server_address[1],
                        model_paths={"/v1/chat/completions"},
                        model_secret="wrap_live_0123456789",
                        model_sentinel="SBX_MODEL_0123456789",
                    ),
                )
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    for path in (
                        "/admin/users",
                        "/v1/chat/completions/../admin",
                        "/v1/chat/%2e%2e/admin",
                    ):
                        response = unix_request(
                            socket_path,
                            (
                                f"POST {path} HTTP/1.1\r\n"
                                "Host: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            ).encode(),
                        )
                        self.assertIn(b" 403 ", response)
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2)
            self.assertEqual(RecordingUpstream.requests, [])
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)

    def test_proxy_rejects_compressed_upstream_before_forwarding_body(self):
        reset_upstream()
        RecordingUpstream.response_encoding = "gzip"
        RecordingUpstream.response_body = b"not-actually-gzip"
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                socket_path = Path(tmp) / "proxy.sock"
                proxy = CapabilityHTTPServer(
                    str(socket_path),
                    EndpointPolicy(
                        sandbox_port=8787,
                        target_port=upstream.server_address[1],
                        model_paths={"/v1/models"},
                        model_secret="wrap_live_0123456789",
                        model_sentinel="SBX_MODEL_0123456789",
                    ),
                )
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    response = unix_request(
                        socket_path,
                        b"GET /v1/models HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                    )
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2)
            self.assertIn(b" 502 ", response)
            self.assertNotIn(b"not-actually-gzip", response)
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)

    def test_bwrap_command_never_mounts_host_root_or_real_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(Path(tmp))
            actual_secret = "REAL_SECRET_MUST_NOT_ENTER_COMMAND"
            endpoints = {
                8787: EndpointPolicy(
                    sandbox_port=8787,
                    target_port=8787,
                    model_paths={"/v1/models"},
                    model_secret=actual_secret,
                )
            }
            command, policy = build_bwrap_command(
                paths,
                arguments=["--mode", "rpc"],
                child_env={"LANG": "C.UTF-8", API_KEY_ENV: "SENTINEL"},
                endpoints=endpoints,
                bwrap_help=BWRAP_HELP,
            )
            triples = list(zip(command, command[1:], command[2:]))
            self.assertNotIn(("--ro-bind", "/", "/"), triples)
            self.assertNotIn(("--bind", "/", "/"), triples)
            self.assertIn("--unshare-net", command)
            self.assertIn("--cap-drop", command)
            self.assertIn("--clearenv", command)
            self.assertIn("--disable-userns", command)
            self.assertIn(str(TMPFS_BYTES), command)
            self.assertIn(("--remount-ro", "/"), list(zip(command, command[1:])))
            self.assertGreater(
                command.index("--remount-ro"),
                max(index for index, value in enumerate(command) if value in {"--bind", "--ro-bind"}),
            )
            self.assertFalse(policy["host_root_mounted"])
            self.assertTrue(policy["empty_root_tmpfs_readonly"])
            self.assertEqual(policy["resource_limits"]["private_tmpfs_bytes"], TMPFS_BYTES)
            self.assertNotIn(actual_secret, "\n".join(command))

    def test_bwrap_command_rejects_untrusted_extension_and_setuid_bwrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = fake_paths(root)
            outside = root / "outside" / "extension.ts"
            outside.parent.mkdir()
            outside.write_text("export default () => {}\n", encoding="utf-8")
            with self.assertRaises(SandboxError):
                build_bwrap_command(
                    paths,
                    arguments=["--extension", str(outside)],
                    child_env={"LANG": "C.UTF-8", API_KEY_ENV: "SENTINEL"},
                    endpoints={8787: EndpointPolicy(sandbox_port=8787, target_port=8787)},
                    bwrap_help=BWRAP_HELP,
                )
            trusted_extensions = paths.repo_root / "extensions"
            trusted_extensions.rmdir()
            trusted_extensions.symlink_to(outside.parent, target_is_directory=True)
            with self.assertRaises(SandboxError):
                build_bwrap_command(
                    paths,
                    arguments=["--extension", str(outside)],
                    child_env={"LANG": "C.UTF-8", API_KEY_ENV: "SENTINEL"},
                    endpoints={8787: EndpointPolicy(sandbox_port=8787, target_port=8787)},
                    bwrap_help=BWRAP_HELP,
                )
            trusted_extensions.unlink()
            trusted_extensions.mkdir()

            paths.bwrap.chmod(0o4755)
            with self.assertRaises(SandboxError):
                build_bwrap_command(
                    paths,
                    arguments=["--mode", "rpc"],
                    child_env={"LANG": "C.UTF-8", API_KEY_ENV: "SENTINEL"},
                    endpoints={8787: EndpointPolicy(sandbox_port=8787, target_port=8787)},
                    bwrap_help=BWRAP_HELP,
                )

    def test_entrypoint_uses_absolute_prlimit_and_preserves_signal_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(Path(tmp))
            write_entrypoint(
                paths,
                {8787: EndpointPolicy(sandbox_port=8787, target_port=8787)},
            )
            entrypoint = (paths.runtime / "entrypoint.sh").read_text(encoding="utf-8")
            self.assertIn(str(paths.prlimit), entrypoint)
            self.assertIn("--nproc=256:256", entrypoint)
            self.assertIn("exit 129", entrypoint)
            self.assertIn("exit 130", entrypoint)
            self.assertIn("exit 143", entrypoint)
            self.assertNotIn("command -v prlimit", entrypoint)


if __name__ == "__main__":
    unittest.main()
