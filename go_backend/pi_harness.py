"""Ejecutor aislado de Pi en modo RPC para tareas del wrapper."""

from __future__ import annotations

import json
import hashlib
import os
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


PROVIDER_NAME = "wrapper-backend"
API_KEY_ENV = "WRAPPER_PI_API_KEY"
CHROME_BRIDGE_URL = "http://127.0.0.1:17318"
CHROME_ISOLATION_PER_RUN = "per_run"
RUNTIME_AUTH_EXTENSION = (
    Path(__file__).resolve().parent.parent / "extensions" / "runtime-auth" / "index.ts"
)


class PiHarnessError(RuntimeError):
    """Error controlado al iniciar o ejecutar Pi."""


class PiHarnessBusy(PiHarnessError):
    """No hay un slot de ejecucion disponible."""


class PiHarnessUsageError(PiHarnessError):
    """La tarea comenzó y debe liquidarse por el consumo real acumulado."""


class PiHarnessTimeout(PiHarnessUsageError):
    """La tarea consumió recursos pero no terminó antes de su deadline."""


@dataclass
class PiRunResult:
    run_id: str
    answer: str
    model: str
    duration_seconds: float
    usage: dict[str, int | float]
    browser: bool
    event_log: str
    stderr_log: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("event_log", None)
        result.pop("stderr_log", None)
        return result


@dataclass
class _WarmSession:
    key: str
    owner_key: str
    session_id: str
    root: Path
    config_dir: Path
    work_dir: Path
    auth_file: Path
    lock: threading.Lock
    process: subprocess.Popen[str] | None = None
    events: queue.Queue[str | None] | None = None
    reader: threading.Thread | None = None
    stderr_stream: Any = None
    last_used: float = 0.0


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
        warm_sessions_enabled: bool = False,
        session_idle_seconds: int = 900,
        max_warm_sessions: int | None = None,
        supports_images: bool = False,
        node_bin_dir: str | None = None,
        connector_extension: str | None = None,
        connector_broker_url: str | None = None,
        chrome_extension: str | None = None,
        chrome_auto_authorize: bool = False,
        chrome_authorize_minutes: int = 30,
        chrome_binary: str | None = None,
        chrome_isolation: str = CHROME_ISOLATION_PER_RUN,
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
        self.warm_sessions_enabled = warm_sessions_enabled
        self.session_idle_seconds = max(0, session_idle_seconds)
        self.max_warm_sessions = max(
            1, max_warm_sessions if max_warm_sessions is not None else self.max_concurrent
        )
        self.supports_images = supports_images
        self.node_bin_dir = node_bin_dir
        self.connector_extension = connector_extension
        self.connector_broker_url = (connector_broker_url or backend_url).rstrip("/")
        self.chrome_extension = chrome_extension
        self.chrome_auto_authorize = chrome_auto_authorize
        self.chrome_authorize_minutes = chrome_authorize_minutes
        self.chrome_binary = chrome_binary
        self.chrome_isolation = chrome_isolation.strip().lower()
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self._bridge_ports: set[int] = set()
        self._bridge_ports_lock = threading.Lock()
        self._sessions: dict[str, _WarmSession] = {}
        self._sessions_lock = threading.RLock()

    def _resolved_binary(self) -> str | None:
        if os.path.sep in self.binary:
            path = Path(self.binary).expanduser()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(self.binary)

    def _resolved_chrome_extension(self) -> str | None:
        if not self.chrome_extension:
            return None
        path = Path(self.chrome_extension).expanduser()
        return str(path.resolve()) if path.is_file() else None

    def _resolved_connector_extension(self) -> str | None:
        if not self.connector_extension:
            return None
        path = Path(self.connector_extension).expanduser()
        return str(path.resolve()) if path.is_file() else None

    def _resolved_chrome_companion(self) -> Path | None:
        extension = self._resolved_chrome_extension()
        if not extension:
            return None
        companion = Path(extension).parent / "browser-extension"
        required = (companion / "manifest.json", companion / "service_worker.js")
        return companion.resolve() if all(path.is_file() for path in required) else None

    def _resolved_chrome_binary(self) -> str | None:
        if self.chrome_binary:
            configured = Path(self.chrome_binary).expanduser()
            if os.path.sep in self.chrome_binary:
                resolved = (
                    str(configured.resolve())
                    if configured.is_file() and os.access(configured, os.X_OK)
                    else None
                )
            else:
                resolved = shutil.which(self.chrome_binary)
            return resolved if resolved and self._supports_unpacked_extensions(resolved) else None

        candidates = (
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
        for candidate in candidates:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        for name in ("chromium", "chromium-browser"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
        return None

    @staticmethod
    def _supports_unpacked_extensions(binary: str) -> bool:
        """Chrome estable >=137 ignora --load-extension; CfT y Chromium lo admiten."""
        normalized = binary.lower().replace("\\", "/")
        if "google chrome for testing" in normalized:
            return True
        return not (
            "/google chrome.app/" in normalized
            or normalized.endswith("/google-chrome")
            or normalized.endswith("/google-chrome-stable")
        )

    def _browser_available(self) -> bool:
        return bool(
            self.chrome_isolation == CHROME_ISOLATION_PER_RUN
            and self._resolved_chrome_companion()
            and self._resolved_chrome_binary()
        )

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
            "image_input": self.supports_images,
            "max_concurrent": self.max_concurrent,
            "node_available": bool(shutil.which("node", path=runtime_path)),
            "browser_available": self._browser_available(),
            "browser_auto_authorize": self.chrome_auto_authorize,
            "browser_isolation": self.chrome_isolation,
            "browser_profile_scope": "ephemeral_run",
            "connectors_available": bool(self._resolved_connector_extension()),
            "connector_tool_loading": "dynamic",
            "connector_auth_scope": "ephemeral_run",
            "conversation_sessions": (
                "warm_per_user_bot" if self.warm_sessions_enabled else "one_shot"
            ),
            "warm_session_limit": self.max_warm_sessions,
            "warm_session_idle_seconds": self.session_idle_seconds,
        }

    def run(
        self,
        *,
        run_id: str,
        run_api_key: str,
        prompt: str,
        browser: bool = False,
        connector_run_token: str | None = None,
        conversation_key: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> PiRunResult:
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
        if browser and (not self._browser_available() or not self.chrome_auto_authorize):
            raise PiHarnessError(
                "Chrome requiere aislamiento per_run, Chrome for Testing/Chromium, "
                "pi-chrome instalado y PI_CHROME_AUTO_AUTHORIZE=1"
            )
        if connector_run_token and not self._resolved_connector_extension():
            raise PiHarnessError("La extension de conectores no esta instalada")
        if not self._slots.acquire(blocking=False):
            raise PiHarnessBusy("Todos los slots de Pi estan ocupados")
        try:
            if conversation_key and not browser and self.warm_sessions_enabled:
                return self._run_persistent(
                    run_id=run_id,
                    run_api_key=run_api_key,
                    prompt=prompt,
                    connector_run_token=connector_run_token,
                    conversation_key=conversation_key,
                    on_text_delta=on_text_delta,
                )
            return self._run(
                run_id=run_id,
                run_api_key=run_api_key,
                prompt=prompt,
                browser=browser,
                connector_run_token=connector_run_token,
                on_text_delta=on_text_delta,
            )
        finally:
            self._slots.release()

    def prewarm(self, *, conversation_key: str, timeout_seconds: float = 25.0) -> dict[str, Any]:
        """Start one isolated RPC session and wait until it accepts commands.

        This does not call a model or create conversation history. The runtime
        credential file intentionally remains empty until an actual run writes
        its one-time token.
        """
        if not self.enabled:
            raise PiHarnessError("El harness de Pi esta desactivado (PI_ENABLED=0)")
        if not self.warm_sessions_enabled:
            raise PiHarnessError("Las sesiones cálidas de Pi están desactivadas")
        if not conversation_key:
            raise PiHarnessError("La sesión cálida requiere una identidad de bot")
        if not self._resolved_binary():
            raise PiHarnessError(f"No se encontro el ejecutable de Pi: {self.binary}")
        if not self._slots.acquire(blocking=False):
            raise PiHarnessBusy("Todos los slots de Pi estan ocupados")

        session: _WarmSession | None = None
        fatal = False
        started_new = False
        started_at = time.monotonic()
        try:
            session = self._acquire_warm_session(conversation_key)
            if session.process is None or session.process.poll() is not None:
                self._stop_warm_session(session)
                self._start_warm_session(
                    session,
                    run_api_key="",
                    connector_run_token=None,
                )
                started_new = True

            process = session.process
            events = session.events
            assert process is not None and process.stdin is not None and events is not None
            request_id = f"prewarm-{time.time_ns()}"
            process.stdin.write(json.dumps({"id": request_id, "type": "get_state"}) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + max(1.0, timeout_seconds)
            while time.monotonic() < deadline:
                try:
                    remaining = max(0.01, deadline - time.monotonic())
                    line = events.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "extension_ui_request":
                    process.stdin.write(json.dumps({
                        "type": "extension_ui_response",
                        "id": event.get("id"),
                        "cancelled": True,
                    }) + "\n")
                    process.stdin.flush()
                    continue
                if event.get("type") == "response" and event.get("id") == request_id:
                    if not event.get("success"):
                        fatal = True
                        raise PiHarnessError(str(event.get("error") or "Pi no aceptó el precalentamiento"))
                    session.last_used = time.monotonic()
                    return {
                        "ready": True,
                        "started": started_new,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
                    }
            fatal = True
            raise PiHarnessTimeout("Pi no estuvo listo antes del timeout de precalentamiento")
        except (BrokenPipeError, OSError) as exc:
            fatal = True
            raise PiHarnessError("La sesión de Pi se cerró durante el precalentamiento") from exc
        finally:
            if session is not None:
                try:
                    self._atomic_runtime_auth(session.auth_file)
                except OSError:
                    fatal = True
                session.last_used = time.monotonic()
                if fatal:
                    self._remove_warm_session(session)
                session.lock.release()
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
                            "input": ["text", "image"] if self.supports_images else ["text"],
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
                                "supportsReasoningEffort": True,
                                "maxTokensField": "max_tokens",
                                "supportsLongCacheRetention": False,
                                "requiresReasoningContentOnAssistantMessages": True,
                                "thinkingFormat": "deepseek",
                            },
                        }
                    ],
                }
            }
        }
        (config_dir / "models.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def _child_env(
        self,
        run_dir: Path,
        config_dir: Path,
        run_api_key: str,
        chrome_bridge_port: int | None = None,
        connector_run_token: str | None = None,
        runtime_auth_file: Path | None = None,
    ) -> dict[str, str]:
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
            API_KEY_ENV: run_api_key,
        }
        if chrome_bridge_port is not None:
            env["PI_CHROME_BRIDGE_HOST"] = "127.0.0.1"
            env["PI_CHROME_BRIDGE_PORT"] = str(chrome_bridge_port)
        if connector_run_token is not None:
            env["PI_CONNECTOR_BROKER_URL"] = self.connector_broker_url
            env["PI_CONNECTOR_RUN_TOKEN"] = connector_run_token
        if runtime_auth_file is not None:
            env["PI_RUNTIME_AUTH_FILE"] = str(runtime_auth_file)
        return env

    def _reserve_bridge_port(self) -> int:
        with self._bridge_ports_lock:
            for _ in range(20):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = int(probe.getsockname()[1])
                if port not in self._bridge_ports:
                    self._bridge_ports.add(port)
                    return port
        raise PiHarnessError("No se pudo reservar un puerto local aislado para pi-chrome")

    def _release_bridge_port(self, port: int | None) -> None:
        if port is None:
            return
        with self._bridge_ports_lock:
            self._bridge_ports.discard(port)

    def _prepare_chrome_companion(self, run_dir: Path, port: int) -> Path:
        source = self._resolved_chrome_companion()
        if not source:
            raise PiHarnessError("La extension companion de pi-chrome no esta instalada")
        target = run_dir / "chrome-extension"
        shutil.copytree(source, target)

        worker_path = target / "service_worker.js"
        worker = worker_path.read_text(encoding="utf-8")
        if CHROME_BRIDGE_URL not in worker:
            raise PiHarnessError(
                "La version de pi-chrome cambio el bridge; se rechazo una configuracion potencialmente compartida"
            )
        worker_path.write_text(
            worker.replace(CHROME_BRIDGE_URL, f"http://127.0.0.1:{port}"),
            encoding="utf-8",
        )

        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        permissions = manifest.get("host_permissions")
        if not isinstance(permissions, list) or f"{CHROME_BRIDGE_URL}/*" not in permissions:
            raise PiHarnessError(
                "La version de pi-chrome no declara el bridge esperado; se rechazo por seguridad"
            )
        manifest["host_permissions"] = [
            f"http://127.0.0.1:{port}/*" if value == f"{CHROME_BRIDGE_URL}/*" else value
            for value in permissions
        ]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return target

    def _chrome_env(self, run_dir: Path) -> dict[str, str]:
        # Chrome tampoco debe heredar los secretos del proceso del backend.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(run_dir / "home"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        for name in (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "__CF_USER_TEXT_ENCODING",
        ):
            if os.environ.get(name):
                env[name] = os.environ[name]
        return env

    def _chrome_command(self, run_dir: Path, companion: Path) -> list[str]:
        binary = self._resolved_chrome_binary()
        if not binary:
            raise PiHarnessError(
                "No se encontro Chrome for Testing/Chromium compatible; configura PI_CHROME_BIN"
            )
        profile = run_dir / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=False)
        return [
            binary,
            f"--user-data-dir={profile}",
            f"--load-extension={companion}",
            f"--disable-extensions-except={companion}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--password-store=basic",
            "about:blank",
        ]

    @staticmethod
    def _stop_process(process: subprocess.Popen[Any]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            if process.poll() is None:
                process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass

    @staticmethod
    def _remove_chrome_profile(run_dir: Path) -> None:
        # El perfil siempre se crea dentro del run_id aleatorio y nunca se reutiliza.
        shutil.rmtree(run_dir / "chrome-profile", ignore_errors=True)

    def _command(self, browser: bool, *, session_id: str | None = None) -> list[str]:
        command = [
            self._resolved_binary() or self.binary,
            "--mode", "rpc",
            "--provider", PROVIDER_NAME,
            "--model", self.model,
            "--thinking", self.thinking,
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-approve",
            "--offline",
        ]
        if session_id is None:
            command.append("--no-session")
        else:
            command.extend(["--session-id", session_id])
            if RUNTIME_AUTH_EXTENSION.is_file():
                command.extend(["--extension", str(RUNTIME_AUTH_EXTENSION.resolve())])
        connector_extension = self._resolved_connector_extension()
        if connector_extension:
            command.extend(["--extension", connector_extension])
        chrome_extension = self._resolved_chrome_extension()
        if browser and chrome_extension:
            command.extend(["--extension", chrome_extension])
        return command

    @staticmethod
    def _atomic_runtime_auth(
        path: Path,
        *,
        run_api_key: str = "",
        connector_run_token: str = "",
    ) -> None:
        payload = json.dumps(
            {
                "run_api_key": run_api_key,
                "connector_run_token": connector_run_token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _session_id(key: str) -> str:
        return (
            f"{key[:8]}-{key[8:12]}-{key[12:16]}-"
            f"{key[16:20]}-{key[20:32]}"
        )

    def _new_warm_session(self, conversation_key: str) -> _WarmSession:
        owner_id = conversation_key.split("\0", 1)[0]
        owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        key = hashlib.sha256(conversation_key.encode("utf-8")).hexdigest()
        root = self.runs_dir / "sessions" / owner_key / key
        return _WarmSession(
            key=key,
            owner_key=owner_key,
            session_id=self._session_id(key),
            root=root,
            config_dir=root / "config",
            work_dir=root / "workspace",
            auth_file=root / "config" / "runtime-auth.json",
            lock=threading.Lock(),
            last_used=time.monotonic(),
        )

    def _stop_warm_session(self, session: _WarmSession) -> None:
        process = session.process
        session.process = None
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            if process.poll() is None:
                self._stop_process(process)
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
        if session.reader is not None:
            session.reader.join(timeout=1)
        session.reader = None
        session.events = None
        if session.stderr_stream is not None:
            try:
                session.stderr_stream.close()
            except OSError:
                pass
        session.stderr_stream = None
        try:
            self._atomic_runtime_auth(session.auth_file)
        except OSError:
            pass

    def _remove_warm_session(self, session: _WarmSession) -> None:
        with self._sessions_lock:
            if self._sessions.get(session.key) is session:
                self._sessions.pop(session.key, None)
        self._stop_warm_session(session)

    def _prune_warm_sessions(self, *, reserve: int = 0) -> None:
        now = time.monotonic()
        victims: list[_WarmSession] = []
        with self._sessions_lock:
            candidates = sorted(self._sessions.values(), key=lambda item: item.last_used)
            for candidate in candidates:
                process_dead = candidate.process is not None and candidate.process.poll() is not None
                expired = bool(
                    self.session_idle_seconds
                    and now - candidate.last_used >= self.session_idle_seconds
                )
                over_limit = len(self._sessions) - len(victims) + reserve > self.max_warm_sessions
                if not (process_dead or expired or over_limit):
                    continue
                if not candidate.lock.acquire(blocking=False):
                    continue
                if self._sessions.get(candidate.key) is candidate:
                    self._sessions.pop(candidate.key, None)
                    victims.append(candidate)
                else:
                    candidate.lock.release()
        for victim in victims:
            try:
                self._stop_warm_session(victim)
            finally:
                victim.lock.release()

    def _acquire_warm_session(self, conversation_key: str) -> _WarmSession:
        candidate = self._new_warm_session(conversation_key)
        self._prune_warm_sessions()
        with self._sessions_lock:
            session = self._sessions.get(candidate.key)
            if session is None:
                self._prune_warm_sessions(reserve=1)
                if len(self._sessions) >= self.max_warm_sessions:
                    raise PiHarnessBusy("No hay una sesión cálida disponible para este bot")
                session = candidate
                self._sessions[session.key] = session
        if not session.lock.acquire(blocking=False):
            raise PiHarnessBusy("Este bot ya está ejecutando otra tarea")
        return session

    def _start_warm_session(
        self,
        session: _WarmSession,
        *,
        run_api_key: str,
        connector_run_token: str | None,
    ) -> None:
        resolved_binary = self._resolved_binary()
        if not resolved_binary or Path(resolved_binary).name not in {
            "pi-render-safe",
            "fake_pi.py",
        }:
            raise PiHarnessError(
                "Las sesiones cálidas requieren el launcher sin tools locales de Render"
            )
        if not RUNTIME_AUTH_EXTENSION.is_file():
            raise PiHarnessError("La extensión de autenticación dinámica de Pi no está instalada")
        for path in (
            session.config_dir / "sessions",
            session.work_dir,
            session.root / "home",
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
        audit = session.root / "sandbox-audit.json"
        if audit.is_file():
            audit.replace(
                session.root / f"sandbox-audit-{int(time.time() * 1000)}.json"
            )
        shutil.rmtree(session.root / ".sandbox-runtime", ignore_errors=True)
        self._write_config(session.config_dir)
        self._atomic_runtime_auth(
            session.auth_file,
            run_api_key=run_api_key,
            connector_run_token=connector_run_token or "",
        )
        stderr_path = session.root / "session-stderr.log"
        session.stderr_stream = stderr_path.open("a", encoding="utf-8")
        env = self._child_env(
            session.root,
            session.config_dir,
            "runtime-auth-file",
            connector_run_token="runtime-auth-file",
            runtime_auth_file=session.auth_file,
        )
        try:
            process = subprocess.Popen(
                self._command(False, session_id=session.session_id),
                cwd=session.work_dir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=session.stderr_stream,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except Exception:
            session.stderr_stream.close()
            session.stderr_stream = None
            raise
        session.process = process
        session.events = queue.Queue()
        assert process.stdout is not None

        def read_events() -> None:
            try:
                for line in process.stdout:
                    assert session.events is not None
                    session.events.put(line)
            finally:
                if session.events is not None:
                    session.events.put(None)

        session.reader = threading.Thread(target=read_events, daemon=True)
        session.reader.start()

    @staticmethod
    def _copy_session_stderr(session: _WarmSession, target: Path, start: int) -> None:
        source = session.root / "session-stderr.log"
        try:
            data = source.read_bytes()[start:]
        except OSError:
            data = b""
        target.write_bytes(data)

    def _run_persistent(
        self,
        *,
        run_id: str,
        run_api_key: str,
        prompt: str,
        connector_run_token: str | None,
        conversation_key: str,
        on_text_delta: Callable[[str], None] | None,
    ) -> PiRunResult:
        session = self._acquire_warm_session(conversation_key)
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        event_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"
        started = time.monotonic()
        deadline = started + self.timeout_seconds if self.timeout_seconds > 0 else None
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_read_tokens": 0,
            "cached_write_tokens": 0,
        }
        answer = ""
        agent_error = ""
        settled = False
        fatal = False
        stderr_start = 0
        try:
            if session.process is None or session.process.poll() is not None:
                self._stop_warm_session(session)
                self._start_warm_session(
                    session,
                    run_api_key=run_api_key,
                    connector_run_token=connector_run_token,
                )
            else:
                self._atomic_runtime_auth(
                    session.auth_file,
                    run_api_key=run_api_key,
                    connector_run_token=connector_run_token or "",
                )
            process = session.process
            events = session.events
            assert process is not None and process.stdin is not None and events is not None
            session.stderr_stream.flush()
            try:
                stderr_start = (session.root / "session-stderr.log").stat().st_size
            except OSError:
                stderr_start = 0

            def send(payload: dict[str, Any]) -> None:
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()

            send({"id": f"agent-task-{run_id}", "type": "prompt", "message": prompt})
            with event_path.open("w", encoding="utf-8") as event_log:
                while deadline is None or time.monotonic() < deadline:
                    wait = 0.5 if deadline is None else min(
                        0.5, max(0.0, deadline - time.monotonic())
                    )
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
                        send({
                            "type": "extension_ui_response",
                            "id": event.get("id"),
                            "cancelled": True,
                        })
                    if event.get("type") == "message_update":
                        update = event.get("assistantMessageEvent") or {}
                        if update.get("type") == "text_delta":
                            delta = update.get("delta")
                            if isinstance(delta, str) and delta and on_text_delta:
                                on_text_delta(delta)
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
                            usage["input_tokens"] += int(
                                raw_usage.get("input") or raw_usage.get("input_tokens") or 0
                            )
                            usage["output_tokens"] += int(
                                raw_usage.get("output") or raw_usage.get("output_tokens") or 0
                            )
                            usage["cached_read_tokens"] += int(
                                raw_usage.get("cacheRead")
                                or raw_usage.get("cached_read_tokens")
                                or 0
                            )
                            usage["cached_write_tokens"] += int(
                                raw_usage.get("cacheWrite")
                                or raw_usage.get("cached_write_tokens")
                                or 0
                            )
                    if event.get("type") == "agent_settled":
                        settled = True
                        break
            if not settled:
                fatal = True
                if deadline is not None and time.monotonic() >= deadline:
                    try:
                        send({"type": "abort"})
                    except (BrokenPipeError, OSError):
                        pass
                    raise PiHarnessTimeout(
                        f"Pi excedio el timeout de {self.timeout_seconds}s"
                    )
                raise PiHarnessError(
                    f"Pi termino antes de completar la tarea (exit={process.poll()})"
                )
            if agent_error:
                raise PiHarnessUsageError(f"Pi no pudo completar la tarea: {agent_error}")
            if not answer:
                raise PiHarnessError("Pi completo la ejecucion sin una respuesta final")
            session.last_used = time.monotonic()
            return PiRunResult(
                run_id=run_id,
                answer=answer,
                model=self.model,
                duration_seconds=round(time.monotonic() - started, 3),
                usage=usage,
                browser=False,
                event_log=str(event_path),
                stderr_log=str(stderr_path),
            )
        except (BrokenPipeError, OSError):
            fatal = True
            raise PiHarnessError("La sesión de Pi se cerró inesperadamente")
        finally:
            if session.stderr_stream is not None:
                session.stderr_stream.flush()
            self._copy_session_stderr(session, stderr_path, stderr_start)
            try:
                self._atomic_runtime_auth(session.auth_file)
            except OSError:
                fatal = True
            session.last_used = time.monotonic()
            if fatal:
                self._remove_warm_session(session)
            session.lock.release()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            process = session.process
            if process is not None and process.poll() is None:
                self._stop_process(process)
            session.lock.acquire()
            try:
                self._stop_warm_session(session)
            finally:
                session.lock.release()

    def forget_user(self, user_id: str) -> int:
        owner_key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        with self._sessions_lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.owner_key == owner_key
            ]
            for session in sessions:
                self._sessions.pop(session.key, None)
        for session in sessions:
            process = session.process
            if process is not None and process.poll() is None:
                self._stop_process(process)
            session.lock.acquire()
            try:
                self._stop_warm_session(session)
                shutil.rmtree(session.root, ignore_errors=True)
            finally:
                session.lock.release()
        shutil.rmtree(self.runs_dir / "sessions" / owner_key, ignore_errors=True)
        return len(sessions)

    def _run(
        self,
        *,
        run_id: str,
        run_api_key: str,
        prompt: str,
        browser: bool,
        connector_run_token: str | None,
        on_text_delta: Callable[[str], None] | None,
    ) -> PiRunResult:
        run_dir = self.runs_dir / run_id
        config_dir = run_dir / "config"
        work_dir = run_dir / "workspace"
        for path in (config_dir / "sessions", work_dir, run_dir / "home"):
            path.mkdir(parents=True, exist_ok=True)
        self._write_config(config_dir)
        event_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"
        chrome_stderr_path = run_dir / "chrome-stderr.log"
        started = time.monotonic()
        deadline = started + self.timeout_seconds if self.timeout_seconds > 0 else None
        usage = {"input_tokens": 0, "output_tokens": 0, "cached_read_tokens": 0,
                 "cached_write_tokens": 0}
        answer = ""
        agent_error = ""
        settled = False
        bridge_port: int | None = None
        chrome_process: subprocess.Popen[Any] | None = None

        with (
            event_path.open("w", encoding="utf-8") as event_log,
            stderr_path.open("w", encoding="utf-8") as stderr_log,
            chrome_stderr_path.open("w", encoding="utf-8") as chrome_stderr_log,
        ):
            try:
                if browser:
                    bridge_port = self._reserve_bridge_port()
                    companion = self._prepare_chrome_companion(run_dir, bridge_port)
                    chrome_process = subprocess.Popen(
                        self._chrome_command(run_dir, companion),
                        cwd=work_dir,
                        env=self._chrome_env(run_dir),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=chrome_stderr_log,
                        start_new_session=True,
                    )
                    # Give Chromium enough time to enter its process before Pi
                    # asks the bridge to authorize. This only affects explicit
                    # browser runs and avoids a startup race on busy hosts.
                    time.sleep(0.1)
                process = subprocess.Popen(
                    self._command(browser),
                    cwd=work_dir,
                    env=self._child_env(
                        run_dir,
                        config_dir,
                        run_api_key,
                        chrome_bridge_port=bridge_port,
                        connector_run_token=connector_run_token,
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_log,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception:
                if chrome_process is not None:
                    self._stop_process(chrome_process)
                self._remove_chrome_profile(run_dir)
                self._release_bridge_port(bridge_port)
                raise
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
            try:
                if browser:
                    send({
                        "id": "chrome-authorize",
                        "type": "prompt",
                        "message": f"/chrome authorize {self.chrome_authorize_minutes}m",
                    })
                else:
                    send({"id": "agent-task", "type": "prompt", "message": prompt})
            except Exception:
                self._stop_process(process)
                if chrome_process is not None:
                    self._stop_process(chrome_process)
                self._remove_chrome_profile(run_dir)
                self._release_bridge_port(bridge_port)
                raise

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

                    if event.get("type") == "message_update":
                        update = event.get("assistantMessageEvent") or {}
                        if update.get("type") == "text_delta":
                            delta = update.get("delta")
                            if isinstance(delta, str) and delta and on_text_delta:
                                on_text_delta(delta)

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
                        raise PiHarnessTimeout(
                            f"Pi excedio el timeout de {self.timeout_seconds}s"
                        )
                    raise PiHarnessError(
                        f"Pi termino antes de completar la tarea (exit={process.poll()})"
                    )
                if agent_error:
                    raise PiHarnessUsageError(
                        f"Pi no pudo completar la tarea: {agent_error}"
                    )
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
                        self._stop_process(process)
                reader.join(timeout=1)
                process.stdout.close()
                if chrome_process is not None:
                    self._stop_process(chrome_process)
                self._remove_chrome_profile(run_dir)
                self._release_bridge_port(bridge_port)

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
