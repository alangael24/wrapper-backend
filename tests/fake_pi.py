#!/usr/bin/env python3
"""Pi RPC falso para probar el cableado sin consumir el modelo real."""

import json
import os
import sys
import urllib.request
from pathlib import Path


config_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
config = json.loads((config_dir / "models.json").read_text(encoding="utf-8"))
provider = config["providers"]["wrapper-backend"]
wrapper_key = os.environ[provider["apiKey"].lstrip("$")]

for line in sys.stdin:
    command = json.loads(line)
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
    events = [
        {"type": "agent_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"fake-pi uso {model}: {upstream_text}"}],
                "usage": {"input": 11, "output": 7, "cacheRead": 3, "cacheWrite": 0},
            },
        },
        {"type": "agent_settled"},
    ]
    for event in events:
        print(json.dumps(event), flush=True)
