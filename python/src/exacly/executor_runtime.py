"""Trusted local worker supervision; durable coordination belongs to ``executor``.

The worker gate precedes all isolation work. The namespace gate precedes candidate release.
Callbacks must durably retain their identities before returning. Saved process numbers are never
signalled without checking boot, start time and namespace identity through an opened pidfd.
"""

from __future__ import annotations

import base64
import errno
import json
import math
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from exacly import isolation

Doc = dict[str, Any]
MAX_FRAME = 16 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
_BOOT = Path("/proc/sys/kernel/random/boot_id")
_IDENTITY_KEYS = {"boot_id", "pid", "start_time", "pid_namespace"}


class RuntimeError(ValueError):
    """Runtime setup or termination could not establish the required facts."""


def _require(value: Any, message: str) -> None:
    if not value:
        raise RuntimeError(message)


def _start_time(pid: int) -> int:
    # comm can contain spaces and closing parentheses; fields after its final ')' are numeric.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[19])


def identity(pid: int) -> Doc:
    """Capture a live process identity while holding its exact kernel process handle."""
    _require(type(pid) is int and pid > 1, "invalid worker process number")
    handle = os.pidfd_open(pid)
    try:
        first = _start_time(pid)
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        second = _start_time(pid)
        _require(first == second, "process changed while capturing its identity")
        waiter = select.poll()
        waiter.register(handle, select.POLLIN)
        _require(not waiter.poll(0), "process exited while capturing its identity")
        return {
            "boot_id": _BOOT.read_text().strip(),
            "pid": pid,
            "start_time": first,
            "pid_namespace": namespace,
        }
    finally:
        os.close(handle)


def validate_identity(value: Any) -> Doc:
    _require(isinstance(value, dict) and set(value) == _IDENTITY_KEYS, "invalid retained process identity")
    _require(
        isinstance(value["boot_id"], str)
        and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["boot_id"])
        and type(value["pid"]) is int
        and value["pid"] > 1
        and type(value["start_time"]) is int
        and value["start_time"] >= 0
        and isinstance(value["pid_namespace"], str)
        and re.fullmatch(r"pid:\[[0-9]+\]", value["pid_namespace"]),
        "invalid retained process identity fields",
    )
    return dict(value)


def process_identity(pid: int | None = None) -> Doc:
    """Return a durable owner identity; callers may omit their own process number."""
    return identity(os.getpid() if pid is None else pid)


def alive(raw: Doc) -> bool:
    """Inspect the retained exact process without signalling it."""
    saved = validate_identity(raw)
    if saved["boot_id"] != _BOOT.read_text().strip():
        return False
    handle: int | None = None
    try:
        handle = os.pidfd_open(saved["pid"])
        waiter = select.poll()
        waiter.register(handle, select.POLLIN)
        if waiter.poll(0) or _start_time(saved["pid"]) != saved["start_time"]:
            return False
        _require(
            os.readlink(f"/proc/{saved['pid']}/ns/pid") == saved["pid_namespace"],
            "retained live process namespace changed",
        )
        return not waiter.poll(0) and _start_time(saved["pid"]) == saved["start_time"]
    except (ProcessLookupError, FileNotFoundError):
        return False
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise RuntimeError("cannot inspect retained process: " + str(error)) from error
    finally:
        if handle is not None:
            os.close(handle)


def terminate(identities: Sequence[Doc]) -> list[Doc]:
    """Kill and await exact retained processes; never signal a reused process number.

    A failure raises rather than claiming termination. Callers must retain the occupied attempt.
    Pass worker before namespace so no living worker can create another sandbox after cleanup.
    """
    proofs: list[Doc] = []
    for raw in identities:
        saved = validate_identity(raw)
        _require(saved["pid"] != os.getpid(), "refusing to terminate the current supervisor")
        reason = "previous_boot"
        if saved["boot_id"] == _BOOT.read_text().strip():
            handle: int | None = None
            try:
                handle = os.pidfd_open(saved["pid"])
                waiter = select.poll()
                waiter.register(handle, select.POLLIN)
                if waiter.poll(0):
                    reason = "already_exited"
                elif _start_time(saved["pid"]) != saved["start_time"]:
                    reason = "pid_reused"
                else:
                    _require(
                        os.readlink(f"/proc/{saved['pid']}/ns/pid") == saved["pid_namespace"],
                        "retained process namespace changed; termination remains unresolved",
                    )
                    # Recheck after reading the namespace. The pidfd pins the signal target even
                    # if the original process exits and its number is reused between these reads.
                    _require(
                        _start_time(saved["pid"]) == saved["start_time"],
                        "process changed during termination; retry recovery",
                    )
                    if waiter.poll(0):
                        proofs.append({"identity": saved, "terminated": True, "reason": "already_exited"})
                        continue
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(handle, signal.SIGKILL)
                    _require(bool(waiter.poll(5000)), "retained process did not terminate; recovery remains unresolved")
                    reason = "terminated"
            except (ProcessLookupError, FileNotFoundError):
                reason = "absent"
            except OSError as error:
                if error.errno == errno.ESRCH:
                    reason = "absent"
                else:
                    raise RuntimeError("cannot verify retained process termination: " + str(error)) from error
            finally:
                if handle is not None:
                    os.close(handle)
        proofs.append({"identity": saved, "terminated": True, "reason": reason})
    return proofs


def _encoded(value: Any) -> bytes:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    _require(len(content) <= MAX_FRAME, "worker message exceeds its size bound")
    return struct.pack("!I", len(content)) + content


def _send(channel: socket.socket, value: Any) -> None:
    channel.sendall(_encoded(value))


def _receive(channel: socket.socket, *, limit: int = MAX_FRAME) -> Any:
    def read(size: int) -> bytes:
        parts = bytearray()
        while len(parts) < size:
            block = channel.recv(size - len(parts))
            _require(bool(block), "worker control channel closed before its message completed")
            parts.extend(block)
        return bytes(parts)

    size = struct.unpack("!I", read(4))[0]
    _require(size <= limit, "worker message exceeds its size bound")
    return json.loads(read(size))


def run(
    runtime: isolation.Runtime,
    argv: Sequence[str],
    inputs: Mapping[str, bytes],
    out: Path,
    *,
    timeout: float,
    on_worker: Callable[[Doc], None],
    on_namespace: Callable[[Doc], None],
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Doc:
    """Supervise one gated worker including staging, execution and output publication."""
    _require(
        type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 3600,
        "executor timeout must be finite, greater than zero and at most 3600 seconds",
    )
    expires = time.monotonic() + float(timeout)
    channel, child_channel = socket.socketpair()
    process: subprocess.Popen[bytes] | None = None
    worker: Doc | None = None
    namespace: Doc | None = None
    result: Doc | None = None
    status: str | None = None
    reason: str | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_executor_worker.py")),
                str(child_channel.fileno()),
                str(os.getpid()),
                repr(expires),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            close_fds=True,
            pass_fds=(child_channel.fileno(),),
            start_new_session=True,
        )
        child_channel.close()
        worker = identity(process.pid)
        on_worker(worker)
        # No byte that can start staging is sent until the durable callback returns. A slow
        # callback cannot authorize launch after either cancellation or the reviewed deadline.
        cancelled = is_cancelled()
        if cancelled or time.monotonic() >= expires:
            proofs = terminate([worker])
            process.wait(timeout=5)
            return {
                "status": "cancelled" if cancelled else "timed_out",
                "report": None,
                "reason": "execution cancellation requested" if cancelled else "executor wall-clock limit exhausted",
                "termination": {"worker": proofs[0], "namespace": None},
            }
        request = {
            "runtime": {
                "schema_version": isolation.VERSION,
                "backend": isolation.BACKEND,
                "rootfs": str(runtime.rootfs),
                "rootfs_digest": runtime.rootfs_digest,
                "python": runtime.python,
                "bubblewrap": str(runtime.bubblewrap),
            },
            "argv": list(argv),
            "inputs": {name: base64.b64encode(content).decode("ascii") for name, content in inputs.items()},
            "out": str(out),
            "timeout": min(float(timeout), max(0.001, expires - time.monotonic())),
        }
        pending = bytearray(_encoded(request))
        incoming = bytearray()
        channel.setblocking(False)
        while result is None and status is None:
            if is_cancelled():
                status, reason = "cancelled", "execution cancellation requested"
                break
            if time.monotonic() >= expires:
                status, reason = "timed_out", "executor wall-clock limit exhausted"
                break
            readable, writable, _ = select.select([channel], [channel] if pending else [], [], 0.02)
            if writable:
                try:
                    count = channel.send(pending[: 64 * 1024])
                    del pending[:count]
                except BrokenPipeError:
                    status, reason = "runtime_failed", "worker exited before accepting its request"
                    break
            if readable:
                block = channel.recv(64 * 1024)
                if not block:
                    if time.monotonic() >= expires:
                        status, reason = "timed_out", "executor wall-clock limit exhausted"
                    else:
                        status, reason = "interrupted", "worker exited without a complete retained result"
                    break
                incoming.extend(block)
                _require(len(incoming) <= MAX_RESPONSE + 4, "worker response exceeds its size bound")
                while len(incoming) >= 4:
                    size = struct.unpack("!I", incoming[:4])[0]
                    _require(size <= MAX_RESPONSE, "worker response exceeds its size bound")
                    if len(incoming) < size + 4:
                        break
                    message = json.loads(incoming[4 : size + 4])
                    del incoming[: size + 4]
                    _require(isinstance(message, dict), "invalid worker response")
                    if message.get("type") == "namespace":
                        _require(namespace is None, "worker supplied more than one namespace identity")
                        namespace = validate_identity(message.get("identity"))
                        _require(namespace == identity(namespace["pid"]), "worker namespace identity changed")
                        _require(
                            namespace["pid_namespace"] != worker["pid_namespace"],
                            "worker did not establish a separate candidate namespace",
                        )
                        on_namespace(namespace)
                        cancelled = is_cancelled()
                        if cancelled or time.monotonic() >= expires:
                            status = "cancelled" if cancelled else "timed_out"
                            reason = (
                                "execution cancellation requested"
                                if cancelled
                                else "executor wall-clock limit exhausted"
                            )
                            break
                        pending.extend(_encoded({"type": "release"}))
                    elif message.get("type") == "result":
                        _require(
                            namespace is not None and isinstance(message.get("report"), dict), "invalid worker result"
                        )
                        result = message["report"]
                    elif message.get("type") == "error":
                        status = "runtime_failed"
                        reason = str(message.get("reason", "worker failed"))[:4096]
                    else:
                        raise RuntimeError("unknown worker response")
        retained = [item for item in (worker, namespace) if item is not None]
        proofs = terminate(retained)
        process.wait(timeout=5)
        return {
            "status": status or result["status"],  # type: ignore[index]
            "report": result,
            "reason": reason,
            "termination": {"worker": proofs[0], "namespace": proofs[1] if len(proofs) > 1 else None},
        }
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        # A rejected durable callback or broken IPC is an unsuccessful attempt, not proof that
        # candidate processes survived. Return verified cleanup whenever possible; failed cleanup
        # deliberately raises so the caller retains its unresolved reservation.
        proofs = terminate([item for item in (worker, namespace) if item is not None])
        if process is not None:
            if worker is None and process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        return {
            "status": "timed_out" if time.monotonic() >= expires else "runtime_failed",
            "report": None,
            "reason": "executor wall-clock limit exhausted" if time.monotonic() >= expires else str(error)[:4096],
            "termination": {
                "worker": proofs[0] if proofs else None,
                "namespace": proofs[1] if len(proofs) > 1 else None,
            },
        }
    finally:
        try:
            if process is not None:
                if worker is not None:
                    terminate([item for item in (worker, namespace) if item is not None])
                elif process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        finally:
            channel.close()
            child_channel.close()
