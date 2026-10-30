"""Handoff recovery after each durable publication boundary preserves exact authority."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import handoff, handoff_git, handoff_store
from handoff_fixtures import HandoffCase, repository_bytes
from handoff_fixtures import handoff_case as handoff_case

Doc = dict[str, Any]


class Interrupted(RuntimeError):
    pass


def fail_boundary(monkeypatch: pytest.MonkeyPatch, boundary: str, *, kill: bool = False) -> None:
    def stop() -> None:
        if kill:
            os.kill(os.getpid(), signal.SIGKILL)
        raise Interrupted(boundary)

    if boundary == "after_prepare":
        original = handoff_store.Journal.prepare

        def prepare(self: handoff_store.Journal, *args: Any, **kwargs: Any) -> Doc:
            original(self, *args, **kwargs)
            stop()
            raise AssertionError("the interruption returned")

        monkeypatch.setattr(handoff_store.Journal, "prepare", prepare)
        return
    target = {
        "before_objects": "_install_objects",
        "after_objects": "_install_objects",
        "before_ref": "_publish_ref",
        "after_ref": "_publish_ref",
        "before_receipt": "_finalize",
        "after_receipt": "_finalize",
    }[boundary]
    original_operation = getattr(handoff, target)

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        if boundary.startswith("after"):
            original_operation(*args, **kwargs)
        stop()

    monkeypatch.setattr(handoff, target, interrupt)


@pytest.mark.parametrize(
    "boundary",
    ["after_prepare", "before_objects", "after_objects", "before_ref", "after_ref", "before_receipt", "after_receipt"],
)
def test_recovery_uses_prepared_bytes_after_original_artifacts_and_model_are_gone(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    case = handoff_case
    authorization = case.authorization
    authority = case.authoritative_bytes()
    with monkeypatch.context() as fault:
        fail_boundary(fault, boundary)
        with pytest.raises(Interrupted, match=boundary):
            case.apply(authorization)
    shown = handoff.show(case.journal, authorization["handoff_id"])
    assert shown["status"] == ("published" if boundary == "after_receipt" else "prepared"), shown
    assert shown["proposal"]["prepared"]["commit"] == authorization["commit"]
    assert "objects" not in shown["proposal"]["prepared"]
    assert case.authoritative_bytes() == authority
    shutil.rmtree(Path(case.execution["receipt"]["output"]))
    case.item.journal.unlink()
    case.item.documents.clear()
    recovered = handoff.recover(case.journal, authorization["handoff_id"])
    assert recovered["status"] == "published", recovered
    assert recovered["receipt"]["commit"] == authorization["commit"]
    assert handoff_git.read_ref(case.repository, case.request.ref) == authorization["commit"]
    final = case.journal.read_bytes(), repository_bytes(case.repository), case.item.authoritative_bytes()
    assert case.apply(authorization)["receipt"] == recovered["receipt"]
    assert handoff.recover(case.journal, authorization["handoff_id"])["receipt"] == recovered["receipt"]
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.item.authoritative_bytes()) == final


def test_recovery_preserves_a_third_ref_and_retains_unfinished_publication(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handoff_case
    authorization = case.authorization
    with monkeypatch.context() as fault:
        fail_boundary(fault, "before_ref")
        with pytest.raises(Interrupted):
            case.apply(authorization)
    # A different operator selected the base itself. The prepared authority expected absence.
    handoff_git.compare_and_swap(case.repository, case.request.ref, None, case.request.base_revision)
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    result = handoff.recover(case.journal, authorization["handoff_id"])
    assert result["status"] == "conflict" and result["receipt"] is None, result
    assert handoff.show(case.journal, authorization["handoff_id"])["status"] == "prepared"
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


def test_prepared_apply_replay_cannot_publish_without_explicit_recovery(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handoff_case
    authorization = case.authorization
    with monkeypatch.context() as fault:
        fail_boundary(fault, "before_objects")
        with pytest.raises(Interrupted):
            case.apply(authorization)
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    replay = case.apply(authorization)
    assert replay["status"] == "prepared" and replay["receipt"] is None, replay
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before
    assert handoff_git.read_ref(case.repository, case.request.ref) is None


@pytest.mark.parametrize("boundary", ["after_prepare", "after_ref", "after_receipt"])
def test_process_kill_preserves_exact_prepared_or_published_recovery(handoff_case: HandoffCase, boundary: str) -> None:
    case = handoff_case
    authorization = case.authorization
    authority = case.authoritative_bytes()

    def writer() -> None:
        patcher = pytest.MonkeyPatch()
        fail_boundary(patcher, boundary, kill=True)
        case.apply(authorization)

    process = multiprocessing.get_context("fork").Process(target=writer)
    process.start()
    try:
        process.join(timeout=20)
        assert not process.is_alive(), "the handoff writer did not reach its kill boundary"
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    assert case.authoritative_bytes() == authority
    shown = handoff.show(case.journal, authorization["handoff_id"])
    assert shown["status"] == ("published" if boundary == "after_receipt" else "prepared")
    shutil.rmtree(Path(case.execution["receipt"]["output"]))
    case.item.journal.unlink()
    case.item.documents.clear()
    result = handoff.recover(case.journal, authorization["handoff_id"])
    assert result["status"] == "published", result
    assert result["receipt"]["commit"] == authorization["commit"]
    assert handoff_git.read_ref(case.repository, case.request.ref) == authorization["commit"]
    assert case.apply(authorization)["receipt"] == result["receipt"]


def test_recovery_restores_unreachable_objects_from_durable_bytes_without_source_artifacts(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handoff_case
    authorization = case.authorization
    original = repository_bytes(case.repository)
    with monkeypatch.context() as fault:
        fail_boundary(fault, "after_objects")
        with pytest.raises(Interrupted):
            case.apply(authorization)
    introduced = set(repository_bytes(case.repository)) - set(original)
    assert introduced and all(name.startswith("objects/") for name in introduced)
    for name in introduced:
        (case.repository / name).unlink()
    shutil.rmtree(Path(case.execution["receipt"]["output"]))
    case.item.journal.unlink()
    result = handoff.recover(case.journal, authorization["handoff_id"])
    assert result["status"] == "published", result
    restored = repository_bytes(case.repository)
    assert introduced <= set(restored)
    assert handoff_git.read_ref(case.repository, case.request.ref) == authorization["commit"]


def test_ref_cas_race_leaves_the_other_writer_and_prepared_history_intact(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handoff_case
    authorization = case.authorization
    publish = handoff._publish_ref

    def race(repository: Path, ref: str, expected: str | None, commit: str, deadline: Any) -> None:
        handoff_git.compare_and_swap(repository, ref, expected, case.request.base_revision)
        publish(repository, ref, expected, commit, deadline)

    with monkeypatch.context() as fault:
        fault.setattr(handoff, "_publish_ref", race)
        with pytest.raises(ValueError):
            case.apply(authorization)
    assert handoff_git.read_ref(case.repository, case.request.ref) == case.request.base_revision
    assert handoff.show(case.journal, authorization["handoff_id"])["status"] == "prepared"
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    result = handoff.recover(case.journal, authorization["handoff_id"])
    assert result["status"] == "conflict" and result["receipt"] is None, result
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before
