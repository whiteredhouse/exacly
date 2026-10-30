"""Optional local linux-bubblewrap-v1 backend for explicitly authorized candidate commands.

The installed kernel, Bubblewrap executable and reviewed root filesystem are trusted. Candidate
code receives only a private copy of that root and explicit byte snapshots. This module supplies no
dispatch, evidence admission, completion authority, persistent attempts or automatic retries.
Its closed runtime policy implements one backend; it is not a generic executor/provider registry.
Core model, evidence and completion operations do not require this module or its runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from exacly import planning

if TYPE_CHECKING:
    from exacly._isolation_seccomp import NativeFilter

Doc = dict[str, Any]
VERSION = "0.1.0"
BACKEND = "linux-bubblewrap-v1"
DEFAULT_TIMEOUT = 30.0
MAX_INPUT_FILES = 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_ARTIFACT_FILES = 4096
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ROOTFS_BYTES = 1024 * 1024 * 1024
MAX_ROOTFS_ENTRIES = 100_000
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAMESPACES = ("user", "mnt", "pid", "net", "ipc", "uts")
_CAPABILITIES = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
_SLOTS = ("proc", "dev", "tmp", "work", "inputs", "outputs", "run")
_ENVIRONMENT = {"HOME": "/tmp", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
_NOFOLLOW = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_DOMAIN = b"exacly-runtime-root-v1\0"


class IsolationError(ValueError):
    """The requested runtime or exchange cannot establish the supported isolation contract."""


@dataclass(frozen=True)
class Runtime:
    rootfs: Path
    rootfs_digest: str
    python: str
    bubblewrap: Path = Path("/usr/bin/bwrap")


def _require(value: Any, message: str) -> None:
    if not value:
        raise IsolationError(message)


def _resolve(path: Path, *, strict: bool = False) -> Path:
    try:
        return path.resolve(strict=strict)
    except RuntimeError as error:
        # Python 3.12 reports symlink loops as RuntimeError rather than OSError.
        raise IsolationError("cannot resolve isolation path: " + str(error)) from error


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _relative(value: Any) -> str:
    _require(isinstance(value, str) and value and "\0" not in value and "\\" not in value, "invalid snapshot path")
    path = PurePosixPath(value)
    _require(
        bool(path.parts)
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts)
        and len(value.encode()) <= 4096,
        "snapshot paths must be normalized relative paths",
    )
    return str(value)


def parse_policy(value: Any) -> Runtime:
    _require(
        isinstance(value, dict)
        and set(value) == {"schema_version", "backend", "rootfs", "rootfs_digest", "python", "bubblewrap"}
        and value["schema_version"] == VERSION
        and value["backend"] == BACKEND,
        "unsupported isolation runtime policy",
    )
    _require(
        all(isinstance(value[key], str) for key in ("rootfs", "rootfs_digest", "python", "bubblewrap")),
        "invalid runtime policy values",
    )
    runtime = Runtime(Path(value["rootfs"]), value["rootfs_digest"], value["python"], Path(value["bubblewrap"]))
    _runtime_values(runtime)
    return runtime


def _runtime_values(runtime: Runtime) -> None:
    _require(isinstance(runtime, Runtime), "an explicit reviewed runtime is required")
    _require(isinstance(runtime.rootfs, Path) and runtime.rootfs.is_absolute(), "runtime root must be absolute")
    _require(
        isinstance(runtime.bubblewrap, Path) and runtime.bubblewrap.is_absolute(),
        "Bubblewrap executable must be absolute",
    )
    _require(
        isinstance(runtime.rootfs_digest, str) and _DIGEST.fullmatch(runtime.rootfs_digest),
        "invalid reviewed rootfs digest",
    )
    _require(isinstance(runtime.python, str) and runtime.python.startswith("/"), "guest Python must be absolute")
    path = _relative(runtime.python[1:])
    _require(path.split("/", 1)[0] not in _SLOTS, "guest Python overlaps an exchange or control mount")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _write(path: Path, content: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _tree(root: Path | int, destination: Path | None = None, *, artifacts: bool = False) -> tuple[str, list[Doc]]:
    """Read a fixed descriptor tree; never follow source links or open special files."""
    digest = hashlib.sha256(_DOMAIN)
    count = total = 0
    files: list[Doc] = []

    def visit(fd: int, prefix: str, target: Path | None) -> None:
        nonlocal count, total
        before = _identity(os.fstat(fd))
        for name in sorted(os.listdir(fd)):
            relative = _relative(prefix + name)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            count += 1
            _require(
                count <= (MAX_ARTIFACT_FILES if artifacts else MAX_ROOTFS_ENTRIES), "filesystem entry limit exceeded"
            )
            path = target / name if target is not None else None
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
                try:
                    _require(_identity(os.fstat(child)) == _identity(info), "directory changed while capturing runtime")
                    if path is not None:
                        path.mkdir(mode=0o700)
                    digest.update(_json([relative, "directory", mode]) + b"\0")
                    visit(child, relative + "/", path)
                    if path is not None:
                        path.chmod(0o700 if artifacts else mode)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                _require(info.st_nlink == 1 and not mode & 0o6000, "hard links and privileged files are not permitted")
                total += info.st_size
                _require(
                    total <= (MAX_ARTIFACT_BYTES if artifacts else MAX_ROOTFS_BYTES), "filesystem byte limit exceeded"
                )
                source = os.open(name, _NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    _require(_identity(os.fstat(source)) == _identity(info), "file changed while capturing runtime")
                    file_digest = hashlib.sha256()
                    copied = 0
                    dest = (
                        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        if path is not None
                        else None
                    )
                    try:
                        while block := os.read(source, 128 * 1024):
                            copied += len(block)
                            _require(copied <= info.st_size, "file grew while capturing runtime")
                            file_digest.update(block)
                            if dest is not None:
                                offset = 0
                                while offset < len(block):
                                    offset += os.write(dest, block[offset:])
                        _require(
                            copied == info.st_size and _identity(os.fstat(source)) == _identity(info),
                            "file changed while capturing runtime",
                        )
                        if dest is not None:
                            os.fchmod(dest, 0o600 if artifacts else mode)
                            os.fsync(dest)
                    finally:
                        if dest is not None:
                            os.close(dest)
                    selected = "sha256:" + file_digest.hexdigest()
                    digest.update(_json([relative, "file", mode, copied, selected]) + b"\0")
                    files.append({"path": relative, "size": copied, "digest": selected})
                finally:
                    os.close(source)
            elif stat.S_ISLNK(info.st_mode) and not artifacts:
                link = os.readlink(name, dir_fd=fd)
                _require(len(link.encode()) <= 4096, "runtime symlink target is too long")
                _require(
                    _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) == _identity(info),
                    "runtime symlink changed",
                )
                digest.update(_json([relative, "symlink", link]) + b"\0")
                if path is not None:
                    path.symlink_to(link)
            else:
                raise IsolationError(
                    "only regular artifact files and directories are permitted"
                    if artifacts
                    else "runtime contains a special filesystem node"
                )
        _require(_identity(os.fstat(fd)) == before, "directory changed while capturing runtime")
        if target is not None:
            copied_directory = os.open(target, _NOFOLLOW | os.O_DIRECTORY)
            try:
                os.fsync(copied_directory)
            finally:
                os.close(copied_directory)

    root_fd = os.dup(root) if isinstance(root, int) else os.open(root, _NOFOLLOW | os.O_DIRECTORY)
    try:
        visit(root_fd, "", destination)
    finally:
        os.close(root_fd)
    return "sha256:" + digest.hexdigest(), files


def rootfs_digest(root: Path) -> str:
    """Identify exact detached runtime content. This read-only operation does not approve it."""
    try:
        _require(sys.platform == "linux", "candidate isolation requires Linux")
        _require(isinstance(root, Path) and root.is_absolute(), "runtime root must be absolute")
        _detached(root)
        return _tree(root)[0]
    except (OSError, UnicodeError, RecursionError) as error:
        raise IsolationError("cannot identify the runtime root: " + str(error)) from error


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _detached(root: Path) -> None:
    resolved = _resolve(root, strict=True)
    _require(resolved == root, "runtime root must not traverse symlinks")
    broad = {"/", "/usr", "/etc", "/var", "/run", "/proc", "/sys", "/dev", "/tmp", "/home", "/root", "/opt"}
    _require(str(root) not in broad, "a detached runtime root is required")
    for authority in (_resolve(Path.home()), _resolve(Path(__file__)), _resolve(Path(sys.executable))):
        _require(not _within(authority, root), "runtime root overlaps the operator runtime")


def _guest_python(root: Path, python: str) -> None:
    parts = PurePosixPath(python).parts[1:]
    fd = os.open(root, _NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, _NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_mode & 0o111,
            "guest Python must be a regular executable inside the rootfs",
        )
    finally:
        os.close(fd)


def _bubblewrap(runtime: Runtime) -> None:
    _require(
        not _within(_resolve(runtime.bubblewrap, strict=True), runtime.rootfs),
        "Bubblewrap must be outside the candidate runtime",
    )
    info = runtime.bubblewrap.stat()
    _require(
        stat.S_ISREG(info.st_mode) and info.st_mode & 0o111 and not info.st_mode & 0o022,
        "Bubblewrap must be a trusted installed executable",
    )
    help_text = subprocess.run(
        [str(runtime.bubblewrap), "--help"],
        env=_ENVIRONMENT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout
    required = (
        b"--unshare-user",
        b"--unshare-pid",
        b"--unshare-net",
        b"--disable-userns",
        b"--assert-userns-disabled",
        b"--new-session",
        b"--die-with-parent",
        b"--clearenv",
        b"--cap-drop",
        b"--info-fd",
        b"--seccomp",
    )
    _require(
        all(flag in help_text for flag in required),
        "Bubblewrap lacks required isolation features (version 0.8 or later required)",
    )


def _attestation(value: Any, host: Mapping[str, str]) -> Doc:
    keys = {
        "verified",
        "namespaces",
        "capabilities",
        "no_new_privileges",
        "rootfs_read_only",
        "inputs_read_only",
        "inherited_fds_closed",
        "network",
        "pid_namespace_init",
        "syscalls_filtered",
        "keyrings_denied",
        "performance_monitoring_denied",
        "unsupported_socket_families_denied",
        "async_io_denied",
    }
    _require(isinstance(value, dict) and set(value) == keys, "invalid sandbox attestation")
    _require(
        all(value[key] is True for key in keys - {"namespaces", "capabilities", "network"}),
        "sandbox protections were not verified",
    )
    namespaces = value["namespaces"]
    _require(isinstance(namespaces, dict) and set(namespaces) == set(_NAMESPACES), "incomplete namespace verification")
    _require(
        all(
            isinstance(namespaces[key], str)
            and namespaces[key] != host[key]
            and re.fullmatch(key + r":\[[0-9]+\]", namespaces[key])
            for key in _NAMESPACES
        ),
        "sandbox namespace remains shared with operator",
    )
    capabilities = value["capabilities"]
    _require(
        isinstance(capabilities, dict)
        and set(capabilities) == set(_CAPABILITIES)
        and all(isinstance(item, str) and re.fullmatch(r"0+", item) for item in capabilities.values()),
        "sandbox retains capabilities",
    )
    _require(value["network"] == "none", "sandbox network is not isolated")
    return dict(value)


def _namespace_anchor(info_fd: int, facts: Doc) -> tuple[int, int]:
    """Pin the real namespace init while the verified bootstrap is still awaiting release."""
    encoded = os.pread(info_fd, 64 * 1024 + 1, 0)
    _require(len(encoded) <= 64 * 1024, "invalid Bubblewrap process metadata")
    metadata = json.loads(encoded)
    _require(
        isinstance(metadata, dict) and type(metadata.get("child-pid")) is int and metadata["child-pid"] > 0,
        "Bubblewrap did not identify its namespace init",
    )
    child_pid = metadata["child-pid"]
    anchor = os.pidfd_open(child_pid)
    outputs: int | None = None
    try:
        _require(
            os.readlink(f"/proc/{child_pid}/ns/pid") == facts["namespaces"]["pid"],
            "namespace init does not match the verified sandbox",
        )
        status = Path(f"/proc/{child_pid}/status").read_text()
        nested = next((line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")), [])
        _require(bool(nested) and nested[-1] == "1", "Bubblewrap child is not the namespace init")
        # Pin the actual private tmpfs before releasing candidate code. A directory descriptor
        # retains this exact mount after namespace death, without exposing a writable host mount.
        outputs = os.open(f"/proc/{child_pid}/root/outputs", _NOFOLLOW | os.O_DIRECTORY)
        device = os.fstat(outputs).st_dev
        mount_rows = [line.split() for line in Path(f"/proc/{child_pid}/mountinfo").read_text().splitlines()]
        output_mounts = [row for row in mount_rows if row[4] == "/outputs"]
        _require(
            len(output_mounts) == 1
            and output_mounts[0][output_mounts[0].index("-") + 1] == "tmpfs"
            and output_mounts[0][2] == f"{os.major(device)}:{os.minor(device)}",
            "output directory is not the verified private tmpfs",
        )
        volume = os.fstatvfs(outputs)
        _require(
            volume.f_flag & os.ST_RDONLY == 0 and volume.f_blocks * volume.f_frsize <= MAX_ARTIFACT_BYTES,
            "output tmpfs does not enforce its storage limit",
        )
        return anchor, outputs
    except BaseException:
        if outputs is not None:
            os.close(outputs)
        os.close(anchor)
        raise


def _wait_namespace(anchor: int) -> None:
    # A pidfd becomes readable only after this exact process exits; numeric PID reuse cannot
    # satisfy the wait. Linux tears down the namespace's remaining processes when PID 1 exits.
    waiter = select.poll()
    waiter.register(anchor, select.POLLIN)
    _require(bool(waiter.poll(5000)), "sandbox namespace init did not terminate")


def _stop_namespace(anchor: int | None, process: subprocess.Popen[bytes]) -> None:
    if anchor is not None:
        with suppress(ProcessLookupError):
            signal.pidfd_send_signal(anchor, signal.SIGKILL)
    else:
        process.kill()


def _execute(
    command: list[str], host: Mapping[str, str], timeout: float, syscall_filter: NativeFilter
) -> tuple[int, bytes, bytes, Doc, str | None, int]:
    info_fd = os.memfd_create("exacly-isolation-status", os.MFD_CLOEXEC)
    try:
        process = subprocess.Popen(
            [command[0], "--info-fd", str(info_fd), "--seccomp", str(syscall_filter.fd), *command[1:]],
            env=_ENVIRONMENT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(info_fd, syscall_filter.fd),
            start_new_session=True,
            bufsize=0,
        )
    except BaseException:
        os.close(info_fd)
        raise
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    handshake = bytearray()
    facts: Doc | None = None
    stopped: str | None = None
    expires = time.monotonic() + timeout
    kill_expires: float | None = None
    anchor: int | None = None
    outputs: int | None = None
    result: tuple[int, bytes, bytes, Doc, str | None] | None = None
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            # A candidate can close its logs and keep running. EOF does not end supervision:
            # retain the caller's deadline until the namespace anchor has actually exited.
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if now >= expires and stopped is None:
                    stopped = "timed_out"
                    _stop_namespace(anchor, process)
                    kill_expires = now + 5
                if kill_expires is not None and now >= kill_expires:
                    raise IsolationError("sandbox descriptors remained open after namespace termination")
                for selected, _ in selector.select(0.05):
                    block = os.read(selected.fd, 64 * 1024)
                    if not block:
                        selector.unregister(selected.fd)
                        continue
                    name = selected.data
                    if name == "stdout" and facts is None:
                        handshake.extend(block)
                        _require(len(handshake) <= 64 * 1024, "sandbox attestation exceeded its size bound")
                        if b"\n" not in handshake:
                            continue
                        line, block = bytes(handshake).split(b"\n", 1)
                        facts = _attestation(json.loads(line), host)
                        anchor, outputs = _namespace_anchor(info_fd, facts)
                        facts["syscall_policy"] = syscall_filter.description
                        process.stdin.write(b"EXACLY-RUN\n")
                        process.stdin.close()
                    room = MAX_CAPTURE_BYTES - len(buffers[name])
                    buffers[name].extend(block[:room])
                    if len(block) > room and stopped is None:
                        stopped = "output_limit"
                        _stop_namespace(anchor, process)
                        kill_expires = time.monotonic() + 5
            code = process.wait(timeout=5)
        _require(
            facts is not None,
            "sandbox setup failed before candidate release: " + buffers["stderr"].decode("utf-8", "replace")[:1000],
        )
        assert facts is not None
        result = code, bytes(buffers["stdout"]), bytes(buffers["stderr"]), facts, stopped
    finally:
        terminated = False
        try:
            if anchor is not None:
                _stop_namespace(anchor, process)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if anchor is not None:
                _wait_namespace(anchor)
                if facts is not None:
                    facts["namespace_terminated"] = True
            terminated = True
        finally:
            if outputs is not None and (not terminated or result is None):
                os.close(outputs)
            if anchor is not None:
                os.close(anchor)
            os.close(info_fd)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    assert result is not None and outputs is not None
    return (*result, outputs)


def _publish(folder: Path, out: Path) -> None:
    out.mkdir(mode=0o700)
    reserved = out.stat()
    try:
        os.rename(folder, out)
    except BaseException:
        current = out.lstat()
        if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
            out.rmdir()
        raise
    fd = os.open(out.parent, _NOFOLLOW | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as error:
        raise IsolationError(
            "candidate output was published but directory durability could not be confirmed; "
            "inspect the bundle before retrying"
        ) from error
    finally:
        os.close(fd)


def run(
    runtime: Runtime, argv: Sequence[str], inputs: Mapping[str, bytes], out: Path, *, timeout: float = DEFAULT_TIMEOUT
) -> Doc:
    """Run one candidate command after verifying its actual sandbox, publishing untrusted outputs."""
    try:
        _require(sys.platform == "linux", "candidate isolation requires Linux")
        _require(
            hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal") and hasattr(os, "memfd_create"),
            "candidate isolation requires Linux pidfd and memfd support",
        )
        _runtime_values(runtime)
        _detached(runtime.rootfs)
        _require(
            type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 3600,
            "timeout must be finite and greater than zero, at most 3600 seconds",
        )
        _require(
            not isinstance(argv, (str, bytes)) and isinstance(argv, Sequence) and 0 < len(argv) <= 1024,
            "candidate argv must be a nonempty sequence",
        )
        command = list(argv)
        _require(
            all(isinstance(item, str) and "\0" not in item for item in command) and command[0], "invalid candidate argv"
        )
        _require(sum(len(item.encode()) for item in command) <= 128 * 1024, "candidate argv exceeds its size bound")
        _require(isinstance(inputs, Mapping) and len(inputs) <= MAX_INPUT_FILES, "too many input snapshots")
        snapshots = {_relative(name): value for name, value in inputs.items()}
        _require(all(isinstance(value, bytes) for value in snapshots.values()), "input snapshots must contain bytes")
        _require(
            sum(len(value) for value in snapshots.values()) <= MAX_INPUT_BYTES, "input snapshot byte limit exceeded"
        )
        for name in snapshots:
            _require(
                not any(
                    parent.as_posix() in snapshots
                    for parent in PurePosixPath(name).parents
                    if parent != PurePosixPath(".")
                ),
                "snapshot file conflicts with a directory",
            )
        _require(isinstance(out, Path) and out.name not in ("", ".", ".."), "a new output directory is required")
        parent = _resolve(out.parent, strict=True)
        destination = parent / out.name
        _require(not destination.exists() and not destination.is_symlink(), "output must be a new directory")
        _require(
            not _within(parent, runtime.rootfs) and not _within(runtime.rootfs, destination),
            "runtime and output paths overlap",
        )
        _bubblewrap(runtime)
        host = {name: os.readlink("/proc/self/ns/" + name) for name in _NAMESPACES}
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        _require("Seccomp_filters" in status, "candidate isolation requires kernel seccomp-filter count reporting")
        from exacly import _isolation_seccomp

        with (
            tempfile.TemporaryDirectory(prefix=".exacly-isolation-", dir=parent) as temporary,
            _isolation_seccomp.native_filter() as syscall_filter,
        ):
            stage = Path(temporary)
            root = stage / "root"
            root.mkdir(mode=0o700)
            captured, _ = _tree(runtime.rootfs, root)
            _require(captured == runtime.rootfs_digest, "runtime root does not match its reviewed digest")
            _guest_python(root, runtime.python)
            for name in _SLOTS:
                path = root / name
                if path.exists() or path.is_symlink():
                    _require(
                        stat.S_ISDIR(path.lstat().st_mode), "runtime exchange mount slot must be a plain directory"
                    )
                else:
                    path.mkdir(mode=0o755)
            control_slot = root / "run" / "exacly"
            if control_slot.exists() or control_slot.is_symlink():
                _require(stat.S_ISDIR(control_slot.lstat().st_mode), "runtime control slot must be a plain directory")
            else:
                control_slot.mkdir(mode=0o755)
            control = stage / "control"
            control.mkdir(mode=0o700)
            bootstrap = Path(__file__).with_name("_isolation_bootstrap.py").read_bytes()
            _write(control / "bootstrap.py", bootstrap)
            _write(control / "empty", b"")
            config = {
                "host_namespaces": host,
                "argv": command,
                "environment": _ENVIRONMENT,
                "file_size_limit": MAX_ARTIFACT_BYTES,
                "host_seccomp_filters": int(status["Seccomp_filters"].strip()),
                "syscalls": syscall_filter.syscalls,
            }
            _write(control / "config.json", _json(config))
            snapshot_root = stage / "inputs"
            snapshot_root.mkdir(mode=0o700)
            for name, content in sorted(snapshots.items()):
                path = snapshot_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _write(path, content, 0o400)
            launch = [
                str(runtime.bubblewrap),
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--unshare-uts",
                "--disable-userns",
                "--assert-userns-disabled",
                "--die-with-parent",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--clearenv",
                "--ro-bind",
                str(root),
                "/",
                "--proc",
                "/proc",
                "--ro-bind",
                str(control / "empty"),
                "/proc/keys",
                "--ro-bind",
                str(control / "empty"),
                "/proc/key-users",
                "--dev",
                "/dev",
                "--size",
                str(MAX_ARTIFACT_BYTES),
                "--tmpfs",
                "/tmp",
                "--size",
                str(MAX_ARTIFACT_BYTES),
                "--tmpfs",
                "/work",
                "--ro-bind",
                str(snapshot_root),
                "/inputs",
                "--size",
                str(MAX_ARTIFACT_BYTES),
                "--tmpfs",
                "/outputs",
                "--ro-bind",
                str(control),
                "/run/exacly",
                "--chdir",
                "/work",
                "--",
                runtime.python,
                "-I",
                "-S",
                "/run/exacly/bootstrap.py",
            ]
            code, stdout, stderr, facts, stopped, outputs = _execute(launch, host, float(timeout), syscall_filter)
            bundle = stage / "bundle"
            try:
                bundle.mkdir(mode=0o700)
                artifacts = bundle / "artifacts"
                artifacts.mkdir(mode=0o700)
                _, collected = _tree(outputs, artifacts, artifacts=True)
            finally:
                os.close(outputs)
            _write(bundle / "stdout.bin", stdout)
            _write(bundle / "stderr.bin", stderr)
            report: Doc = {
                "schema_version": VERSION,
                "backend": BACKEND,
                "status": stopped or ("completed" if code == 0 else "candidate_failed"),
                "exit_code": code,
                "argv": command,
                "runtime": {"rootfs_digest": captured, "python": runtime.python},
                "isolation": facts,
                "inputs": [
                    {"path": name, "size": len(content), "digest": _hash(content)}
                    for name, content in sorted(snapshots.items())
                ],
                "stdout": {"path": "stdout.bin", "size": len(stdout), "digest": _hash(stdout)},
                "stderr": {"path": "stderr.bin", "size": len(stderr), "digest": _hash(stderr)},
                "artifacts": [{**item, "path": "artifacts/" + item["path"]} for item in collected],
                "claims": {
                    "outputs_trusted": False,
                    "evidence_admitted": False,
                    "completion_authorized": False,
                    "self_authenticating": False,
                },
            }
            report["digest"] = planning.digest(report)
            _write(bundle / "report.json", _json(report) + b"\n")
            bundle_fd = os.open(bundle, _NOFOLLOW | os.O_DIRECTORY)
            try:
                os.fsync(bundle_fd)
            finally:
                os.close(bundle_fd)
            _publish(bundle, destination)
            return report
    except IsolationError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, subprocess.SubprocessError) as error:
        raise IsolationError("candidate isolation failed: " + str(error)) from error


def as_text(report: Doc) -> str:
    return "\n".join(
        (
            f"Candidate isolation: {report['status']} (exit {report['exit_code']}).",
            f"Root: {report['runtime']['rootfs_digest']}.",
            f"Artifacts: {len(report['artifacts'])}; results are untrusted.",
        )
    )
