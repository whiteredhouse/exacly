"""Exact process cleanup and worker gate checks require no optional sandbox runtime."""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from exacly import executor_runtime as runtime
from exacly import isolation


@pytest.fixture
def child() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_exact_process_cleanup(child: subprocess.Popen[bytes]) -> None:
    saved = runtime.process_identity(child.pid)
    assert runtime.alive(saved)
    proof = runtime.terminate([saved])
    assert proof[0]["identity"] == saved
    assert proof[0]["terminated"] is True
    assert not runtime.alive(saved)
    child.wait(timeout=5)
    assert runtime.terminate([saved])[0]["terminated"] is True


def test_reused_process_number_is_never_signalled(child: subprocess.Popen[bytes]) -> None:
    saved = runtime.process_identity(child.pid)
    saved["start_time"] += 1
    assert not runtime.alive(saved)
    assert runtime.terminate([saved])[0]["reason"] == "pid_reused"
    assert child.poll() is None


def test_previous_boot_cannot_signal_current_process(child: subprocess.Popen[bytes]) -> None:
    saved = runtime.process_identity(child.pid)
    saved["boot_id"] = "00000000-0000-0000-0000-000000000000"
    assert not runtime.alive(saved)
    assert runtime.terminate([saved])[0]["reason"] == "previous_boot"
    assert child.poll() is None


def test_changed_namespace_leaves_cleanup_unresolved(child: subprocess.Popen[bytes]) -> None:
    saved = runtime.process_identity(child.pid)
    saved["pid_namespace"] = "pid:[0]"
    with pytest.raises(runtime.RuntimeError, match="namespace changed"):
        runtime.terminate([saved])
    assert child.poll() is None


def test_cleanup_refuses_supervisor_process() -> None:
    saved = runtime.process_identity()
    assert saved["pid"] == os.getpid()
    with pytest.raises(runtime.RuntimeError, match="current supervisor"):
        runtime.terminate([saved])


@pytest.mark.parametrize("mutation", [{"pid": True}, {"start_time": -1}, {"extra": True}, {"pid_namespace": "net:[1]"}])
def test_retained_process_identity_is_strict(mutation: dict[str, Any]) -> None:
    saved = runtime.process_identity()
    saved.update(mutation)
    with pytest.raises(runtime.RuntimeError, match="invalid retained"):
        runtime.validate_identity(saved)


def test_cancelled_worker_is_never_released_to_runtime(tmp_path: Path) -> None:
    workers: list[dict[str, Any]] = []
    namespaces: list[dict[str, Any]] = []
    # Invalid runtime deliberately proves that a cancelled startup never reaches runtime setup.
    result = runtime.run(
        isolation.Runtime(tmp_path / "missing", "sha256:" + "0" * 64, "/usr/bin/python3"),
        ["/usr/bin/python3", "-c", "raise SystemExit(99)"],
        {},
        tmp_path / "out",
        timeout=10,
        on_worker=workers.append,
        on_namespace=namespaces.append,
        is_cancelled=lambda: True,
    )
    assert result["status"] == "cancelled"
    assert result["report"] is None
    assert result["termination"]["worker"]["terminated"] is True
    assert len(workers) == 1
    assert not runtime.alive(workers[0])
    assert namespaces == []
    assert not (tmp_path / "out").exists()


def test_worker_setup_error_retains_verified_cleanup(tmp_path: Path) -> None:
    workers: list[dict[str, Any]] = []
    result = runtime.run(
        isolation.Runtime(tmp_path / "missing", "sha256:" + "0" * 64, "/usr/bin/python3"),
        ["/usr/bin/python3", "-c", "raise SystemExit(99)"],
        {},
        tmp_path / "out",
        timeout=10,
        on_worker=workers.append,
        on_namespace=lambda _: pytest.fail("invalid runtime reached candidate release"),
    )
    assert result["status"] == "runtime_failed"
    assert result["report"] is None
    assert result["termination"]["worker"]["terminated"] is True
    assert not runtime.alive(workers[0])


def test_failed_durable_worker_callback_does_not_start_runtime(tmp_path: Path) -> None:
    workers: list[dict[str, Any]] = []

    def reject(saved: dict[str, Any]) -> None:
        workers.append(saved)
        raise ValueError("durable callback rejected")

    result = runtime.run(
        isolation.Runtime(tmp_path / "missing", "sha256:" + "0" * 64, "/usr/bin/python3"),
        ["/usr/bin/python3"],
        {},
        tmp_path / "out",
        timeout=10,
        on_worker=reject,
        on_namespace=lambda _: pytest.fail("rejected worker started candidate"),
    )
    assert result["status"] == "runtime_failed"
    assert result["reason"] == "durable callback rejected"
    assert result["termination"]["worker"]["terminated"] is True
    assert len(workers) == 1
    assert not runtime.alive(workers[0])
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("blocked_alarm", [False, True])
def test_worker_deadline_runs_while_waiting_for_startup_gate(blocked_alarm: bool) -> None:
    parent, child = socket.socketpair()
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM}) if blocked_alarm else None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(runtime.__file__).with_name("_executor_worker.py")),
                str(child.fileno()),
                str(os.getpid()),
                repr(time.monotonic() + 0.25),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(child.fileno(),),
        )
    finally:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    child.close()
    try:
        # No request or snapshot is supplied. Its independent timer bounds even this gate.
        assert select.select([parent], [], [], 3)[0]
        assert parent.recv(1) == b""
        process.wait(timeout=3)
        assert process.returncode in {-signal.SIGALRM, 124}
    finally:
        parent.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
