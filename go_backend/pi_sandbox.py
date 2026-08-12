"""Fail-closed Linux sandbox launcher for the Pi runtime.

The existing ``PiHarness`` keeps invoking an ordinary executable. Setting
``PI_BIN=./scripts/pi-sandbox`` inserts this launcher in front of the real Pi
binary without changing the harness contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from typing import Any

from .pi_sandbox_bwrap import (
    _bwrap_help,
    _repo_root,
    _resolved_executable,
    _runtime_path,
    _validate_bwrap_security,
    _validate_executable,
    _validate_run_paths,
    build_bwrap_command,
    write_entrypoint,
)
from .pi_sandbox_model import (
    API_KEY_ENV,
    CONNECTOR_TOKEN_ENV,
    CONNECTOR_URL_ENV,
    TMPFS_BYTES,
    EndpointPolicy,
    ParsedLoopbackURL,
    SandboxError,
    SandboxPaths,
    StreamingMasker,
    _atomic_json,
    _is_relative_to,
    _safe_error,
    build_endpoint_policies,
    normalize_models_config,
    parse_loopback_http_url,
    utc_now,
)
from .pi_sandbox_proxy import CapabilityHTTPServer, Relay

def _start_relays(paths: SandboxPaths, endpoints: dict[int, EndpointPolicy]) -> list[Relay]:
    relays: list[Relay] = []
    try:
        for index, port in enumerate(sorted(endpoints)):
            relay = Relay(paths.runtime / f"relay-{index}.sock", endpoints[port])
            relays.append(relay)
            relay.start()
        return relays
    except Exception:
        for relay in reversed(relays):
            relay.close()
        raise


def _write_audit(
    paths: SandboxPaths,
    *,
    status: str,
    started_at: str,
    policy: dict[str, Any] | None = None,
    arguments: list[str] | None = None,
    pid: int | None = None,
    exit_code: int | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "run_id": paths.run_dir.name,
        "status": status,
        "started_at": started_at,
        "updated_at": utc_now(),
        "launcher_pid": os.getpid(),
        "workspace": str(paths.workspace),
        "integration": "external_pi_bin_launcher",
        "harness_modified": False,
        "real_credentials_visible_in_sandbox": False,
    }
    if policy is not None:
        payload["policy"] = policy
    if arguments is not None:
        encoded = json.dumps(arguments, separators=(",", ":")).encode()
        payload["pi_argument_count"] = len(arguments)
        payload["pi_arguments_sha256"] = hashlib.sha256(encoded).hexdigest()
    if pid is not None:
        payload["sandbox_pid"] = pid
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if error:
        payload["error"] = _redact_runtime_secrets(error)
    _atomic_json(paths.audit, payload)


def run_sandbox(arguments: list[str]) -> int:
    paths = _validate_run_paths(_repo_root())
    started_at = utc_now()
    relays: list[Relay] = []
    process: subprocess.Popen[bytes] | None = None
    policy: dict[str, Any] | None = None
    previous_handlers: dict[int, Any] = {}
    try:
        model_url = normalize_models_config(paths.config)
        endpoints, child_env = build_endpoint_policies(dict(os.environ), model_url=model_url)
        if not endpoints:
            raise SandboxError("No network capability was generated")
        help_text = _bwrap_help(paths.bwrap)
        command, policy = build_bwrap_command(
            paths,
            arguments=arguments,
            child_env=child_env,
            endpoints=endpoints,
            bwrap_help=help_text,
        )
        write_entrypoint(paths, endpoints)
        _write_audit(
            paths,
            status="prepared",
            started_at=started_at,
            policy=policy,
            arguments=arguments,
        )
        relays = _start_relays(paths, endpoints)

        # The bwrap parent receives no server secrets; all sandbox variables are
        # passed through explicit --setenv arguments and contain sentinels only.
        process_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": child_env.get("LANG", "C.UTF-8"),
        }
        process = subprocess.Popen(command, env=process_env, close_fds=True)

        def forward(signum: int, _frame: Any) -> None:
            if process is not None and process.poll() is None:
                try:
                    process.send_signal(signum)
                except ProcessLookupError:
                    pass

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, forward)
        _write_audit(
            paths,
            status="running",
            started_at=started_at,
            policy=policy,
            arguments=arguments,
            pid=process.pid,
        )
        exit_code = process.wait()
        _write_audit(
            paths,
            status="completed" if exit_code == 0 else "failed",
            started_at=started_at,
            policy=policy,
            arguments=arguments,
            pid=process.pid,
            exit_code=exit_code,
        )
        return exit_code
    except BaseException as exc:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            _write_audit(
                paths,
                status="launcher_error",
                started_at=started_at,
                policy=policy,
                arguments=arguments,
                pid=process.pid if process is not None else None,
                exit_code=process.returncode if process is not None else None,
                error=_safe_error(exc),
            )
        except OSError:
            pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SandboxError(_safe_error(exc)) from exc
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        for relay in reversed(relays):
            relay.close()
        shutil.rmtree(paths.runtime, ignore_errors=True)


def preflight() -> dict[str, Any]:
    if sys.platform != "linux":
        raise SandboxError("Strict sandbox v1 requires Linux")
    repo_root = _repo_root()
    runtime_path = _runtime_path()
    bwrap = _validate_executable(
        _resolved_executable("bwrap", path=runtime_path), name="bwrap"
    )
    socat = _validate_executable(
        _resolved_executable("socat", path=runtime_path), name="socat"
    )
    prlimit = _validate_executable(
        _resolved_executable("prlimit", path=runtime_path), name="prlimit"
    )
    node = _validate_executable(
        _resolved_executable("node", path=runtime_path), name="node"
    )

    node_modules = repo_root / "node_modules"
    if node_modules.is_symlink() or not node_modules.is_dir():
        raise SandboxError("node_modules must be a real directory")
    real_pi_raw = node_modules / ".bin" / "pi"
    if not real_pi_raw.is_file() or not os.access(real_pi_raw, os.X_OK):
        raise SandboxError("Pi is not installed; run pnpm install")
    real_pi = _validate_executable(real_pi_raw, name="Pi")
    if not _is_relative_to(real_pi, node_modules.resolve(strict=True)):
        raise SandboxError("Real Pi executable escapes node_modules")

    help_text = _bwrap_help(bwrap)
    _validate_bwrap_security(bwrap, help_text)
    true_binary = _validate_executable(
        _resolved_executable("true", path=runtime_path), name="true"
    )
    probe = [
        str(bwrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-net",
    ]
    if "--unshare-cgroup-try" in help_text:
        probe.append("--unshare-cgroup-try")
    probe.extend(
        [
            "--cap-drop",
            "ALL",
            "--disable-userns",
            "--hostname",
            "agentgenia-probe",
            "--clearenv",
            "--ro-bind",
            "/",
            "/",
            "--size",
            str(TMPFS_BYTES),
            "--perms",
            "1777",
            "--tmpfs",
            "/tmp",
            "--",
            str(true_binary),
        ]
    )
    try:
        result = subprocess.run(
            probe,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            text=True,
            env={"PATH": runtime_path, "LANG": os.environ.get("LANG", "C.UTF-8")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError("Could not run Bubblewrap probe") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise SandboxError(
            "Kernel/host rejected the Bubblewrap namespace"
            + (f": {detail}" if detail else "")
        )
    return {
        "ok": True,
        "platform": sys.platform,
        "bwrap": str(bwrap),
        "socat": str(socat),
        "prlimit": str(prlimit),
        "node": str(node),
        "pi": str(real_pi),
        "network_default": "deny",
        "nested_user_namespaces": "denied",
        "private_tmpfs_bytes": TMPFS_BYTES,
        "fallback": "none (fail closed)",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["--check"]:
            print(json.dumps(preflight(), indent=2, sort_keys=True))
            return 0
        if arguments == ["--policy"]:
            print(
                json.dumps(
                    {
                        "backend": "bubblewrap",
                        "filesystem": "ephemeral allow-write only",
                        "network": "deny by default; fixed loopback capabilities",
                        "credentials": "injected by host HTTP proxies",
                        "fallback": "none",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if not arguments:
            raise SandboxError("Missing Pi arguments")
        return run_sandbox(arguments)
    except SandboxError as exc:
        print(f"[pi-sandbox] error: {exc}", file=sys.stderr, flush=True)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
