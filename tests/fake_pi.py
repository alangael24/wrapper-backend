#!/usr/bin/env python3
"""Pi RPC falso para probar el cableado sin consumir el modelo real."""

import json
import os
import sys
import urllib.request
from pathlib import Path


config_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
if os.environ.get("PI_CHROME_BRIDGE_PORT"):
    (config_dir / "chrome-bridge-port.txt").write_text(
        os.environ["PI_CHROME_BRIDGE_PORT"], encoding="utf-8"
    )
config = json.loads((config_dir / "models.json").read_text(encoding="utf-8"))
provider = config["providers"]["wrapper-backend"]
runtime_auth_file = os.environ.get("PI_RUNTIME_AUTH_FILE")


def runtime_credentials():
    if runtime_auth_file:
        return json.loads(Path(runtime_auth_file).read_text(encoding="utf-8"))
    return {
        "run_api_key": os.environ[provider["apiKey"].lstrip("$")],
        "connector_run_token": os.environ.get("PI_CONNECTOR_RUN_TOKEN", ""),
    }


connector_token = runtime_credentials().get("connector_run_token")
if connector_token and not runtime_auth_file:
    connector_request = urllib.request.Request(
        os.environ["PI_CONNECTOR_BROKER_URL"] + "/v1/internal/connectors/catalog",
        headers={"X-Connector-Run-Token": connector_token},
    )
    with urllib.request.urlopen(connector_request, timeout=5) as response:
        connector_catalog = json.load(response)
    (config_dir / "connector-catalog.json").write_text(
        json.dumps(connector_catalog), encoding="utf-8"
    )

for line in sys.stdin:
    command = json.loads(line)
    if command.get("type") == "get_state":
        print(json.dumps({
            "id": command.get("id"),
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"isStreaming": False},
        }), flush=True)
        continue
    if command.get("type") == "extension_ui_response" and command.get("id") == "fake-chrome-confirm":
        print(json.dumps({
            "id": "chrome-authorize",
            "type": "response",
            "success": bool(command.get("confirmed")),
        }), flush=True)
        continue
    if command.get("type") != "prompt":
        continue
    if command.get("id") == "chrome-authorize":
        print(json.dumps({
            "id": "fake-chrome-confirm",
            "type": "extension_ui_request",
            "method": "confirm",
            "title": "Authorize pi-chrome control?",
        }), flush=True)
        continue
    credentials = runtime_credentials()
    wrapper_key = credentials["run_api_key"]
    connector_token = credentials.get("connector_run_token")
    if connector_token:
        connector_request = urllib.request.Request(
            os.environ["PI_CONNECTOR_BROKER_URL"] + "/v1/internal/connectors/catalog",
            headers={"X-Connector-Run-Token": connector_token},
        )
        with urllib.request.urlopen(connector_request, timeout=5) as response:
            connector_catalog = json.load(response)
        (config_dir / "connector-catalog.json").write_text(
            json.dumps(connector_catalog), encoding="utf-8"
        )
    model = provider["models"][0]["id"]
    request = urllib.request.Request(
        provider["baseUrl"] + "/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": command.get("message", "")}],
        }).encode(),
        headers={"Authorization": f"Bearer {wrapper_key}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        completion = json.load(response)
    upstream_text = completion["choices"][0]["message"]["content"]
    answer_text = f"fake-pi uso {model}: {upstream_text}"
    if command.get("message") == "__stream_json__":
        answer_text = json.dumps(
            {"text": "hola rápido", "widget": None}, ensure_ascii=False
        )
    midpoint = max(1, len(answer_text) // 2)
    events = [
        {"type": "agent_start"},
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta", "contentIndex": 0,
                "delta": answer_text[:midpoint],
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta", "contentIndex": 0,
                "delta": answer_text[midpoint:],
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": answer_text}],
                "usage": {"input": 11, "output": 7, "cacheRead": 3, "cacheWrite": 0},
            },
        },
        {"type": "agent_settled"},
    ]
    for event in events:
        print(json.dumps(event), flush=True)
