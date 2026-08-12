"""Policy data, URL validation and credential sentinels for Pi sandbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

PROVIDER_NAME = "wrapper-backend"
API_KEY_ENV = "WRAPPER_PI_API_KEY"
CONNECTOR_TOKEN_ENV = "PI_CONNECTOR_RUN_TOKEN"
CONNECTOR_URL_ENV = "PI_CONNECTOR_BROKER_URL"
CHROME_HOST_ENV = "PI_CHROME_BRIDGE_HOST"
CHROME_PORT_ENV = "PI_CHROME_BRIDGE_PORT"
SANDBOX_MARKER_ENV = "AGENTGENIA_SANDBOX"

RUNTIME_MOUNT = Path("/run/agentgenia-sandbox")
RUNTIME_DIRNAME = ".sandbox-runtime"
AUDIT_FILENAME = "sandbox-audit.json"
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")

MAX_REQUEST_BODY = 16 * 1024 * 1024
MAX_REQUEST_TARGET_BYTES = 8 * 1024
MAX_PROXY_CONNECTIONS = 64
PROXY_TIMEOUT_SECONDS = 3600
TMPFS_BYTES = 512 * 1024 * 1024
MIN_DELEGATED_PORT = 1024

MODEL_SUFFIXES = (
    "/chat/completions",
    "/responses",
    "/messages",
    "/models",
)
CONNECTOR_SUFFIXES = (
    "/v1/internal/connectors/",
    "/v1/internal/computers/",
)
MODEL_METHODS = frozenset({"GET", "POST"})
CONNECTOR_METHODS = frozenset({"GET", "POST"})
FORWARDED_REQUEST_HEADERS = frozenset({"accept", "content-type", "user-agent"})
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
BLOCKED_RESPONSE_HEADERS = frozenset({"set-cookie", "www-authenticate"})

# PiHarness already passes a narrow environment. This second allowlist prevents
# a future harness expansion from accidentally exposing server secrets.
CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "PI_CODING_AGENT_DIR",
        "PI_CODING_AGENT_SESSION_DIR",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
        "PI_OFFLINE",
        API_KEY_ENV,
        CONNECTOR_URL_ENV,
        CONNECTOR_TOKEN_ENV,
        CHROME_HOST_ENV,
        CHROME_PORT_ENV,
    }
)


class SandboxError(RuntimeError):
    """Configuration or startup error that must abort the run."""


@dataclass(frozen=True)
class ParsedLoopbackURL:
    port: int
    base_path: str
    normalized_url: str


@dataclass
class EndpointPolicy:
    """A single loopback capability exposed inside the network namespace."""

    sandbox_port: int
    target_host: str = "127.0.0.1"
    target_port: int = 0
    model_paths: set[str] = field(default_factory=set)
    connector_prefixes: set[str] = field(default_factory=set)
    model_secret: str | None = None
    model_sentinel: str | None = None
    connector_secret: str | None = None
    connector_sentinel: str | None = None
    raw_tcp: bool = False
    label: str = ""

    def response_masks(self) -> tuple[tuple[bytes, bytes], ...]:
        replacements: list[tuple[bytes, bytes]] = []
        if self.model_secret and self.model_sentinel:
            replacements.append(
                (self.model_secret.encode("ascii"), self.model_sentinel.encode("ascii"))
            )
        if self.connector_secret and self.connector_sentinel:
            replacements.append(
                (
                    self.connector_secret.encode("ascii"),
                    self.connector_sentinel.encode("ascii"),
                )
            )
        return tuple(replacements)

    def public_summary(self) -> dict[str, Any]:
        return {
            "sandbox": f"127.0.0.1:{self.sandbox_port}",
            "target": f"loopback:{self.target_port}",
            "mode": "raw_tcp" if self.raw_tcp else "http_capability_proxy",
            "roles": [
                role
                for role, enabled in (
                    ("model", bool(self.model_paths)),
                    ("connectors", bool(self.connector_prefixes)),
                    ("browser_bridge", self.raw_tcp),
                )
                if enabled
            ],
            "model_paths": sorted(self.model_paths),
            "connector_prefixes": sorted(self.connector_prefixes),
            "credentials_injected": [
                name
                for name, enabled in (
                    (API_KEY_ENV, bool(self.model_secret)),
                    (CONNECTOR_TOKEN_ENV, bool(self.connector_secret)),
                )
                if enabled
            ],
        }


@dataclass(frozen=True)
class SandboxPaths:
    repo_root: Path
    run_dir: Path
    workspace: Path
    home: Path
    config: Path
    runtime: Path
    audit: Path
    real_pi: Path
    node: Path
    socat: Path
    prlimit: Path
    bwrap: Path


class StreamingMasker:
    """Replace secrets even when a value is split across response chunks.

    Replacements must keep exactly the same byte length. That preserves an
    upstream ``Content-Length`` header while ensuring reflected credentials do
    not enter the sandbox.
    """

    def __init__(self, replacements: Iterable[tuple[bytes, bytes]]):
        pairs = tuple(
            sorted(
                ((source, target) for source, target in replacements if source),
                key=lambda pair: len(pair[0]),
                reverse=True,
            )
        )
        for source, target in pairs:
            if len(source) != len(target):
                raise ValueError("Proxy replacements must preserve byte length")
        self._pairs = pairs
        self._keep = max((len(source) - 1 for source, _ in pairs), default=0)
        self._tail = b""

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        data = self._tail + chunk
        if not self._pairs:
            self._tail = b""
            return data

        replaced = data
        for source, target in self._pairs:
            replaced = replaced.replace(source, target)

        if final or self._keep == 0:
            self._tail = b""
            return replaced

        emit_length = max(0, len(data) - self._keep)
        output = replaced[:emit_length]
        # Keeping the already-masked tail avoids reintroducing real bytes when
        # a complete secret crossed the emission boundary.
        self._tail = replaced[emit_length:]
        return output



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _join_url_path(base: str, suffix: str) -> str:
    clean_base = base.rstrip("/")
    clean_suffix = "/" + suffix.lstrip("/")
    return (clean_base + clean_suffix) or "/"


def _is_safe_http_path(path: str) -> bool:
    if not HTTP_PATH_RE.fullmatch(path) or "//" in path:
        return False
    return all(segment not in {".", ".."} for segment in path.split("/"))


def parse_loopback_http_url(raw: str, *, label: str) -> ParsedLoopbackURL:
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise SandboxError(f"{label} is not a valid URL") from exc
    if parsed.scheme != "http":
        raise SandboxError(f"{label} must use loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SandboxError(f"{label} cannot contain credentials, query or fragment")
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SandboxError(f"{label} must target loopback")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise SandboxError(f"{label} has an invalid port") from exc
    if not (MIN_DELEGATED_PORT <= port <= 65535):
        raise SandboxError(f"{label} must use an unprivileged port")
    base_path = parsed.path.rstrip("/")
    if not _is_safe_http_path(base_path or "/"):
        raise SandboxError(f"{label} has an ambiguous or unsafe path")
    normalized = urlunsplit(("http", f"127.0.0.1:{port}", base_path, "", ""))
    return ParsedLoopbackURL(port=port, base_path=base_path, normalized_url=normalized)


def _validate_header_secret(value: str, *, name: str) -> None:
    if not value:
        raise SandboxError(f"Missing {name}")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SandboxError(f"{name} must be ASCII") from exc
    if any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise SandboxError(f"{name} contains invalid HTTP header characters")


def make_sentinel(secret_value: str, *, label: str) -> str:
    _validate_header_secret(secret_value, name=label)
    length = len(secret_value.encode("ascii"))
    digest = hashlib.sha256(
        label.encode("ascii") + b":" + secrets.token_bytes(32)
    ).hexdigest().upper()
    prefix = f"SBX_{label.upper()}_"
    material = (prefix + digest * ((length // len(digest)) + 2))[:length]
    if material == secret_value:
        replacement = "X" if material[:1] != "X" else "Y"
        material = replacement + material[1:]
    return material


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def normalize_models_config(config_dir: Path) -> ParsedLoopbackURL:
    path = config_dir / "models.json"
    if path.is_symlink() or not path.is_file():
        raise SandboxError("Pi models.json must be a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        provider = payload["providers"][PROVIDER_NAME]
        raw_url = str(provider["baseUrl"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SandboxError("Could not read Pi's ephemeral models.json") from exc
    parsed = parse_loopback_http_url(raw_url, label="model baseUrl")
    provider["baseUrl"] = parsed.normalized_url
    _atomic_json(path, payload)
    return parsed


def _merge_endpoint(
    endpoints: dict[int, EndpointPolicy],
    *,
    port: int,
    label: str,
) -> EndpointPolicy:
    policy = endpoints.get(port)
    if policy is None:
        policy = EndpointPolicy(
            sandbox_port=port,
            target_port=port,
            label=label,
        )
        endpoints[port] = policy
    if policy.raw_tcp:
        raise SandboxError(
            f"Port {port} cannot be both a raw browser bridge and an HTTP proxy"
        )
    return policy


def build_endpoint_policies(
    env: dict[str, str],
    *,
    model_url: ParsedLoopbackURL,
) -> tuple[dict[int, EndpointPolicy], dict[str, str]]:
    child_env = {name: value for name, value in env.items() if name in CHILD_ENV_ALLOWLIST}
    endpoints: dict[int, EndpointPolicy] = {}

    model_secret = env.get(API_KEY_ENV, "")
    _validate_header_secret(model_secret, name=API_KEY_ENV)
    model_sentinel = make_sentinel(model_secret, label="MODEL_TOKEN")
    child_env[API_KEY_ENV] = model_sentinel
    model_policy = _merge_endpoint(endpoints, port=model_url.port, label="model")
    model_policy.model_paths.update(
        _join_url_path(model_url.base_path, suffix) for suffix in MODEL_SUFFIXES
    )
    model_policy.model_secret = model_secret
    model_policy.model_sentinel = model_sentinel

    connector_url_raw = env.get(CONNECTOR_URL_ENV)
    connector_secret = env.get(CONNECTOR_TOKEN_ENV)
    if connector_url_raw or connector_secret:
        if not connector_url_raw or not connector_secret:
            raise SandboxError("Connector capability requires both URL and token")
        connector_url = parse_loopback_http_url(
            connector_url_raw,
            label=CONNECTOR_URL_ENV,
        )
        connector_sentinel = make_sentinel(
            connector_secret,
            label="CONNECTOR_TOKEN",
        )
        child_env[CONNECTOR_URL_ENV] = connector_url.normalized_url
        child_env[CONNECTOR_TOKEN_ENV] = connector_sentinel
        connector_policy = _merge_endpoint(
            endpoints,
            port=connector_url.port,
            label="connectors",
        )
        connector_policy.connector_prefixes.update(
            _join_url_path(connector_url.base_path, suffix)
            for suffix in CONNECTOR_SUFFIXES
        )
        connector_policy.connector_secret = connector_secret
        connector_policy.connector_sentinel = connector_sentinel
    else:
        child_env.pop(CONNECTOR_URL_ENV, None)
        child_env.pop(CONNECTOR_TOKEN_ENV, None)

    chrome_host = env.get(CHROME_HOST_ENV)
    chrome_port_raw = env.get(CHROME_PORT_ENV)
    if chrome_host or chrome_port_raw:
        if chrome_host not in {"127.0.0.1", "localhost"} or not chrome_port_raw:
            raise SandboxError("Chrome bridge must use IPv4 loopback")
        try:
            chrome_port = int(chrome_port_raw)
        except ValueError as exc:
            raise SandboxError("Chrome bridge has an invalid port") from exc
        if not (MIN_DELEGATED_PORT <= chrome_port <= 65535):
            raise SandboxError("Chrome bridge must use an unprivileged port")
        if chrome_port in endpoints:
            raise SandboxError("Chrome bridge port collides with another capability")
        endpoints[chrome_port] = EndpointPolicy(
            sandbox_port=chrome_port,
            target_port=chrome_port,
            raw_tcp=True,
            label="browser_bridge",
        )
        child_env[CHROME_HOST_ENV] = "127.0.0.1"
        child_env[CHROME_PORT_ENV] = str(chrome_port)

    child_env[SANDBOX_MARKER_ENV] = "linux-bwrap-v1"
    child_env["TMPDIR"] = "/tmp"
    return endpoints, child_env



def _redact_runtime_secrets(text: str) -> str:
    redacted = text
    for name in (API_KEY_ENV, CONNECTOR_TOKEN_ENV):
        value = os.environ.get(name)
        if value:
            redacted = redacted.replace(value, f"<{name}:redacted>")
    return redacted


def _safe_error(error: BaseException) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ")
    return _redact_runtime_secrets(text)[:500]
