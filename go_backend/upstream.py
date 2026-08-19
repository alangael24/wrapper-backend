"""HTTP client for Agent Genia's server-owned DeepSeek account.

Soporta forwarding con streaming (SSE) y extraccion de `usage` de las
respuestas para registrar consumo.
"""

from __future__ import annotations

import json
import logging
import httpx
from urllib.parse import urljoin

UPSTREAM_TIMEOUT = 900
MAX_UPSTREAM_BODY = 16 * 1024 * 1024

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


_CLIENT = httpx.Client(
    # DeepSeek's OpenAI-compatible endpoint does not require HTTP/2. Keeping
    # this on HTTP/1.1 preserves connection pooling without making `h2` a
    # production/runtime dependency.
    http2=False,
    follow_redirects=False,
    timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=20.0),
    # Reuse TLS connections across normal pauses between chat messages.
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=300.0),
)


def _read_limited(response: httpx.Response, limit: int = MAX_UPSTREAM_BODY) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ValueError("Upstream response too large")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError("Upstream response too large")
        chunks.append(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise ValueError("Upstream response too large")
    return data


def build_request(method: str, base_url: str, path: str, headers: dict, body: bytes | None):
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    output: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        output[k] = v
    if not any(k.lower() == "user-agent" for k in headers):
        output["User-Agent"] = DEFAULT_UA
    return method, url, output, body


class Usage:
    """Resultado del parseo de uso de una respuesta."""

    def __init__(self, model=None, input_tokens=None, output_tokens=None,
                 cached_read=None, cached_write=None):
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_read = cached_read
        self.cached_write = cached_write

    def any(self) -> bool:
        return any(v is not None for v in (
            self.input_tokens, self.output_tokens, self.cached_read, self.cached_write))


def parse_usage_from_body(body: bytes, model: str | None = None) -> tuple[str | None, Usage]:
    """Extrae usage de una respuesta JSON no-stream (chat/responses/messages)."""
    try:
        data = json.loads(body)
    except Exception:
        return model, Usage(model=model)
    m = model or data.get("model")
    u = data.get("usage") or {}
    input_tokens = u.get("input_tokens")
    output_tokens = u.get("output_tokens")
    if input_tokens is None:
        input_tokens = u.get("prompt_tokens")  # chat.completions clasico
    if output_tokens is None:
        output_tokens = u.get("completion_tokens")
    cached_read = None
    cached_write = None
    det = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    cached_read = det.get("cached_tokens")
    if cached_read is None:
        cached_read = u.get("cached_tokens")
    if cached_read is None:
        cached_read = u.get("prompt_cache_hit_tokens")
    if cached_read is None:
        cached_read = u.get("cache_read_input_tokens")  # Anthropic
        cached_write = u.get("cache_creation_input_tokens")
    return m, Usage(m, input_tokens, output_tokens, cached_read, cached_write)


def parse_usage_from_sse_line(line: str, model: str | None = None) -> tuple[str | None, Usage]:
    """Intenta extraer usage de una linea SSE (eventos con 'usage' JSON)."""
    if not line.startswith("data:"):
        return model, Usage(model=model)
    payload = line[5:].strip()
    if payload == "[DONE]":
        return model, Usage(model=model)
    try:
        data = json.loads(payload)
    except Exception:
        return model, Usage(model=model)
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            m = model or data.get("model")
            u = usage
            input_tokens = u.get("input_tokens")
            output_tokens = u.get("output_tokens")
            if input_tokens is None:
                input_tokens = u.get("prompt_tokens")
            if output_tokens is None:
                output_tokens = u.get("completion_tokens")
            det = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
            cached_read = (
                det.get("cached_tokens")
                or u.get("cached_tokens")
                or u.get("prompt_cache_hit_tokens")
            )
            cached_write = u.get("cache_creation_input_tokens")
            if isinstance(data.get("type"), str) and data["type"] == "response.completed":
                cached_read = cached_read or u.get("cached_tokens")
            return m, Usage(m, input_tokens, output_tokens, cached_read, cached_write)
    return model, Usage(model=model)


def proxy_request(
    method: str,
    base_url: str,
    path: str,
    headers: dict,
    body: bytes | None,
    api_key: str,
    on_chunk=None,
    on_headers=None,
    timeout: httpx.Timeout | float | None = None,
    raise_on_timeout: bool = False,
) -> tuple[int, dict, bytes | None, Usage]:
    """Hace la request al upstream.

    - No-stream: devuelve (status, headers, body, usage).
    - Stream: devuelve (status, headers, None, usage) y va llamando
      on_chunk(bytes) con cada trozo (SSE) mientras llega.
    """
    hdrs = dict(headers)
    hdrs["Authorization"] = f"Bearer {api_key}"
    request_method, url, request_headers, request_body = build_request(
        method, base_url, path, hdrs, body
    )
    try:
        response_context = _CLIENT.stream(
            request_method,
            url,
            headers=request_headers,
            content=request_body,
            timeout=timeout,
        )
        resp = response_context.__enter__()
    except httpx.TimeoutException:
        if raise_on_timeout:
            raise
        logging.warning("Upstream timeout", exc_info=True)
        return 502, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "Upstream timeout", "type": "upstream_error"}}
        ).encode(), Usage()
    except httpx.HTTPError:
        logging.warning("Upstream no disponible", exc_info=True)
        return 502, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "Upstream unavailable", "type": "upstream_error"}}
        ).encode(), Usage()

    status = resp.status_code
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    content_type = resp.headers.get("content-type", "").lower()

    if status < 200 or status >= 300:
        try:
            err_body = _read_limited(resp)
        except ValueError:
            err_body = json.dumps(
                {"error": {"message": "Upstream response too large", "type": "upstream_error"}}
            ).encode()
        finally:
            response_context.__exit__(None, None, None)
        usage = parse_usage_from_body(err_body) if not is_stream(headers) else (None, Usage())
        return status, out_headers, err_body, usage[1]

    if is_stream(headers) or "text/event-stream" in content_type:
        if on_headers:
            on_headers(status, out_headers)
        model: str | None = None
        usage = Usage()
        try:
            pending = b""
            for chunk in resp.iter_raw():
                # Preservar el framing SSE exacto, incluidas las lineas vacias
                # que separan eventos. Clientes como Pi las necesitan.
                if on_chunk:
                    on_chunk(chunk)
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    m, u = parse_usage_from_sse_line(line, model)
                    if u.any():
                        usage = u
                    if m:
                        model = m
            if pending:
                line = pending.decode("utf-8", errors="replace").rstrip("\r")
                m, u = parse_usage_from_sse_line(line, model)
                if u.any():
                    usage = u
                if m:
                    model = m
        finally:
            response_context.__exit__(None, None, None)
        return status, out_headers, None, usage

    try:
        body_data = _read_limited(resp)
    except ValueError:
        response_context.__exit__(None, None, None)
        return 502, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "Upstream response too large", "type": "upstream_error"}}
        ).encode(), Usage()
    response_context.__exit__(None, None, None)
    m, usage = parse_usage_from_body(body_data)
    return status, out_headers, body_data, usage


def is_stream(headers: dict) -> bool:
    return headers.get("stream", "").lower() in ("true", "1")
