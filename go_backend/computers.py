"""Computadoras persistentes y aisladas por bot.

El proveedor nunca recibe credenciales de usuario de Agent Genia. El backend
conserva la relación (usuario, bot) -> sandbox y entrega a Electron únicamente
previews firmados de corta duración. Pi opera la misma sandbox mediante grants
efímeros emitidos para una sola ejecución.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .store import ComputerLimitReached


MAX_COMPUTER_ARGUMENTS_BYTES = 64 * 1024
MAX_COMPUTER_RESULT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_SCREENSHOT_BYTES = 1_400_000


class ComputerError(RuntimeError):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ComputerConfig:
    enabled: bool = False
    api_key: str = ""
    api_url: str = ""
    target: str = ""
    snapshot: str = ""
    auto_stop_minutes: int = 15
    auto_archive_minutes: int = 1440
    preview_ttl_seconds: int = 3600
    vnc_port: int = 6080
    vnc_resolution: str = "1440x900"
    basic_limit: int = 1
    pro_limit: int = 3

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def validate(self) -> None:
        if self.enabled and not self.api_key:
            raise ValueError("COMPUTERS_ENABLED=1 requiere DAYTONA_API_KEY")
        if not 1 <= self.auto_stop_minutes <= 1440:
            raise ValueError("COMPUTER_AUTO_STOP_MINUTES debe estar entre 1 y 1440")
        if not 0 <= self.auto_archive_minutes <= 525600:
            raise ValueError("COMPUTER_AUTO_ARCHIVE_MINUTES debe estar entre 0 y 525600")
        if not 60 <= self.preview_ttl_seconds <= 86400:
            raise ValueError("COMPUTER_PREVIEW_TTL_SECONDS debe estar entre 60 y 86400")
        if not 1 <= self.vnc_port <= 65535:
            raise ValueError("COMPUTER_VNC_PORT no es válido")
        match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", self.vnc_resolution)
        if not match or not 640 <= int(match.group(1)) <= 3840 or not 480 <= int(match.group(2)) <= 2160:
            raise ValueError("COMPUTER_VNC_RESOLUTION no es válida")
        if not 1 <= self.basic_limit <= 100 or not 1 <= self.pro_limit <= 100:
            raise ValueError("Los límites de computadoras deben estar entre 1 y 100")
        if self.pro_limit < self.basic_limit:
            raise ValueError("COMPUTER_PRO_LIMIT no puede ser menor que COMPUTER_BASIC_LIMIT")


class ComputerProvider(Protocol):
    name: str

    def create(
        self, *, user_id: str, bot_id: str, bot_name: str, include_viewer: bool = False
    ) -> dict[str, Any]: ...
    def inspect(self, provider_ref: str, *, include_viewer: bool = False) -> dict[str, Any]: ...
    def ensure(self, provider_ref: str, *, include_viewer: bool = False) -> dict[str, Any]: ...
    def stop(self, provider_ref: str) -> dict[str, Any]: ...
    def delete(self, provider_ref: str) -> None: ...
    def delete_identity(self, *, user_id: str, bot_id: str) -> None: ...
    def execute(self, provider_ref: str, operation: str, arguments: dict[str, Any]) -> Any: ...


class DaytonaComputerProvider:
    """Adaptador del SDK oficial. Se importa solo cuando la función está activa."""

    name = "daytona"

    def __init__(self, config: ComputerConfig):
        config.validate()
        try:
            from daytona import Daytona, DaytonaConfig
        except ImportError as exc:  # pragma: no cover - guard de despliegue
            raise RuntimeError(
                "COMPUTERS_ENABLED requiere daytona==0.198.0 de requirements.txt"
            ) from exc
        options: dict[str, Any] = {"api_key": config.api_key}
        if config.api_url:
            options["api_url"] = config.api_url
        if config.target:
            options["target"] = config.target
        self.config = config
        self.client = Daytona(DaytonaConfig(**options))

    def create(
        self, *, user_id: str, bot_id: str, bot_name: str, include_viewer: bool = False
    ) -> dict[str, Any]:
        from daytona import CreateSandboxFromSnapshotParams

        sandbox_name = self._sandbox_name(user_id, bot_id)
        params = CreateSandboxFromSnapshotParams(
            name=sandbox_name,
            snapshot=self.config.snapshot or None,
            language="python",
            public=False,
            auto_stop_interval=self.config.auto_stop_minutes,
            auto_archive_interval=self.config.auto_archive_minutes,
            auto_delete_interval=-1,
            labels={
                "agentgenia": "computer",
                "agentgenia-user": user_id[:80],
                "agentgenia-bot": bot_id[:100],
            },
            env_vars={"VNC_RESOLUTION": self.config.vnc_resolution},
        )
        try:
            sandbox = self.client.create(params, timeout=120)
        except Exception as create_error:
            # El nombre es determinista por (usuario, bot). Si dos instancias del
            # backend provisionan a la vez, una recupera la sandbox de la otra en
            # vez de dejar una segunda máquina facturable sin referencia.
            sandbox = None
            for _attempt in range(5):
                try:
                    sandbox = self.client.get(sandbox_name, request_timeout=30)
                    break
                except Exception:
                    time.sleep(1)
            if sandbox is None:
                raise create_error
        self._start_computer_use(sandbox)
        return self._snapshot(sandbox, include_viewer=include_viewer)

    def inspect(self, provider_ref: str, *, include_viewer: bool = False) -> dict[str, Any]:
        sandbox = self.client.get(provider_ref, request_timeout=30)
        return self._snapshot(sandbox, include_viewer=include_viewer)

    def ensure(self, provider_ref: str, *, include_viewer: bool = False) -> dict[str, Any]:
        sandbox = self.client.get(provider_ref, request_timeout=30)
        state = self._state_value(sandbox)
        if state not in {"started", "running"}:
            sandbox.start(timeout=120)
        self._start_computer_use(sandbox)
        return self._snapshot(sandbox, include_viewer=include_viewer)

    def stop(self, provider_ref: str) -> dict[str, Any]:
        sandbox = self.client.get(provider_ref, request_timeout=30)
        state = self._state_value(sandbox)
        if state in {"started", "running", "starting"}:
            sandbox.stop(timeout=120)
        return self._snapshot(sandbox)

    def delete(self, provider_ref: str) -> None:
        sandbox = self.client.get(provider_ref, request_timeout=30)
        self.client.delete(sandbox, timeout=120, wait=True)

    def delete_identity(self, *, user_id: str, bot_id: str) -> None:
        try:
            sandbox = self.client.get(self._sandbox_name(user_id, bot_id), request_timeout=30)
        except Exception as exc:
            if _provider_not_found(exc):
                return
            raise
        self.client.delete(sandbox, timeout=120, wait=True)

    def execute(self, provider_ref: str, operation: str, arguments: dict[str, Any]) -> Any:
        sandbox = self.client.get(provider_ref, request_timeout=30)
        if self._state_value(sandbox) not in {"started", "running"}:
            sandbox.start(timeout=120)
        self._start_computer_use(sandbox)
        computer = sandbox.computer_use

        if operation == "status":
            return self._snapshot(sandbox)
        if operation == "screenshot":
            from daytona import ScreenshotOptions

            response = computer.screenshot.take_compressed(
                ScreenshotOptions(format="jpeg", quality=75, scale=0.8, show_cursor=True),
                request_timeout=30,
            )
            image = response.screenshot or ""
            if not image:
                raise ComputerError(502, "La computadora no devolvió una captura", "computer_screenshot_failed")
            try:
                decoded_size = len(base64.b64decode(image, validate=True))
            except (binascii.Error, ValueError, TypeError) as exc:
                raise ComputerError(502, "La captura no es una imagen válida", "computer_screenshot_failed") from exc
            size_bytes = response.size_bytes or decoded_size
            if size_bytes > MAX_SCREENSHOT_BYTES or len(image) > (MAX_SCREENSHOT_BYTES * 4 // 3) + 8:
                raise ComputerError(502, "La captura excede el límite seguro", "computer_result_too_large")
            return {
                "image_base64": image,
                "mime_type": "image/jpeg",
                "size_bytes": size_bytes,
            }
        if operation == "click":
            double = arguments.get("double", False)
            if not isinstance(double, bool):
                raise ComputerError(400, "double debe ser true o false", "bad_computer_arguments")
            result = computer.mouse.click(
                _int_arg(arguments, "x", 0, 10000),
                _int_arg(arguments, "y", 0, 10000),
                _choice_arg(arguments, "button", {"left", "right", "middle"}, "left"),
                double,
                request_timeout=30,
            )
            return _model_dict(result)
        if operation == "move":
            result = computer.mouse.move(
                _int_arg(arguments, "x", 0, 10000),
                _int_arg(arguments, "y", 0, 10000),
                request_timeout=30,
            )
            return _model_dict(result)
        if operation == "drag":
            result = computer.mouse.drag(
                _int_arg(arguments, "start_x", 0, 10000),
                _int_arg(arguments, "start_y", 0, 10000),
                _int_arg(arguments, "end_x", 0, 10000),
                _int_arg(arguments, "end_y", 0, 10000),
                _choice_arg(arguments, "button", {"left", "right", "middle"}, "left"),
                request_timeout=30,
            )
            return _model_dict(result)
        if operation == "scroll":
            return {
                "success": computer.mouse.scroll(
                    _int_arg(arguments, "x", 0, 10000),
                    _int_arg(arguments, "y", 0, 10000),
                    _choice_arg(arguments, "direction", {"up", "down"}, "down"),
                    _int_arg(arguments, "amount", 1, 100, default=3),
                    request_timeout=30,
                )
            }
        if operation == "type":
            text = _text_arg(arguments, "text", 20000)
            computer.keyboard.type(
                text,
                _int_arg(arguments, "delay", 0, 1000, default=0),
                request_timeout=60,
            )
            return {"typed": len(text)}
        if operation == "key":
            key = _text_arg(arguments, "key", 40)
            modifiers = arguments.get("modifiers", [])
            if not isinstance(modifiers, list) or len(modifiers) > 4 or not all(isinstance(item, str) for item in modifiers):
                raise ComputerError(400, "modifiers debe ser una lista corta", "bad_computer_arguments")
            computer.keyboard.press(key, modifiers, request_timeout=30)
            return {"pressed": key, "modifiers": modifiers}
        if operation == "hotkey":
            keys = _text_arg(arguments, "keys", 80)
            computer.keyboard.hotkey(keys, request_timeout=30)
            return {"pressed": keys}
        if operation == "shell":
            command = _text_arg(arguments, "command", 4000)
            timeout = _int_arg(arguments, "timeout", 1, 120, default=30)
            response = sandbox.process.exec(
                command,
                cwd=_optional_text_arg(arguments, "cwd", 1000),
                timeout=timeout,
            )
            output = (response.result or "").encode("utf-8", errors="replace")[:MAX_TEXT_BYTES].decode(
                "utf-8", errors="replace"
            )
            return {"exit_code": response.exit_code, "output": output}
        if operation == "list_files":
            path = _optional_text_arg(arguments, "path", 1000) or "workspace"
            depth = _int_arg(arguments, "depth", 1, 3, default=1)
            return [_model_dict(item) for item in sandbox.fs.list_files(path, depth=depth, request_timeout=30)[:500]]
        if operation == "read_file":
            path = _text_arg(arguments, "path", 1000)
            info = _model_dict(sandbox.fs.get_file_info(path, request_timeout=30))
            if isinstance(info, dict) and int(info.get("size") or 0) > MAX_FILE_BYTES:
                raise ComputerError(413, "El archivo excede 64 KiB", "computer_file_too_large")
            content = sandbox.fs.download_file(path, 30)
            if not isinstance(content, bytes):
                raise ComputerError(502, "No se pudo leer el archivo", "computer_file_error")
            if len(content) > MAX_FILE_BYTES:
                raise ComputerError(413, "El archivo excede 64 KiB", "computer_file_too_large")
            try:
                return {"text": content.decode("utf-8"), "encoding": "utf-8"}
            except UnicodeDecodeError:
                return {"data_base64": base64.b64encode(content).decode(), "encoding": "base64"}
        if operation == "write_file":
            path = _text_arg(arguments, "path", 1000)
            content = _text_arg(arguments, "content", MAX_FILE_BYTES)
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_FILE_BYTES:
                raise ComputerError(413, "El contenido excede 64 KiB", "computer_file_too_large")
            sandbox.fs.upload_file(encoded, path, 30)
            return {"path": path, "bytes": len(encoded)}
        raise ComputerError(400, "Operación de computadora no permitida", "bad_computer_operation")

    def _start_computer_use(self, sandbox: Any) -> None:
        try:
            status = _model_dict(sandbox.computer_use.get_status(request_timeout=20))
            if str(status.get("status", "")).lower() in {"running", "started", "ready"}:
                return
        except Exception:
            pass
        sandbox.computer_use.start(request_timeout=120)

    def _snapshot(self, sandbox: Any, *, include_viewer: bool = False) -> dict[str, Any]:
        provider_state = self._state_value(sandbox)
        state = _public_state(provider_state)
        viewer_url = ""
        viewer_expires_at = 0
        if include_viewer and state == "running":
            preview = sandbox.create_signed_preview_url(
                self.config.vnc_port,
                expires_in_seconds=self.config.preview_ttl_seconds,
                request_timeout=30,
            )
            viewer_url = str(preview.url or "")
            viewer_expires_at = int(time.time()) + self.config.preview_ttl_seconds
        return {
            "provider_ref": str(sandbox.id),
            "provider_state": provider_state,
            "state": state,
            "viewer_url": viewer_url,
            "viewer_expires_at": viewer_expires_at,
        }

    @staticmethod
    def _state_value(sandbox: Any) -> str:
        state = getattr(sandbox, "state", "")
        value = getattr(state, "value", state)
        return str(value or "unknown").lower()

    @staticmethod
    def _sandbox_name(user_id: str, bot_id: str) -> str:
        identity = hashlib.sha256(f"{user_id}\0{bot_id}".encode()).hexdigest()[:24]
        return f"agentgenia-{identity}"


class ComputerManager:
    def __init__(self, *, store: Any, config: ComputerConfig, provider: ComputerProvider | None = None):
        config.validate()
        self.store = store
        self.config = config
        self.provider = provider or (DaytonaComputerProvider(config) if config.configured else None)
        self._locks_guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.RLock] = {}

    def _bot_lock(self, user_id: str, bot_id: str) -> threading.RLock:
        key = (user_id, bot_id)
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    @property
    def configured(self) -> bool:
        return self.provider is not None

    def status(self, *, user_id: str, bot_id: str) -> dict[str, Any]:
        bot_id = _bot_id(bot_id)
        row = self.store.get_bot_computer(user_id, bot_id)
        if not self.configured:
            return self._response(
                row,
                bot_id=bot_id,
                state="disabled",
                reason="Las computadoras todavía no están configuradas.",
            )
        if not row:
            return self._response(None, bot_id=bot_id, state="off")
        if not row.get("provider_ref"):
            return self._response(row, state=row.get("state") or "pulling")
        try:
            snapshot = self.provider.inspect(row["provider_ref"])  # type: ignore[union-attr]
            self.store.update_bot_computer(
                user_id=user_id,
                bot_id=bot_id,
                state=snapshot["state"],
                last_error="",
            )
            return self._response(row, **snapshot)
        except Exception as exc:
            self.store.update_bot_computer(user_id=user_id, bot_id=bot_id, state="error", last_error=str(exc)[:500])
            return self._response(row, state="error", reason="No pudimos consultar la computadora.")

    def ensure(
        self,
        *,
        user_id: str,
        bot_id: str,
        bot_name: str,
        include_viewer: bool = True,
    ) -> dict[str, Any]:
        if not self.configured:
            raise ComputerError(503, "Las computadoras no están configuradas", "computers_disabled")
        bot_id = _bot_id(bot_id)
        bot_name = (bot_name or "Bot").strip()[:60]
        # Different users/bots provision in parallel; duplicate ensures for the
        # same persistent computer remain serialized.
        with self._bot_lock(user_id, bot_id):
            try:
                row = self.store.claim_bot_computer(
                    user_id,
                    bot_id,
                    self.provider.name,  # type: ignore[union-attr]
                    self._user_limit(user_id),
                )
            except ComputerLimitReached as exc:
                raise ComputerError(409, str(exc), "computer_limit_reached") from exc
            try:
                if row.get("provider_ref"):
                    try:
                        snapshot = self.provider.ensure(  # type: ignore[union-attr]
                            row["provider_ref"], include_viewer=include_viewer
                        )
                    except Exception as exc:
                        if not _provider_not_found(exc):
                            raise
                        snapshot = self.provider.create(  # type: ignore[union-attr]
                            user_id=user_id,
                            bot_id=bot_id,
                            bot_name=bot_name,
                            include_viewer=include_viewer,
                        )
                else:
                    snapshot = self.provider.create(  # type: ignore[union-attr]
                        user_id=user_id,
                        bot_id=bot_id,
                        bot_name=bot_name,
                        include_viewer=include_viewer,
                    )
                self.store.update_bot_computer(
                    user_id=user_id,
                    bot_id=bot_id,
                    provider_ref=snapshot["provider_ref"],
                    state=snapshot["state"],
                    last_error="",
                    touch=True,
                )
                return self._response(row, **snapshot)
            except ComputerError:
                raise
            except Exception as exc:
                self.store.update_bot_computer(user_id=user_id, bot_id=bot_id, state="error", last_error=str(exc)[:500])
                raise ComputerError(502, "No pudimos iniciar la computadora", "computer_provider_error") from exc

    def hand_back(self, *, user_id: str, bot_id: str) -> dict[str, Any]:
        bot_id = _bot_id(bot_id)
        row = self.store.get_bot_computer(user_id, bot_id)
        if not row:
            return self._response(None, bot_id=bot_id, state="off")
        if not self.configured or not row.get("provider_ref"):
            return self._response(row, state=row.get("state") or "off")
        try:
            snapshot = self.provider.stop(row["provider_ref"])  # type: ignore[union-attr]
            self.store.update_bot_computer(user_id=user_id, bot_id=bot_id, state="hibernated", last_error="")
            snapshot["state"] = "hibernated"
            return self._response(row, **snapshot)
        except Exception as exc:
            raise ComputerError(502, "No pudimos hibernar la computadora", "computer_provider_error") from exc

    def delete(self, *, user_id: str, bot_id: str) -> dict[str, Any]:
        bot_id = _bot_id(bot_id)
        row = self.store.get_bot_computer(user_id, bot_id)
        if not row:
            return {"deleted": False}
        if row.get("provider_ref") and not self.configured:
            raise ComputerError(
                503,
                "No se puede eliminar la referencia mientras el proveedor está desactivado",
                "computers_disabled",
            )
        if row.get("provider_ref"):
            try:
                self.provider.delete(row["provider_ref"])  # type: ignore[union-attr]
            except Exception as exc:
                if not _provider_not_found(exc):
                    raise ComputerError(502, "No pudimos eliminar la computadora", "computer_provider_error") from exc
        elif self.configured:
            try:
                self.provider.delete_identity(user_id=user_id, bot_id=bot_id)  # type: ignore[union-attr]
            except Exception as exc:
                raise ComputerError(502, "No pudimos reconciliar la computadora", "computer_provider_error") from exc
        self.store.delete_bot_computer(user_id, bot_id)
        return {"deleted": True}

    def delete_all(self, *, user_id: str) -> dict[str, Any]:
        """Elimina toda computadora persistente conocida durante una revocación."""
        deleted = 0
        errors: list[str] = []
        for row in self.store.list_bot_computers(user_id):
            try:
                self.delete(user_id=user_id, bot_id=row["bot_id"])
                deleted += 1
            except ComputerError as exc:
                errors.append(str(exc))
        return {"deleted": deleted, "errors": errors}

    def execute(self, *, user_id: str, bot_id: str, operation: Any, arguments: Any) -> dict[str, Any]:
        if not self.configured:
            raise ComputerError(503, "Las computadoras no están configuradas", "computers_disabled")
        if not isinstance(operation, str) or operation not in COMPUTER_OPERATIONS:
            raise ComputerError(400, "Operación de computadora no permitida", "bad_computer_operation")
        if not isinstance(arguments, dict):
            raise ComputerError(400, "arguments debe ser un objeto JSON", "bad_computer_arguments")
        import json

        try:
            encoded_arguments = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ComputerError(400, "arguments no es JSON válido", "bad_computer_arguments") from exc
        if len(encoded_arguments) > MAX_COMPUTER_ARGUMENTS_BYTES:
            raise ComputerError(413, "arguments excede 64 KiB", "computer_arguments_too_large")
        if operation == "status":
            return {"operation": operation, "result": self.status(user_id=user_id, bot_id=bot_id)}
        snapshot = self.ensure(
            user_id=user_id,
            bot_id=bot_id,
            bot_name="Bot",
            include_viewer=False,
        )
        row = self.store.get_bot_computer(user_id, bot_id)
        assert row and row.get("provider_ref")
        try:
            result = self.provider.execute(row["provider_ref"], operation, arguments)  # type: ignore[union-attr]
            self.store.update_bot_computer(user_id=user_id, bot_id=bot_id, state="running", last_error="", touch=True)
        except ComputerError:
            raise
        except Exception as exc:
            raise ComputerError(502, "La computadora rechazó la operación", "computer_provider_error") from exc
        payload = {"operation": operation, "computer": snapshot, "result": result}
        if operation != "screenshot":
            try:
                if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_COMPUTER_RESULT_BYTES:
                    raise ComputerError(502, "El resultado excede el límite", "computer_result_too_large")
            except (TypeError, ValueError) as exc:
                raise ComputerError(502, "El resultado no es serializable", "computer_provider_error") from exc
        return payload

    def health(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "provider": self.provider.name if self.provider else None,
            "auto_stop_minutes": self.config.auto_stop_minutes,
            "auto_archive_minutes": self.config.auto_archive_minutes,
            "basic_limit": self.config.basic_limit,
            "pro_limit": self.config.pro_limit,
            "vnc_resolution": self.config.vnc_resolution,
        }

    def _user_limit(self, user_id: str) -> int:
        user = self.store.get_user_by_id(user_id)
        return (
            self.config.pro_limit
            if user and user.get("tier") in {"pro", "business"}
            else self.config.basic_limit
        )

    def _response(self, row: dict[str, Any] | None, **overrides: Any) -> dict[str, Any]:
        state = overrides.get("state") or (row or {}).get("state") or "off"
        return {
            "configured": self.configured,
            "bot_id": overrides.get("bot_id") or (row or {}).get("bot_id", ""),
            "provider": (row or {}).get("provider") or (self.provider.name if self.provider else None),
            "state": state,
            "viewer_url": overrides.get("viewer_url", ""),
            "viewer_expires_at": int(overrides.get("viewer_expires_at") or 0),
            "reason": overrides.get("reason") or ((row or {}).get("last_error") if state == "error" else ""),
        }


COMPUTER_OPERATIONS = frozenset({
    "status", "screenshot", "click", "move", "drag", "scroll", "type", "key", "hotkey",
    "shell", "list_files", "read_file", "write_file",
})


def _bot_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ComputerError(400, "bot_id no es válido", "bad_bot_id")
    return value


def _public_state(provider_state: str) -> str:
    if provider_state in {"started", "running"}:
        return "running"
    if provider_state in {"pending_build", "building", "starting", "pending", "creating"}:
        return "pulling"
    if provider_state in {"stopped", "stopping", "paused", "pausing", "archived", "archiving"}:
        return "hibernated"
    if provider_state in {"destroyed", "deleted", "off"}:
        return "off"
    if provider_state in {"error", "build_failed"}:
        return "error"
    return "pulling"


def _model_dict(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _provider_not_found(error: Exception) -> bool:
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "status", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if value == 404:
            return True
    message = str(error).lower().strip()
    return bool(re.search(r"(?:^|\b)(?:http\s*)?404(?:\b|$).*(?:not found|no encontrado)", message))


def _text_arg(arguments: dict[str, Any], name: str, limit: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ComputerError(400, f"{name} debe ser texto de máximo {limit} caracteres", "bad_computer_arguments")
    return value


def _optional_text_arg(arguments: dict[str, Any], name: str, limit: int) -> str | None:
    value = arguments.get(name)
    if value is None or value == "":
        return None
    return _text_arg(arguments, name, limit)


def _int_arg(
    arguments: dict[str, Any],
    name: str,
    minimum: int,
    maximum: int,
    *,
    default: int | None = None,
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ComputerError(400, f"{name} debe ser entero", "bad_computer_arguments")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ComputerError(400, f"{name} está fuera de rango", "bad_computer_arguments")
    return result


def _choice_arg(arguments: dict[str, Any], name: str, allowed: set[str], default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or value not in allowed:
        raise ComputerError(400, f"{name} no es válido", "bad_computer_arguments")
    return value
