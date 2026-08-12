"""Cliente HTTP hacia el upstream de OpenCode Go.

Soporta forwarding con streaming (SSE) y extraccion de `usage` de las
respuestas para registrar consumo.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urljoin

UPSTREAM_TIMEOUT = 900  # 15 min, igual que el gateway de vision local

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def build_request(method: str, base_url: str, path: str, headers: dict, body: bytes | None):
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        req.add_header(k, v)
    if not any(k.lower() == "user-agent" for k in headers):
        req.add_header("User-Agent", DEFAULT_UA)
    return req


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
            cached_read = det.get("cached_tokens") or u.get("cached_tokens")
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
    go_api_key: str,
    on_chunk=None,
    on_headers=None,
) -> tuple[int, dict, bytes | None, Usage]:
    """Hace la request al upstream.

    - No-stream: devuelve (status, headers, body, usage).
    - Stream: devuelve (status, headers, None, usage) y va llamando
      on_chunk(bytes) con cada trozo (SSE) mientras llega.
    """
    hdrs = dict(headers)
    hdrs["Authorization"] = f"Bearer {go_api_key}"
    req = build_request(method, base_url, path, hdrs, body)
    try:
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as e:
        err_body = e.read()
        usage = parse_usage_from_body(err_body) if not is_stream(headers) else (None, Usage())
        return e.code, dict(e.headers), err_body, usage[1]
    except urllib.error.URLError:
        logging.warning("Upstream no disponible", exc_info=True)
        return 502, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "Upstream unavailable", "type": "upstream_error"}}
        ).encode(), Usage()

    status = resp.status
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    content_type = resp.headers.get("content-type", "").lower()

    if is_stream(headers) or "text/event-stream" in content_type:
        if on_headers:
            on_headers(status, out_headers)
        model: str | None = None
        usage = Usage()
        try:
            for raw in resp:
                # Preservar el framing SSE exacto, incluidas las lineas vacias
                # que separan eventos. Clientes como Pi las necesitan.
                if on_chunk:
                    on_chunk(raw)
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    m, u = parse_usage_from_sse_line(line, model)
                    if u.any():
                        usage = u
                    if m:
                        model = m
        finally:
            resp.close()
        return status, out_headers, None, usage

    body_data = resp.read()
    resp.close()
    m, usage = parse_usage_from_body(body_data)
    return status, out_headers, body_data, usage


def is_stream(headers: dict) -> bool:
    return headers.get("stream", "").lower() in ("true", "1")
