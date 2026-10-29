"""Exercise conditional state publication with real competing and interrupted processes."""

from __future__ import annotations

import copy
import fcntl
import json
import multiprocessing
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from exacly import state_writer
from exacly.cli import load
from exacly.refresh import refresh
from test_refresh import CI, RUN, config, requirement, valid_state

Doc = dict[str, Any]


def initial_state(label: str = "initial") -> Doc:
    documents = {"config": config(requirement("REQ-1", {**CI, "cases": ["t::case"]}))}
    state = refresh(documents, {"t::case": "passed"}, RUN)
    state["config_revision"] = label
    return valid_state(state)


def successor(previous: Doc, label: str) -> Doc:
    state = copy.deepcopy(previous)
    state["serial"] += 1
    state["config_revision"] = label
    return valid_state(state)


def competing_writer(path: Path, label: str, control: Connection) -> None:
    """Both processes must capture their expected state before either is allowed to publish."""
    try:
        snapshot = state_writer.read(path)
        previous = snapshot.document
        proposed = initial_state(label) if previous is None else successor(previous, label)
        control.send("ready")
        assert control.recv() == "publish"
        try:
            state_writer.publish(snapshot, proposed, lock_timeout=3.0)
        except state_writer.StateConflictError:
            control.send(("conflict", label))
        else:
            control.send(("published", label))
    except BaseException as error:
        control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@pytest.mark.parametrize("existing", [False, True])
def test_competing_processes_publish_only_one_successor(tmp_path: Path, existing: bool) -> None:
    path = tmp_path / "main.json"
    if existing:
        state_writer.publish(state_writer.read(path), initial_state())
    context = multiprocessing.get_context("fork")
    workers = []
    for label in ("first-writer", "second-writer"):
        parent, child = context.Pipe()
        process = context.Process(target=competing_writer, args=(path, label, child))
        process.start()
        child.close()
        workers.append((process, parent))
    try:
        for _, control in workers:
            assert control.poll(10), "writer did not capture its snapshot"
            assert control.recv() == "ready"
        for _, control in workers:
            control.send("publish")
        outcomes = []
        for process, control in workers:
            assert control.poll(10), "competing writer did not finish"
            outcomes.append(control.recv())
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(result[0] for result in outcomes) == ["conflict", "published"]
        winner = next(label for result, label in outcomes if result == "published")
        persisted = valid_state(json.loads(path.read_bytes()))
        assert persisted["config_revision"] == winner
        assert persisted["serial"] == (2 if existing else 1)
        assert sorted(item.name for item in tmp_path.iterdir()) == ["main.json"]
    finally:
        for process, control in workers:
            if process.is_alive():
                process.kill()
                process.join(timeout=10)
            control.close()


def test_same_identity_and_serial_cannot_hide_a_content_conflict(tmp_path: Path) -> None:
    path = tmp_path / "main.json"
    original = initial_state()
    state_writer.publish(state_writer.read(path), original)
    expected = state_writer.read(path)
    proposed = successor(original, "observed")
    externally_changed = copy.deepcopy(original)
    externally_changed["records"]["REQ-1"]["assessments"][0]["evidence"][0]["claim"] = "Updated evidence"
    external_bytes = (json.dumps(externally_changed, indent=2) + "\n").encode()
    path.write_bytes(external_bytes)
    with pytest.raises(state_writer.StateConflictError):
        state_writer.publish(expected, proposed)
    assert path.read_bytes() == external_bytes
    assert sorted(item.name for item in tmp_path.iterdir()) == ["main.json"]


def hold_parent_directory(path: Path, control: Connection) -> None:
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        control.send("locked")
        control.recv()
    finally:
        os.close(directory_fd)
        control.close()


@contextmanager
def held_parent(path: Path) -> Iterator[None]:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=hold_parent_directory, args=(path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "directory lock holder did not start"
        assert parent.recv() == "locked"
        yield
    finally:
        with suppress(BrokenPipeError, EOFError):
            parent.send("release")
        parent.close()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        assert process.exitcode == 0


@pytest.mark.parametrize("timeout", [0.0, 0.1])
def test_lock_wait_is_bounded_and_the_same_observation_can_retry_after_release(tmp_path: Path, timeout: float) -> None:
    path = tmp_path / "main.json"
    original = initial_state()
    state_writer.publish(state_writer.read(path), original)
    before = path.read_bytes()
    snapshot = state_writer.read(path)
    proposed = successor(original, "after-release")
    with held_parent(path):
        started = time.monotonic()
        with pytest.raises(state_writer.StateLockTimeoutError):
            state_writer.publish(snapshot, proposed, lock_timeout=timeout)
        assert time.monotonic() - started < timeout + 1.0
        assert path.read_bytes() == before
        assert sorted(item.name for item in tmp_path.iterdir()) == ["main.json"]
    state_writer.publish(snapshot, proposed, lock_timeout=0.0)
    assert json.loads(path.read_bytes()) == proposed


def interrupted_writer(path: Path, proposed: Doc, phase: str, control: Connection) -> None:
    """Stop immediately before/after the real atomic rename; the parent then sends SIGKILL."""
    original_replace = os.replace

    def replace(*args: Any, **kwargs: Any) -> None:
        if phase == "before_replace":
            control.send("ready")
            control.recv()
        original_replace(*args, **kwargs)
        if phase == "after_replace":
            control.send("ready")
            control.recv()

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "replace", replace)
            state_writer.publish(state_writer.read(path), proposed)
    except BaseException as error:
        control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@pytest.mark.parametrize("phase", ["before_replace", "after_replace"])
def test_killed_writer_leaves_complete_state_and_releases_the_shared_lock(tmp_path: Path, phase: str) -> None:
    path = tmp_path / "main.json"
    original = initial_state()
    state_writer.publish(state_writer.read(path), original)
    before = path.read_bytes()
    proposed = successor(original, "interrupted")
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_writer, args=(path, proposed, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "writer did not reach the atomic replacement boundary"
        assert parent.recv() == "ready"
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()
    survived = valid_state(json.loads(path.read_bytes()))
    if phase == "before_replace":
        assert path.read_bytes() == before
        assert survived == original
    else:
        assert survived == proposed
    # A killed stage can stay on disk, but it is hidden from model loading and cannot block
    # a new observation after the OS releases this process's directory lock.
    assert list(load([tmp_path])) == [str(path)]
    next_state = successor(survived, "fresh-observation")
    state_writer.publish(state_writer.read(path), next_state, lock_timeout=0.0)
    assert valid_state(json.loads(path.read_bytes())) == next_state
