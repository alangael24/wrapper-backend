"""Ejecutor aislado de Pi en modo RPC para tareas del wrapper."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PROVIDER_NAME = "wrapper-backend"
API_KEY_ENV = "WRAPPER_PI_API_KEY"


class PiHarnessError(RuntimeError):
    """Error controlado al iniciar o ejecutar Pi."""


class PiHarnessBusy(PiHarnessError):
    """No hay un slot de ejecucion disponible."""


@dataclass
class PiRunResult:
    run_id: str
    answer: str
    model: str
    duration_seconds: float
    usage: dict[str, int]
    browser: bool
    event_log: str
    stderr_log: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("event_log", None)
        result.pop("stderr_log", None)
        return result


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for block in content:
        if isinstance(block, str):
            chunks.append(block)
        elif isinstance(block, dict) and block.get("type") in ("text", "output_text"):
            chunks.append(str(block.get("text") or ""))
    return "".join(chunks)


class PiHarness:
    def __init__(
        self,
        *,
        enabled: bool,
        binary: str,
        backend_url: str,
        runs_dir: Path,
        model: str,
        thinking: str,
        timeout_seconds: int,
        max_concurrent: int,
        max_prompt_chars: int,
        node_bin_dir: str | None = None,
        chrome_extension: str | None = None,
        chrome_auto_authorize: bool = False,
        chrome_authorize_minutes: int = 30,
    ):
        self.enabled = enabled
        self.binary = binary
        self.backend_url = backend_url.rstrip("/")
        self.runs_dir = runs_dir
        self.model = model
        self.thinking = thinking
        self.timeout_seconds = timeout_seconds
        self.max_concurrent = max(1, max_concurrent)
        self.max_prompt_chars = max_prompt_chars
        self.node_bin_dir = node_bin_dir
        self.chrome_extension = chrome_extension
        self.chrome_auto_authorize = chrome_auto_authorize
        self.chrome_authorize_minutes = chrome_authorize_minutes
        self._slots = threading.BoundedSemaphore(self.max_concurrent)

    def _resolved_binary(self) -> str | None:
        if os.path.sep in self.binary:
            path = Path(self.binary).expanduser()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(self.binary)

    def status(self) -> dict[str, Any]:
        resolved = self._resolved_binary()
        runtime_path = os.environ.get("PATH", "/usr/bin:/bin")
        if self.node_bin_dir:
            runtime_path = self.node_bin_dir + os.pathsep + runtime_path
        return {
            "enabled": self.enabled,
            "available": bool(resolved),
            "binary": resolved,
            "model": self.model,
            "thinking": self.thinking,
            "max_concurrent": self.max_concurrent,
            "node_available": bool(shutil.which("node", path=runtime_path)),
            "browser_available": bool(self.chrome_extension),
            "browser_auto_authorize": self.chrome_auto_authorize,
        }

    def run(self, *, user_api_key: str, prompt: str, browser: bool = False) -> PiRunResult:
        if not self.enabled:
            raise PiHarnessError("El harness de Pi esta desactivado (PI_ENABLED=0)")
        if not self._resolved_binary():
            raise PiHarnessError(f"No se encontro el ejecutable de Pi: {self.binary}")
        if not prompt.strip():
            raise PiHarnessError("El prompt no puede estar vacio")
        if len(prompt) > self.max_prompt_chars:
            raise PiHarnessError(
                f"El prompt excede PI_MAX_PROMPT_CHARS ({self.max_prompt_chars})"
            )
        if browser and (not self.chrome_extension or not self.chrome_auto_authorize):
            raise PiHarnessError(
                "Chrome requiere PI_CHROME_EXTENSION y PI_CHROME_AUTO_AUTHORIZE=1"
            )
        if not self._slots.acquire(blocking=False):
            raise PiHarnessBusy("Todos los slots de Pi estan ocupados")
        try:
            return self._run(user_api_key=user_api_key, prompt=prompt, browser=browser)
        finally:
            self._slots.release()

    def _write_config(self, config_dir: Path) -> None:
        payload = {
            "providers": {
                PROVIDER_NAME: {
                    "baseUrl": self.backend_url + "/v1",
                    "api": "openai-completions",
                    "apiKey": f"${API_KEY_ENV}",
                    "authHeader": True,
                    "models": [
                        {
                            "id": self.model,
                            "name": f"{self.model} (wrapper)",
                            "reasoning": True,
                            "input": ["text"],
                            "cost": {
                                "input": 0.14,
                                "output": 0.28,
                                "cacheRead": 0.0028,
                                "cacheWrite": 0,
                            },
                            "contextWindow": 1_000_000,
                            "maxTokens": 384_000,
                            "thinkingLevelMap": {
                                "off": None,
                                "minimal": None,
                                "low": None,
                                "medium": None,
                                "high": "high",
                                "xhigh": None,
                                "max": "max",
                            },
                            "compat": {
                                "supportsStore": False,
                                "supportsDeveloperRole": False,
                                "maxTokensField": "max_tokens",
                                "supportsLongCacheRetention": False,
                                "requiresReasoningContentOnAssistantMessages": True,
                            },
                        }
                    ],
                }
            }
        }
        (config_dir / "models.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def _child_env(self, run_dir: Path, config_dir: Path, user_api_key: str) -> dict[str, str]:
        # No heredar ADMIN_TOKEN, WRAPPER_SECRET ni otras API keys del servidor.
        runtime_path = os.environ.get("PATH", "/usr/bin:/bin")
        if self.node_bin_dir:
            runtime_path = self.node_bin_dir + os.pathsep + runtime_path
        env = {
            "PATH": runtime_path,
            "HOME": str(run_dir / "home"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PI_CODING_AGENT_DIR": str(config_dir),
            "PI_CODING_AGENT_SESSION_DIR": str(config_dir / "sessions"),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "PI_OFFLINE": "1",
            API_KEY_ENV: user_api_key,
        }
        return env

    def _command(self, browser: bool) -> list[str]:
        command = [
            self._resolved_binary() or self.binary,
            "--mode", "rpc",
            "--no-session",
            "--provider", PROVIDER_NAME,
            "--model", self.model,
            "--thinking", self.thinking,
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-approve",
            "--offline",
        ]
        if browser and self.chrome_extension:
            command.extend(["--extension", self.chrome_extension])
        return command

    def _run(self, *, user_api_key: str, prompt: str, browser: bool) -> PiRunResult:
        run_id = uuid.uuid4().hex
        run_dir = self.runs_dir / run_id
        config_dir = run_dir / "config"
        work_dir = run_dir / "workspace"
        for path in (config_dir / "sessions", work_dir, run_dir / "home"):
            path.mkdir(parents=True, exist_ok=True)
        self._write_config(config_dir)
        event_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"
        started = time.monotonic()
        deadline = started + self.timeout_seconds if self.timeout_seconds > 0 else None
        usage = {"input_tokens": 0, "output_tokens": 0, "cached_read_tokens": 0,
                 "cached_write_tokens": 0}
        answer = ""
        agent_error = ""
        settled = False

        with event_path.open("w", encoding="utf-8") as event_log, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_log:
            process = subprocess.Popen(
                self._command(browser),
                cwd=work_dir,
                env=self._child_env(run_dir, config_dir, user_api_key),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_log,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            assert process.stdin is not None and process.stdout is not None
            events: queue.Queue[str | None] = queue.Queue()

            def read_events() -> None:
                try:
                    for line in process.stdout:
                        events.put(line)
                finally:
                    events.put(None)

            reader = threading.Thread(target=read_events, daemon=True)
            reader.start()

            def send(payload: dict[str, Any]) -> None:
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()

            task_sent = not browser
            if browser:
                send({
                    "id": "chrome-authorize",
                    "type": "prompt",
                    "message": f"/chrome authorize {self.chrome_authorize_minutes}m",
                })
            else:
                send({"id": "agent-task", "type": "prompt", "message": prompt})

            try:
                while deadline is None or time.monotonic() < deadline:
                    wait = 0.5 if deadline is None else min(0.5, max(0.0, deadline - time.monotonic()))
                    try:
                        line = events.get(timeout=wait)
                    except queue.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    if line is None:
                        break
                    event_log.write(line)
                    event_log.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") == "extension_ui_request":
                        request_id = event.get("id")
                        if event.get("method") == "confirm" and event.get("title") == "Authorize pi-chrome control?":
                            send({"type": "extension_ui_response", "id": request_id, "confirmed": True})
                        else:
                            send({"type": "extension_ui_response", "id": request_id, "cancelled": True})

                    if event.get("type") == "response" and event.get("id") == "chrome-authorize":
                        if not event.get("success"):
                            raise PiHarnessError(
                                f"No se pudo autorizar Chrome: {event.get('error', 'error desconocido')}"
                            )
                        send({"id": "agent-task", "type": "prompt", "message": prompt})
                        task_sent = True

                    if event.get("type") == "message_end":
                        message = event.get("message") or {}
                        if message.get("role") == "assistant":
                            if message.get("stopReason") in ("error", "aborted"):
                                agent_error = str(
                                    message.get("errorMessage") or message.get("stopReason")
                                )
                            text = _message_text(message)
                            if text:
                                answer = text
                            raw_usage = message.get("usage") or event.get("usage") or {}
                            usage["input_tokens"] += int(raw_usage.get("input") or raw_usage.get("input_tokens") or 0)
                            usage["output_tokens"] += int(raw_usage.get("output") or raw_usage.get("output_tokens") or 0)
                            usage["cached_read_tokens"] += int(raw_usage.get("cacheRead") or raw_usage.get("cached_read_tokens") or 0)
                            usage["cached_write_tokens"] += int(raw_usage.get("cacheWrite") or raw_usage.get("cached_write_tokens") or 0)

                    if task_sent and event.get("type") == "agent_settled":
                        settled = True
                        break

                if not settled:
                    if deadline is not None and time.monotonic() >= deadline:
                        try:
                            send({"type": "abort"})
                        except (BrokenPipeError, OSError):
                            pass
                        raise PiHarnessError(f"Pi excedio el timeout de {self.timeout_seconds}s")
                    raise PiHarnessError(
                        f"Pi termino antes de completar la tarea (exit={process.poll()})"
                    )
                if agent_error:
                    raise PiHarnessError(f"Pi no pudo completar la tarea: {agent_error}")
                if not answer:
                    raise PiHarnessError("Pi completo la ejecucion sin una respuesta final")
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                reader.join(timeout=1)
                process.stdout.close()

        return PiRunResult(
            run_id=run_id,
            answer=answer,
            model=self.model,
            duration_seconds=round(time.monotonic() - started, 3),
            usage=usage,
            browser=browser,
            event_log=str(event_path),
            stderr_log=str(stderr_path),
        )
