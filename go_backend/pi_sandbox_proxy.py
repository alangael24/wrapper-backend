"""Fixed loopback capability proxies for the Pi sandbox."""

from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .pi_sandbox_model import (
    API_KEY_ENV,
    BLOCKED_RESPONSE_HEADERS,
    CONNECTOR_METHODS,
    CONNECTOR_TOKEN_ENV,
    FORWARDED_REQUEST_HEADERS,
    HOP_BY_HOP_HEADERS,
    MAX_PROXY_CONNECTIONS,
    MAX_REQUEST_BODY,
    MAX_REQUEST_TARGET_BYTES,
    MODEL_METHODS,
    PROXY_TIMEOUT_SECONDS,
    EndpointPolicy,
    SandboxError,
    StreamingMasker,
    _is_safe_http_path,
    _safe_error,
)

class BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """Thread-per-connection server with a hard host-side fanout limit."""

    daemon_threads = True
    block_on_close = True

    def __init__(self, *args: Any, **kwargs: Any):
        self._connection_slots = threading.BoundedSemaphore(MAX_PROXY_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class CapabilityHTTPServer(BoundedThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, socket_path: str, policy: EndpointPolicy):
        self.policy = policy
        super().__init__(socket_path, CapabilityProxyHandler)
        os.chmod(socket_path, 0o600)


class CapabilityProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentGeniaSandbox/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def handle_expect_100(self) -> bool:
        self._send_json_error(417, "Expect is not allowed by this capability")
        return False

    @property
    def policy(self) -> EndpointPolicy:
        return self.server.policy  # type: ignore[attr-defined, no-any-return]

    def _send_json_error(self, status: int, message: str) -> None:
        body = json.dumps(
            {"error": {"type": "sandbox_proxy", "message": message}},
            separators=(",", ":"),
        ).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _route(self) -> str | None:
        try:
            encoded_length = len(self.path.encode("utf-8", errors="surrogatepass"))
            parsed = urlsplit(self.path)
        except (UnicodeError, ValueError):
            return None
        if encoded_length > MAX_REQUEST_TARGET_BYTES:
            return None
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        if not _is_safe_http_path(parsed.path):
            return None
        if parsed.path in self.policy.model_paths and self.command in MODEL_METHODS:
            return "model"
        if (
            any(parsed.path.startswith(prefix) for prefix in self.policy.connector_prefixes)
            and self.command in CONNECTOR_METHODS
        ):
            return "connector"
        return None

    def _read_body(self) -> bytes:
        transfer_values = self.headers.get_all("Transfer-Encoding", [])
        transfer_encoding = ",".join(transfer_values).strip().lower()
        if transfer_encoding and transfer_encoding != "identity":
            raise SandboxError("Transfer-Encoding is not allowed")
        if self.headers.get("Expect"):
            raise SandboxError("Expect is not allowed")
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) > 1:
            raise SandboxError("Duplicate Content-Length")
        raw_length = (length_values[0] if length_values else "0").strip()
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise SandboxError("Invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BODY:
            raise SandboxError("Request body exceeds capability limit")
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            raise SandboxError("Client closed the request body early")
        return body

    def _forward_headers(self, role: str, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if name.lower() not in FORWARDED_REQUEST_HEADERS:
                continue
            if "\r" in value or "\n" in value:
                continue
            headers[name] = value
        headers["Host"] = f"{self.policy.target_host}:{self.policy.target_port}"
        headers["Connection"] = "close"
        headers["Accept-Encoding"] = "identity"
        headers["X-AgentGenia-Sandbox"] = "1"
        if body:
            headers["Content-Length"] = str(len(body))
        elif self.command in {"POST", "PUT", "PATCH"}:
            headers["Content-Length"] = "0"

        if role == "model":
            if not self.policy.model_secret:
                raise SandboxError("Missing model capability credential")
            headers["Authorization"] = f"Bearer {self.policy.model_secret}"
        elif role == "connector":
            if not self.policy.connector_secret:
                raise SandboxError("Missing connector capability credential")
            headers["X-Connector-Run-Token"] = self.policy.connector_secret
        return headers

    def _mask_header_value(self, value: str) -> str:
        masked = value
        for source, target in self.policy.response_masks():
            masked = masked.replace(source.decode("ascii"), target.decode("ascii"))
        return masked

    def _proxy(self) -> None:
        role = self._route()
        if role is None:
            self._send_json_error(403, "Route or method is outside this capability")
            return
        try:
            body = self._read_body()
            headers = self._forward_headers(role, body)
        except SandboxError as exc:
            self._send_json_error(400, str(exc))
            return

        parsed = urlsplit(self.path)
        upstream_path = urlunsplit(SplitResult("", "", parsed.path, parsed.query, ""))
        connection = http.client.HTTPConnection(
            self.policy.target_host,
            self.policy.target_port,
            timeout=PROXY_TIMEOUT_SECONDS,
        )
        headers_sent = False
        try:
            connection.request(self.command, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            content_encoding = (response.getheader("Content-Encoding") or "").strip().lower()
            if content_encoding not in {"", "identity"}:
                raise SandboxError("Upstream Content-Encoding is not allowed")

            self.send_response_only(response.status)
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in HOP_BY_HOP_HEADERS or lowered in BLOCKED_RESPONSE_HEADERS:
                    continue
                if "\r" in value or "\n" in value:
                    continue
                self.send_header(name, self._mask_header_value(value))
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True

            if self.command != "HEAD":
                masker = StreamingMasker(self.policy.response_masks())
                read_chunk = getattr(response, "read1", response.read)
                while True:
                    chunk = read_chunk(64 * 1024)
                    if not chunk:
                        break
                    output = masker.feed(chunk)
                    if output:
                        self.wfile.write(output)
                        self.wfile.flush()
                tail = masker.feed(b"", final=True)
                if tail:
                    self.wfile.write(tail)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException, SandboxError) as exc:
            if not headers_sent and not self.wfile.closed:
                try:
                    self._send_json_error(
                        502,
                        f"Loopback capability is unavailable: {_safe_error(exc)}",
                    )
                except OSError:
                    pass
        finally:
            self.close_connection = True
            connection.close()


class RawRelayServer(BoundedThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, socket_path: str, policy: EndpointPolicy):
        self.policy = policy
        super().__init__(socket_path, RawRelayHandler)
        os.chmod(socket_path, 0o600)


class RawRelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        policy: EndpointPolicy = self.server.policy  # type: ignore[attr-defined]
        try:
            upstream = socket.create_connection(
                (policy.target_host, policy.target_port), timeout=30
            )
        except OSError:
            return

        with upstream:
            client: socket.socket = self.request
            client.settimeout(None)
            upstream.settimeout(None)

            def copy(source: socket.socket, target: socket.socket) -> None:
                try:
                    while True:
                        data = source.recv(64 * 1024)
                        if not data:
                            break
                        target.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            outbound = threading.Thread(target=copy, args=(client, upstream), daemon=True)
            outbound.start()
            copy(upstream, client)
            outbound.join(timeout=1)


class Relay:
    def __init__(self, socket_path: Path, policy: EndpointPolicy):
        self.socket_path = socket_path
        self.policy = policy
        self._started = False
        if socket_path.exists() or socket_path.is_symlink():
            socket_path.unlink()
        # Linux sun_path is 108 bytes; leave room for the terminating NUL.
        if len(os.fsencode(socket_path)) >= 104:
            raise SandboxError("Sandbox Unix socket path is too long")
        server_class: type[socketserver.BaseServer]
        server_class = RawRelayServer if policy.raw_tcp else CapabilityHTTPServer
        self.server = server_class(str(socket_path), policy)  # type: ignore[call-arg]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name=f"pi-sandbox-relay-{policy.sandbox_port}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        self._started = True
        if not self.thread.is_alive():
            raise SandboxError(f"Relay {self.policy.sandbox_port} failed to start")

    def close(self) -> None:
        if self._started:
            self.server.shutdown()
        self.server.server_close()
        if self._started:
            self.thread.join(timeout=2)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
