"""Bubblewrap filesystem/process policy construction for Pi sandbox."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from .pi_sandbox_model import (
    API_KEY_ENV,
    AUDIT_FILENAME,
    CHILD_ENV_ALLOWLIST,
    CONNECTOR_TOKEN_ENV,
    MAX_PROXY_CONNECTIONS,
    RUNTIME_DIRNAME,
    RUNTIME_MOUNT,
    RUN_ID_RE,
    TMPFS_BYTES,
    EndpointPolicy,
    SandboxError,
    SandboxPaths,
    _is_relative_to,
)

def _resolved_executable(name: str, *, path: str | None = None) -> Path:
    resolved = shutil.which(name, path=path)
    if not resolved:
        raise SandboxError(f"Missing required executable: {name}")
    return Path(resolved).resolve()


def _validate_executable(
    path: Path,
    *,
    name: str,
    forbidden_root: Path | None = None,
) -> Path:
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise SandboxError(f"Could not validate {name}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SandboxError(f"{name} is not executable")
    if mode & 0o022:
        raise SandboxError(f"{name} cannot be group/world writable")
    if forbidden_root is not None and _is_relative_to(resolved, forbidden_root):
        raise SandboxError(f"{name} cannot live inside the writable run directory")
    return resolved


def _runtime_path() -> str:
    runtime_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    node_bin_dir = (os.environ.get("PI_NODE_BIN_DIR") or "").strip()
    if node_bin_dir:
        runtime_path = node_bin_dir + os.pathsep + runtime_path
    return runtime_path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _validate_run_paths(repo_root: Path) -> SandboxPaths:
    if sys.platform != "linux":
        raise SandboxError("Strict sandbox v1 requires Linux")

    workspace_raw = Path.cwd()
    home_raw = os.environ.get("HOME")
    config_raw = os.environ.get("PI_CODING_AGENT_DIR")
    sessions_raw = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if not home_raw or not config_raw:
        raise SandboxError("This launcher may only be called by PiHarness")

    home_path = Path(home_raw).expanduser()
    config_path = Path(config_raw).expanduser()
    if not home_path.is_absolute() or not config_path.is_absolute():
        raise SandboxError("PiHarness run paths must be absolute")
    for name, path in (
        ("workspace", workspace_raw),
        ("home", home_path),
        ("config", config_path),
    ):
        if path.is_symlink():
            raise SandboxError(f"{name} cannot be a symlink")

    try:
        workspace = workspace_raw.resolve(strict=True)
        home = home_path.resolve(strict=True)
        config = config_path.resolve(strict=True)
    except OSError as exc:
        raise SandboxError("Could not resolve ephemeral run directories") from exc

    run_dir = workspace.parent
    if workspace.name != "workspace" or home.name != "home" or config.name != "config":
        raise SandboxError("Run directory layout does not match PiHarness")
    if home.parent != run_dir or config.parent != run_dir:
        raise SandboxError("workspace, home and config must share one ephemeral run")
    if not RUN_ID_RE.fullmatch(run_dir.name):
        raise SandboxError("Run directory does not contain a PiHarness run id")

    expected_sessions = config / "sessions"
    if sessions_raw:
        try:
            sessions = Path(sessions_raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise SandboxError("Could not resolve Pi session directory") from exc
        if sessions != expected_sessions.resolve(strict=True):
            raise SandboxError("Pi session directory escapes the run config")

    effective_uid = os.geteuid()
    for name, path in (
        ("run", run_dir),
        ("workspace", workspace),
        ("home", home),
        ("config", config),
        ("sessions", expected_sessions),
    ):
        if not path.is_dir():
            raise SandboxError(f"{name} is not a real directory")
        if path.stat().st_uid != effective_uid:
            raise SandboxError(f"{name} is not owned by the backend user")
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise SandboxError(f"Could not harden permissions for {path}") from exc

    runtime = run_dir / RUNTIME_DIRNAME
    audit = run_dir / AUDIT_FILENAME
    if runtime.exists() or runtime.is_symlink():
        raise SandboxError("Sandbox runtime directory already exists")
    if audit.exists() or audit.is_symlink():
        raise SandboxError("Sandbox audit file already exists")

    node_modules = repo_root / "node_modules"
    if node_modules.is_symlink() or not node_modules.is_dir():
        raise SandboxError("node_modules must be a real directory")
    node_modules_resolved = node_modules.resolve(strict=True)
    real_pi_raw = node_modules / ".bin" / "pi"
    if not real_pi_raw.is_file() or not os.access(real_pi_raw, os.X_OK):
        raise SandboxError("Pi is not installed; run pnpm install")
    real_pi = _validate_executable(real_pi_raw, name="Pi", forbidden_root=run_dir)
    if not _is_relative_to(real_pi, node_modules_resolved):
        raise SandboxError("Real Pi executable escapes node_modules")

    runtime_path = _runtime_path()
    node = _validate_executable(
        _resolved_executable("node", path=runtime_path),
        name="node",
        forbidden_root=run_dir,
    )
    socat = _validate_executable(
        _resolved_executable("socat", path=runtime_path),
        name="socat",
        forbidden_root=run_dir,
    )
    prlimit = _validate_executable(
        _resolved_executable("prlimit", path=runtime_path),
        name="prlimit",
        forbidden_root=run_dir,
    )
    bwrap = _validate_executable(
        _resolved_executable("bwrap", path=runtime_path),
        name="bwrap",
        forbidden_root=run_dir,
    )

    runtime.mkdir(mode=0o700)
    return SandboxPaths(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace=workspace,
        home=home,
        config=config,
        runtime=runtime,
        audit=audit,
        real_pi=real_pi,
        node=node,
        socat=socat,
        prlimit=prlimit,
        bwrap=bwrap,
    )


def _mkdir_destination(
    command: list[str],
    path: Path,
    created: set[Path],
    mounted: set[Path],
) -> None:
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        if current in created or any(
            mount == current or mount in current.parents for mount in mounted
        ):
            continue
        command.extend(["--dir", str(current)])
        created.add(current)


def _add_bind(
    command: list[str],
    source: Path,
    destination: Path,
    *,
    writable: bool,
    created: set[Path],
    mounted: set[Path],
) -> None:
    _mkdir_destination(command, destination.parent, created, mounted)
    option = "--bind" if writable else "--ro-bind"
    command.extend([option, str(source), str(destination)])
    created.add(destination)
    mounted.add(destination)


def _runtime_root(binary: Path) -> Path | None:
    system_roots = (Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64"))
    if any(binary == root or root in binary.parents for root in system_roots):
        return None
    parent = binary.parent
    if parent.name == "bin":
        return parent.parent
    return parent


def _extension_mounts(arguments: list[str], repo_root: Path) -> set[Path]:
    mounts: set[Path] = set()
    node_modules = (repo_root / "node_modules").resolve(strict=True)
    repo_extensions = repo_root / "extensions"
    if repo_extensions.is_symlink():
        raise SandboxError("Repository extensions directory cannot be a symlink")
    resolved_extensions = (
        repo_extensions.resolve(strict=True) if repo_extensions.is_dir() else None
    )

    index = 0
    while index < len(arguments):
        if arguments[index] == "--extension":
            if index + 1 >= len(arguments):
                raise SandboxError("--extension requires a path")
            try:
                extension = Path(arguments[index + 1]).expanduser().resolve(strict=True)
            except OSError as exc:
                raise SandboxError("Could not resolve Pi extension") from exc
            if not extension.is_file():
                raise SandboxError(f"Pi extension does not exist: {extension}")
            if _is_relative_to(extension, node_modules):
                pass
            elif resolved_extensions and _is_relative_to(extension, resolved_extensions):
                mounts.add(resolved_extensions)
            else:
                raise SandboxError(
                    "Pi extensions must live under node_modules or repository extensions"
                )
            index += 2
            continue
        index += 1
    return mounts


def _system_mounts(
    command: list[str],
    created: set[Path],
    mounted: set[Path],
) -> list[str]:
    readonly: list[str] = []
    usr = Path("/usr")
    if not usr.is_dir():
        raise SandboxError("Linux without /usr is not supported")
    command.extend(["--dir", "/usr", "--ro-bind", "/usr", "/usr"])
    created.add(Path("/usr"))
    mounted.add(Path("/usr"))
    readonly.append("/usr")

    for raw in ("/bin", "/sbin", "/lib", "/lib64"):
        path = Path(raw)
        if path.is_symlink():
            command.extend(["--symlink", os.readlink(path), raw])
            created.add(path)
        elif path.exists():
            command.extend(["--dir", raw, "--ro-bind", raw, raw])
            created.add(path)
            mounted.add(path)
            readonly.append(raw)

    command.extend(["--dir", "/etc"])
    created.add(Path("/etc"))
    for raw in (
        "/etc/passwd",
        "/etc/group",
        "/etc/nsswitch.conf",
        "/etc/hosts",
        "/etc/localtime",
        "/etc/ld.so.cache",
        "/etc/services",
        "/etc/protocols",
    ):
        path = Path(raw)
        if path.is_file():
            command.extend(["--ro-bind", raw, raw])
            readonly.append(raw)
    for raw in ("/etc/ssl", "/etc/ca-certificates", "/etc/pki"):
        path = Path(raw)
        if path.is_dir():
            command.extend(["--ro-bind", raw, raw])
            readonly.append(raw)
    return readonly


def _bwrap_help(bwrap: Path) -> str:
    try:
        result = subprocess.run(
            [str(bwrap), "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError("Could not inspect Bubblewrap") from exc
    return result.stdout


def _validate_bwrap_security(bwrap: Path, help_text: str) -> None:
    required = (
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-net",
        "--cap-drop",
        "--disable-userns",
        "--clearenv",
        "--remount-ro",
        "--size",
        "--perms",
    )
    missing = [option for option in required if option not in help_text]
    if missing:
        raise SandboxError(f"Bubblewrap lacks required options: {', '.join(missing)}")
    try:
        mode = bwrap.stat().st_mode
    except OSError as exc:
        raise SandboxError("Could not validate Bubblewrap") from exc
    if mode & stat.S_ISUID:
        raise SandboxError(
            "Setuid Bubblewrap is not supported; use non-setuid bwrap with user namespaces"
        )


def build_bwrap_command(
    paths: SandboxPaths,
    *,
    arguments: list[str],
    child_env: dict[str, str],
    endpoints: dict[int, EndpointPolicy],
    bwrap_help: str,
) -> tuple[list[str], dict[str, Any]]:
    _validate_bwrap_security(paths.bwrap, bwrap_help)
    command = [
        str(paths.bwrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-net",
    ]
    if "--unshare-cgroup-try" in bwrap_help:
        command.append("--unshare-cgroup-try")
    command.extend(
        [
            "--cap-drop",
            "ALL",
            "--disable-userns",
            "--hostname",
            "agentgenia-sandbox",
            "--clearenv",
        ]
    )

    created: set[Path] = {Path("/")}
    mounted: set[Path] = set()
    readonly = _system_mounts(command, created, mounted)
    command.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            str(TMPFS_BYTES),
            "--perms",
            "1777",
            "--tmpfs",
            "/tmp",
        ]
    )
    created.update({Path("/proc"), Path("/dev"), Path("/tmp")})
    mounted.update({Path("/proc"), Path("/dev"), Path("/tmp")})

    runtime_roots: set[Path] = set()
    for binary in (paths.node, paths.socat, paths.prlimit):
        runtime_root = _runtime_root(binary)
        if runtime_root and runtime_root.exists() and runtime_root not in runtime_roots:
            _add_bind(
                command,
                runtime_root,
                runtime_root,
                writable=False,
                created=created,
                mounted=mounted,
            )
            readonly.append(str(runtime_root))
            runtime_roots.add(runtime_root)

    node_modules = paths.repo_root / "node_modules"
    _add_bind(
        command,
        node_modules,
        node_modules,
        writable=False,
        created=created,
        mounted=mounted,
    )
    readonly.append(str(node_modules))

    for mount in sorted(_extension_mounts(arguments, paths.repo_root), key=str):
        _add_bind(
            command,
            mount,
            mount,
            writable=False,
            created=created,
            mounted=mounted,
        )
        readonly.append(str(mount))

    writable_paths = (paths.workspace, paths.home, paths.config)
    for writable_path in writable_paths:
        _add_bind(
            command,
            writable_path,
            writable_path,
            writable=True,
            created=created,
            mounted=mounted,
        )

    _add_bind(
        command,
        paths.runtime,
        RUNTIME_MOUNT,
        writable=False,
        created=created,
        mounted=mounted,
    )
    readonly.append(str(RUNTIME_MOUNT))

    safe_path_parts = [str(paths.node.parent)]
    for candidate in ("/usr/local/bin", "/usr/bin", "/bin"):
        if candidate not in safe_path_parts:
            safe_path_parts.append(candidate)
    sandbox_env = dict(child_env)
    sandbox_env["PATH"] = os.pathsep.join(safe_path_parts)
    sandbox_env["HOME"] = str(paths.home)
    sandbox_env["PI_CODING_AGENT_DIR"] = str(paths.config)
    sandbox_env["PI_CODING_AGENT_SESSION_DIR"] = str(paths.config / "sessions")
    sandbox_env["TMPDIR"] = "/tmp"
    for name in sorted(sandbox_env):
        value = sandbox_env[name]
        if "\x00" in name or "\x00" in value:
            raise SandboxError("Sandbox environment contains a NUL byte")
        command.extend(["--setenv", name, value])

    # Bubblewrap starts with an empty tmpfs root. Freeze that root after all
    # mounts are assembled; writable bind mounts and the /tmp submount remain
    # writable because --remount-ro is intentionally non-recursive.
    command.extend(["--remount-ro", "/"])
    command.extend(["--chdir", str(paths.workspace), "--"])
    command.extend(
        [
            "/bin/sh",
            str(RUNTIME_MOUNT / "entrypoint.sh"),
            str(paths.real_pi),
            *arguments,
        ]
    )

    policy = {
        "backend": "bubblewrap",
        "network_namespace": True,
        "nested_user_namespaces_disabled": True,
        "host_root_mounted": False,
        "empty_root_tmpfs_readonly": True,
        "writable": [str(path) for path in writable_paths] + ["/tmp"],
        "readonly": sorted(set(readonly)),
        "capabilities": [endpoints[port].public_summary() for port in sorted(endpoints)],
        "resource_limits": {
            "core_bytes": 0,
            "open_files": 1024,
            "processes": 256,
            "single_file_bytes": 1_073_741_824,
            "private_tmpfs_bytes": TMPFS_BYTES,
            "host_proxy_connections": MAX_PROXY_CONNECTIONS,
            "wall_clock": "enforced by PiHarness timeout",
        },
    }
    return command, policy


def write_entrypoint(paths: SandboxPaths, endpoints: dict[int, EndpointPolicy]) -> None:
    lines = ["#!/bin/sh", "set -eu", "relay_pids=''", "agent_pid=''", ""]
    for index, port in enumerate(sorted(endpoints)):
        socket_path = RUNTIME_MOUNT / f"relay-{index}.sock"
        lines.extend(
            [
                f"{shlex.quote(str(paths.socat))} "
                f"TCP4-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork,backlog=16 "
                f"UNIX-CONNECT:{shlex.quote(str(socket_path))} >/dev/null 2>&1 &",
                "relay_pids=\"$relay_pids $!\"",
            ]
        )
    lines.extend(
        [
            "sleep 0.05",
            "for relay_pid in $relay_pids; do",
            "  kill -0 \"$relay_pid\" 2>/dev/null || {",
            "    printf '%s\\n' '[pi-sandbox] failed to start a loopback relay' >&2",
            "    exit 78",
            "  }",
            "done",
            "stop_children() {",
            "  if [ -n \"$agent_pid\" ]; then kill -TERM \"$agent_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"$relay_pids\" ]; then kill -TERM $relay_pids 2>/dev/null || true; fi",
            "  sleep 0.05",
            "  if [ -n \"$agent_pid\" ]; then kill -KILL \"$agent_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"$relay_pids\" ]; then kill -KILL $relay_pids 2>/dev/null || true; fi",
            "  wait 2>/dev/null || true",
            "}",
            "on_exit() {",
            "  status=$?",
            "  trap - EXIT HUP INT TERM",
            "  stop_children",
            "  exit \"$status\"",
            "}",
            "on_hup() { trap - EXIT HUP INT TERM; stop_children; exit 129; }",
            "on_int() { trap - EXIT HUP INT TERM; stop_children; exit 130; }",
            "on_term() { trap - EXIT HUP INT TERM; stop_children; exit 143; }",
            "trap on_exit EXIT",
            "trap on_hup HUP",
            "trap on_int INT",
            "trap on_term TERM",
            f"{shlex.quote(str(paths.prlimit))} "
            "--core=0:0 --nofile=1024:1024 --nproc=256:256 "
            "--fsize=1073741824:1073741824 -- \"$@\" &",
            "agent_pid=$!",
            "set +e",
            "wait \"$agent_pid\"",
            "status=$?",
            "set -e",
            "agent_pid=''",
            "exit \"$status\"",
            "",
        ]
    )
    entrypoint = paths.runtime / "entrypoint.sh"
    entrypoint.write_text("\n".join(lines), encoding="utf-8")
    entrypoint.chmod(0o700)
