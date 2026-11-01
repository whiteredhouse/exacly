"""Trusted Linux watchdog boundary for native controller invocations.

A gated subreaper owns exact pidfds, survives controller death and reaps detached descendants.
Component journals and separately retained supervisor cleanup receipts remain authoritative.
"""

from __future__ import annotations

import ctypes
import json
import os
import selectors
import signal
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from exacly import execution_store as store
from exacly import executor_runtime

Doc = dict[str, Any]


def _signal_child(pid: int) -> None:
    # The caller is its parent and never reaps it before opening/signaling the pidfd.
    with suppress(ProcessLookupError):
        descriptor = os.pidfd_open(pid)
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        finally:
            os.close(descriptor)


def _owned_children() -> list[int]:
    # Some kernels omit task/children. Parent IDs in proc stat provide the same kernel-owned relation.
    selected = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    try:
        return [int(value) for value in selected.read_text().split()]
    except FileNotFoundError:
        children: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                content = (entry / "stat").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            fields = content.rsplit(b") ", 1)[1].split()
            if int(fields[1]) == os.getpid():
                children.append(int(entry.name))
        return children


def _reap_owned_children(expires: float) -> bool:
    """A Linux subreaper kills/reaps only its adopted invocation descendants, including setsid."""
    while True:
        for identity in _owned_children():
            _signal_child(identity)
        while True:
            try:
                waited, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if waited == 0:
                break
        if time.monotonic() >= expires:
            return False
        time.sleep(0.001)


def _operation_child(
    operation: Callable[[], Doc],
    *,
    read_fd: int,
    write_fd: int,
    gate_read: int,
    gate_write: int,
    parent: int,
    expires: float,
) -> None:
    os.close(read_fd)
    os.close(gate_write)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        store.require(
            library.prctl(1, int(signal.SIGKILL), 0, 0, 0) == 0 and os.getppid() == parent,
            "native operation parent-death protection unavailable",
        )
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, expires - time.monotonic()))
        os.set_blocking(write_fd, True)
        os.write(write_fd, b"R")
        store.require(
            os.read(gate_read, 1) == b"G" and os.getppid() == parent and time.monotonic() < expires,
            "native operation startup gate was not authorized",
        )
        os.close(gate_read)
        try:
            content = store.canonical(operation()).encode()
        except BaseException:
            content = store.canonical(
                {"status": "uncertain", "reason": "Native child interrupted; reconcile its authoritative journal."}
            ).encode()
        offset = 0
        while offset < len(content):
            offset += os.write(write_fd, content[offset:])
        os.close(write_fd)
        os._exit(0)
    except BaseException:
        os._exit(79)


def _publish_child_result(descriptor: int, parent_fd: int, content: bytes, expires: float) -> bool:
    os.set_blocking(descriptor, False)
    offset = 0
    with selectors.DefaultSelector() as selected:
        selected.register(parent_fd, selectors.EVENT_READ, "parent")
        selected.register(descriptor, selectors.EVENT_WRITE, "pipe")
        while offset < len(content):
            remaining = expires - time.monotonic()
            if remaining <= 0:
                return False
            for key, _events in selected.select(remaining):
                if key.data == "parent":
                    return False
                with suppress(BlockingIOError):
                    offset += os.write(descriptor, content[offset:])
    return True


def _watch_native(
    operation: Callable[[], Doc],
    *,
    write_fd: int,
    gate_read: int,
    parent_fd: int,
    expires: float,
    duration: float,
    on_cleanup: Callable[[bool], None] | None,
) -> None:
    """Outlive the controller, own operation pidfd and reap the entire adopted trusted process tree."""
    operation_pid: int | None = None
    operation_fd: int | None = None
    operation_read: int | None = None
    operation_gate: int | None = None
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, expires - time.monotonic()))
        library = ctypes.CDLL(None, use_errno=True)
        store.require(library.prctl(36, 1, 0, 0, 0) == 0, "Linux invocation subreaper unavailable")
        _owned_children()  # Reject unavailable kernel ownership inspection before releasing any effect.
        operation_expires = expires - min(0.5, duration / 4)
        read_fd, child_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        child_gate, gate_write = os.pipe2(os.O_CLOEXEC)
        operation_pid = os.fork()
        if operation_pid == 0:
            os.close(write_fd)
            os.close(gate_read)
            _operation_child(
                operation,
                read_fd=read_fd,
                write_fd=child_write,
                gate_read=child_gate,
                gate_write=gate_write,
                parent=os.getppid(),
                expires=operation_expires,
            )
            os._exit(79)
        os.close(child_write)
        os.close(child_gate)
        operation_read, operation_gate = read_fd, gate_write
        operation_fd = os.pidfd_open(operation_pid)
        with selectors.DefaultSelector() as ready:
            ready.register(parent_fd, selectors.EVENT_READ, "parent")
            ready.register(read_fd, selectors.EVENT_READ, "operation")
            events = ready.select(max(0.0, operation_expires - time.monotonic()))
            store.require(
                any(key.data == "operation" for key, _ in events)
                and not any(key.data == "parent" for key, _ in events)
                and os.read(read_fd, 1) == b"R",
                "native child failed protected setup",
            )
            os.set_blocking(write_fd, True)
            os.write(write_fd, b"R")
            ready.unregister(read_fd)
            ready.register(gate_read, selectors.EVENT_READ, "gate")
            events = ready.select(max(0.0, operation_expires - time.monotonic()))
            store.require(
                any(key.data == "gate" for key, _ in events)
                and not any(key.data == "parent" for key, _ in events)
                and os.read(gate_read, 1) == b"G"
                and time.monotonic() < operation_expires,
                "controller did not authorize native operation release",
            )
            os.close(gate_read)
            gate_read = -1
            os.write(gate_write, b"G")
            os.close(gate_write)
            operation_gate = None
        content = bytearray()
        exit_status: int | None = None
        interrupted = False
        with selectors.DefaultSelector() as selected:
            selected.register(parent_fd, selectors.EVENT_READ, "parent")
            selected.register(read_fd, selectors.EVENT_READ, "result")
            while True:
                remaining = operation_expires - time.monotonic()
                if remaining <= 0:
                    interrupted = True
                    break
                for key, _events in selected.select(min(remaining, 0.05)):
                    if key.data == "parent":
                        interrupted = True
                        break
                    try:
                        block = os.read(read_fd, min(65536, store.RECORD_LIMIT + 1 - len(content)))
                    except BlockingIOError:
                        continue
                    if not block:
                        selected.unregister(read_fd)
                    content.extend(block)
                if interrupted or len(content) > store.RECORD_LIMIT:
                    interrupted = True
                    break
                if exit_status is None:
                    waited, status = os.waitpid(operation_pid, os.WNOHANG)
                    if waited:
                        exit_status = status
                if exit_status is not None and read_fd not in selected.get_map():
                    break
        cleaned = _reap_owned_children(expires)
        if on_cleanup is not None:
            on_cleanup(cleaned)
        if interrupted or exit_status != 0 or not cleaned or time.monotonic() >= expires:
            result = {
                "status": "uncertain",
                "reason": "Native deadline/result unavailable; retain reservation and reconcile exact journals.",
            }
        else:
            result = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        encoded = store.canonical(result).encode()
        _publish_child_result(write_fd, parent_fd, encoded, expires)
    except BaseException:
        cleaned = operation_pid is None
        if operation_pid is not None and operation_pid > 0:
            with suppress(OSError):
                if operation_fd is not None:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(operation_fd, signal.SIGKILL)
                else:
                    _signal_child(operation_pid)  # Setup has not yet performed any wait/reap.
                cleaned = _reap_owned_children(expires)
        if on_cleanup is not None:
            with suppress(BaseException):
                on_cleanup(cleaned)  # Gate/setup failures still reconcile their durable owner record.
    finally:
        for descriptor in (write_fd, gate_read, operation_fd, operation_read, operation_gate, parent_fd):
            if descriptor is not None and descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
        os._exit(0)


def bounded_native_call(
    operation: Callable[[], Doc],
    timeout: float,
    *,
    on_owner: Callable[[Doc], None] | None = None,
    on_cleanup: Callable[[bool], None] | None = None,
) -> Doc:
    """Gated Linux subreaper supervises all owned descendants through a fixed total deadline.

    The watchdog survives controller death and kills/reaps adopted descendants, including detached
    Git helpers. Cleanup shares the reserved ceiling. Unproved cleanup/result remains uncertain;
    only component journals may establish the native outcome, and no replacement is launched here.
    """
    store.require(
        type(timeout) in (int, float) and 0 < timeout <= 300,
        "native invocation requires a finite total deadline no greater than 300 seconds",
    )
    store.require(
        threading.active_count() == 1
        and hasattr(os, "fork")
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal"),
        "native deadlines require one Linux coordinator thread and pidfds",
    )
    parent_fd = os.pidfd_open(os.getpid())
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
    expires = time.monotonic() + timeout
    watcher: int | None = None
    watcher_fd: int | None = None
    exit_status: int | None = None
    try:
        watcher = os.fork()
        if watcher == 0:
            os.close(read_fd)
            os.close(gate_write)
            _watch_native(
                operation,
                write_fd=write_fd,
                gate_read=gate_read,
                parent_fd=parent_fd,
                expires=expires,
                duration=timeout,
                on_cleanup=on_cleanup,
            )
            os._exit(79)
        os.close(write_fd)
        write_fd = -1
        os.close(gate_read)
        gate_read = -1
        watcher_fd = os.pidfd_open(watcher)
        content = bytearray()
        with selectors.DefaultSelector() as selected:
            selected.register(read_fd, selectors.EVENT_READ)
            store.require(
                bool(selected.select(max(0.0, expires - time.monotonic()))) and os.read(read_fd, 1) == b"R",
                "native ownership/watchdog setup failed before effects",
            )
            store.require(time.monotonic() < expires, "native startup deadline exhausted before effects")
            if on_owner is not None:
                on_owner({"process": executor_runtime.identity(watcher), "deadline_monotonic": expires})
            store.require(time.monotonic() < expires, "native ownership persistence exceeded its deadline")
            os.write(gate_write, b"G")
            os.close(gate_write)
            gate_write = -1
            while time.monotonic() < expires:
                for _key, _events in selected.select(min(0.05, max(0.0, expires - time.monotonic()))):
                    try:
                        block = os.read(read_fd, min(65536, store.RECORD_LIMIT + 1 - len(content)))
                    except BlockingIOError:
                        continue
                    if not block:
                        selected.unregister(read_fd)
                    content.extend(block)
                if len(content) > store.RECORD_LIMIT:
                    break
                if exit_status is None:
                    waited, status = os.waitpid(watcher, os.WNOHANG)
                    if waited:
                        exit_status = status
                if exit_status is not None and not selected.get_map():
                    break
        if exit_status != 0 or time.monotonic() >= expires:
            return {
                "status": "uncertain",
                "reason": "Native watchdog outcome is unresolved; preserve reservation and reconcile.",
            }
        parsed = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(isinstance(parsed, dict), "native watchdog supplied no bounded outcome")
        return dict(parsed)
    finally:
        # Never kill the watchdog after release: it must outlive us to clean its owned descendants.
        if gate_write >= 0 and watcher is not None and watcher > 0:
            os.close(gate_write)
            gate_write = -1  # EOF vetoes the unreleased operation; watchdog performs cleanup.
        if watcher is not None and watcher > 0 and exit_status is None:
            with suppress(ChildProcessError):
                os.waitpid(watcher, os.WNOHANG)
        for descriptor in (read_fd, write_fd, gate_read, gate_write, watcher_fd, parent_fd):
            if descriptor is not None and descriptor >= 0:
                os.close(descriptor)
