#!/usr/bin/env python3
"""Fail a release when the production Agent Genia backend is not usable."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_API_BASE_URL = "https://agentgenia-api.onrender.com"
REQUIRED_CHECKS = (
    "database",
    "google_auth",
    "apple_auth",
    "stripe",
    "connectors",
    "computers",
    "pi",
    "desktop_relay",
    "model_provider",
    "whatsapp",
)


def readiness_url() -> str:
    base = os.environ.get("AGENTGENIA_API_BASE_URL", DEFAULT_API_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(base)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("AGENTGENIA_API_BASE_URL must use HTTPS outside loopback")
    return f"{base}/readyz"


def main() -> int:
    try:
        url = readiness_url()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "AgentGenia-Release-Gate/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Production readiness request failed: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("Production readiness response is not a JSON object", file=sys.stderr)
        return 1
    checks = payload.get("checks")
    failed = [name for name in REQUIRED_CHECKS if not isinstance(checks, dict) or checks.get(name) is not True]
    if payload.get("ready") is not True or payload.get("environment") != "production" or failed:
        suffix = f"; failed checks: {', '.join(failed)}" if failed else ""
        print(f"Production backend is not release-ready{suffix}", file=sys.stderr)
        return 1

    print(f"Production backend is release-ready at {url}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
