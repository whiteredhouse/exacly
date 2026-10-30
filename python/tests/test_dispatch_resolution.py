"""Explicit resolution retires stale coordination without rewriting current operator work."""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import shutil
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import dispatch, dispatch_store, execution_index, execution_store, planning, state_writer
from test_dispatch_faults import pause, paused_at, prepare_fault_case, remote_for

Doc = dict[str, Any]
REASON = "The approved model advanced; retain current work for separately reviewed registration."


def files(case: Scenario) -> tuple[bytes, bytes | None]:
    return case.state_path.read_bytes(), case.ledger.read_bytes() if case.ledger.exists() else None


@contextmanager
def stopped_registration(case: Scenario, *, binding_committed: bool = False) -> Iterator[str]:
    authorization = case.authorization
    with pytest.MonkeyPatch.context() as patched:
        if binding_committed:
            append = dispatch_store.Journal.append

            def interrupted_append(journal: dispatch_store.Journal, identity: str, phase: str, data: Doc) -> Doc:
                if phase == "registered":
                    raise ValueError("controlled interruption after binding commit")
                return append(journal, identity, phase, data)

            patched.setattr(dispatch_store.Journal, "append", interrupted_append)
        else:

            def interrupted_put(*args: Any, **kwargs: Any) -> Doc:
                raise ValueError("controlled interruption before binding commit")

            patched.setattr(execution_store, "put_locked", interrupted_put)
        report = dispatch.apply(
            case.journal,
            case.ledger,
            case.documents,
            case.state_path,
            case.request,
            authorization=authorization,
            client=case.client,
        )
        assert report["status"] == "conflict", report
    identity = report["dispatch_id"]
    assert dispatch.show(case.journal, identity)["record"]["phase"] == "registration_prepared"
    yield str(identity)


def advance(case: Scenario, *, config: bool = True) -> Doc:
    snapshot = state_writer.read(case.state_path)
    current = snapshot.document
    assert current is not None
    current["serial"] += 1
    current["written_at"] = "2026-10-30T10:01:00+00:00"
    current["x-operator-progress"] = {"retain": "unrelated observations and pending work"}
    if config:
        current["config_revision"] = "f" * 40
    state_writer.publish(snapshot, current)
    return current


def preview(case: Scenario, identity: str) -> Doc:
    return dict(dispatch.resolution_preview(case.journal, identity, reason=REASON))


def resolve(case: Scenario, identity: str, authorization: Doc) -> Doc:
    return dict(dispatch.resolve(case.journal, identity, authorization=authorization))


def test_resolution_preview_is_read_only_and_records_concrete_current_facts(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        current = advance(case)
        before = files(case), case.journal.read_bytes(), copy.deepcopy(case.client.calls)
        report = preview(case, identity)
        assert report["status"] == "ready" and report["dry_run"] is True
        approval = report["authorization_template"]
        assert approval["dispatch_id"] == identity
        assert approval["effect"] == "resolve_dispatch_coordination"
        assert approval["storage"] == case.policy["storage"]
        assert approval["history"]["phase"] == "registration_prepared"
        assert approval["state"]["header"]["config_revision"] == current["config_revision"]
        assert approval["state"]["digest"] == state_writer.read(case.state_path).digest
        assert approval["reason"] == REASON
        assert report["preconditions"]
        assert (files(case), case.journal.read_bytes(), case.client.calls) == before


@pytest.mark.parametrize("binding_committed", [False, True])
def test_resolution_preserves_current_work_and_retains_terminal_receipt(
    dispatch_scenario: Scenario, binding_committed: bool
) -> None:
    case = dispatch_scenario
    with stopped_registration(case, binding_committed=binding_committed) as identity:
        current = advance(case)
        original = dispatch.show(case.journal, identity)["record"]
        before = files(case), copy.deepcopy(case.client.calls), copy.deepcopy(case.client.issues)
        approval = preview(case, identity)["authorization_template"]
        result = resolve(case, identity, approval)
        assert result["status"] == "resolved", result
        assert (files(case), case.client.calls, case.client.issues) == before
        assert case.state == current
        retained = dispatch.show(case.journal, identity)["record"]
        assert retained["phase"] == "resolved"
        assert retained["events"][:-1] == original["events"]
        assert len(retained["events"]) == len(original["events"]) + 1
        assert result["receipt"]["claims"]["task_completion_authorized"] is False
        assert result["receipt"]["claims"]["verification_established"] is False
        assert case.client.calls.count("create") == 1


@pytest.mark.parametrize("phase", ["after_state_publish", "after_binding_commit"])
@pytest.mark.parametrize("version", [2, 4])
def test_sigkill_then_model_advance_can_be_explicitly_resolved_without_another_effect(
    dispatch_scenario: Scenario, tmp_path: Path, phase: str, version: int
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path, version)
    old_approval = case.authorization
    identity = old_approval["dispatch_id"]
    remote = remote_for(tmp_path)
    with paused_at(case, remote, phase):
        pass
    advance(case)
    before = files(case), copy.deepcopy(remote.tasks), remote.creates
    with pytest.raises(ValueError, match=r"model revision|planned contract"):
        dispatch.recover(
            case.journal,
            case.ledger,
            case.documents,
            case.state_path,
            case.request,
            authorization=old_approval,
            client=remote,
        )
    with pytest.raises(ValueError, match="recovery before superseding"):
        dispatch.supersede(case.journal, identity, reason=REASON, authorize=True)
    approval = preview(case, identity)["authorization_template"]
    report = resolve(case, identity, approval)
    assert report["status"] == "resolved", report
    assert (files(case), remote.tasks, remote.creates) == before
    assert remote.creates == 1
    assert dispatch.show(case.journal, identity)["record"]["phase"] == "resolved"


def test_resolution_can_read_a_known_stale_v4_profile_without_activating_or_writing_it(
    dispatch_scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path, 4)
    with stopped_registration(case, binding_committed=True) as identity:
        advance(case)
        installed = copy.deepcopy(execution_index.profile())
        installed["runtime"]["python"] += ".resolution-review"
        installed["digest"] = planning.digest({key: value for key, value in installed.items() if key != "digest"})
        monkeypatch.setattr(execution_index, "profile", lambda: copy.deepcopy(installed))
        before = files(case)
        approval = preview(case, identity)["authorization_template"]
        assert resolve(case, identity, approval)["status"] == "resolved"
        assert files(case) == before


def test_resolution_replay_is_immutable_even_after_later_state_advancement(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        first = resolve(case, identity, approval)
        advance(case)
        before = files(case), case.journal.read_bytes(), copy.deepcopy(case.client.calls)
        repeated = resolve(case, identity, approval)
        assert repeated["status"] == "resolved" and repeated["receipt"] == first["receipt"]
        assert (files(case), case.journal.read_bytes(), case.client.calls) == before
        changed = copy.deepcopy(approval)
        changed["reason"] = "Different authorization cannot replace the retained resolution."
        with pytest.raises(ValueError):
            resolve(case, identity, changed)
        assert case.journal.read_bytes() == before[1]


def test_first_resolution_records_the_reason_in_the_operator_reviewed_document(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        approval["reason"] = "Operator reviewed the captured facts and explicitly chose to retain all current work."
        report = resolve(case, identity, approval)
        assert report["status"] == "resolved"
        assert report["receipt"]["reason"] == approval["reason"]
        assert report["receipt"]["authorization"] == approval


@pytest.mark.parametrize("change", ["config", "pending", "removed_pending", "unrelated"])
def test_resolution_authorization_rejects_current_state_changes_after_review(
    dispatch_scenario: Scenario, change: str
) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        snapshot = state_writer.read(case.state_path)
        current = snapshot.document
        assert current is not None
        current["serial"] += 1
        if change == "config":
            current["config_revision"] = "e" * 40
        elif change == "pending":
            current["pending"][0]["task"] = "https://github.com/acme/demo/issues/other"
        elif change == "removed_pending":
            current.pop("pending")
        else:
            current["x-unrelated"] = "operator advancement after review"
        state_writer.publish(snapshot, current)
        before = files(case), case.journal.read_bytes()
        with pytest.raises(ValueError):
            resolve(case, identity, approval)
        assert (files(case), case.journal.read_bytes()) == before


@pytest.mark.parametrize("change", ["changed_pending", "removed_pending", "lineage"])
def test_reviewed_resolution_preserves_changed_or_missing_current_work(
    dispatch_scenario: Scenario, change: str
) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        current = advance(case)
        if change == "changed_pending":
            current["pending"][0]["task"] = "https://github.com/acme/demo/issues/reviewed-replacement"
        elif change == "removed_pending":
            current.pop("pending")
        else:
            current["lineage"] = "reviewed-new-lineage"
        current["serial"] += 1
        # A lineage change represents an explicit operator replacement of the current state;
        # resolution must acknowledge it, never restore the retained older snapshot over it.
        case.state_path.write_bytes(state_writer.serialize(current))
        before = files(case), copy.deepcopy(case.client.calls)
        proposal = preview(case, identity)
        assert proposal["preconditions"]
        assert resolve(case, identity, proposal["authorization_template"])["status"] == "resolved"
        assert (files(case), case.client.calls) == before
        assert case.state == current


def test_binding_commit_after_preview_invalidates_resolution_approval(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        record = dispatch.show(case.journal, identity)["record"]
        prepared = dispatch._event(record, "registration_prepared")
        assert prepared is not None
        execution_store.put(case.ledger, prepared["binding"])
        before = files(case), case.journal.read_bytes()
        with pytest.raises(ValueError):
            resolve(case, identity, approval)
        assert (files(case), case.journal.read_bytes()) == before


def test_retained_history_advancement_invalidates_resolution_approval(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        original = dispatch.show(case.journal, identity)["record"]
        prepared = dispatch._event(original, "registration_prepared")
        assert prepared is not None
        with dispatch_store.opened(case.journal, write=True) as journal:
            journal.append(identity, "registration_prepared", copy.deepcopy(prepared))
        before = files(case), case.journal.read_bytes()
        with pytest.raises(ValueError):
            resolve(case, identity, approval)
        assert (files(case), case.journal.read_bytes()) == before


def test_resolution_preserves_an_absent_current_state_without_recreating_it(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        case.state_path.unlink()
        proposal = preview(case, identity)
        assert proposal["authorization_template"]["state"]["exists"] is False
        assert resolve(case, identity, proposal["authorization_template"])["status"] == "resolved"
        assert not case.state_path.exists() and not case.ledger.exists()
        assert case.client.calls.count("create") == 1


def test_a_journal_copy_cannot_replay_another_storage_authorization(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        copied = case.journal.with_name("copied-journal.sqlite3")
        shutil.copyfile(case.journal, copied)
        copied.chmod(0o600)
        before = files(case), case.journal.read_bytes(), copied.read_bytes()
        with pytest.raises(ValueError):
            dispatch.resolve(copied, identity, authorization=approval)
        assert (files(case), case.journal.read_bytes(), copied.read_bytes()) == before


def test_resolved_coordination_never_resumes_original_registration(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        approval = preview(case, identity)["authorization_template"]
        result = resolve(case, identity, approval)
        before = files(case), case.journal.read_bytes(), copy.deepcopy(case.client.calls)
        replay = case.apply()
        assert replay["status"] == "resolved" and replay["receipt"] == result["receipt"]
        assert (files(case), case.journal.read_bytes(), case.client.calls) == before
        assert not case.ledger.exists()


@pytest.mark.parametrize("field", ["effect", "dispatch_id", "storage", "history", "reason"])
def test_resolution_requires_the_exact_reviewed_authorization(dispatch_scenario: Scenario, field: str) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        approval[field] = "" if field == "reason" else "not-the-reviewed-scope"
        before = files(case), case.journal.read_bytes()
        with pytest.raises(ValueError):
            resolve(case, identity, approval)
        assert (files(case), case.journal.read_bytes()) == before


def test_a_preview_report_is_not_resolution_authorization(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        proposal = preview(case, identity)
        before = files(case), case.journal.read_bytes()
        with pytest.raises(ValueError):
            resolve(case, identity, proposal)
        assert (files(case), case.journal.read_bytes()) == before


def interrupted_resolution(
    journal_path: Path, identity: str, authorization: Doc, phase: str, control: Connection
) -> None:
    transaction = dispatch_store.Journal._transaction
    append = dispatch_store.Journal.append

    @contextmanager
    def transaction_before_commit(journal: dispatch_store.Journal) -> Iterator[None]:
        journal.connection.execute("PRAGMA cache_size=1")
        journal.connection.execute("PRAGMA cache_spill=1")
        with transaction(journal):
            yield
            current = journal.connection.execute(
                "SELECT phase FROM dispatch_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if phase == "during_resolution" and current == ("resolved",):
                pause(control, phase)

    def append_then_pause(*args: Any, **kwargs: Any) -> Doc:
        result = append(*args, **kwargs)
        if phase == "after_resolution":
            pause(control, phase)
        return result

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(dispatch_store.Journal, "_transaction", transaction_before_commit)
            patched.setattr(dispatch_store.Journal, "append", append_then_pause)
            control.send(("unexpected-result", dispatch.resolve(journal_path, identity, authorization=authorization)))
    finally:
        control.close()


@contextmanager
def resolution_paused_at(case: Scenario, identity: str, authorization: Doc, phase: str) -> Iterator[BaseProcess]:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_resolution, args=(case.journal, identity, authorization, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), f"resolution did not reach {phase}"
        assert parent.recv() == ("paused", phase)
        yield process
    finally:
        if process.is_alive():
            assert process.pid is not None
            os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        parent.close()
        assert process.exitcode == -signal.SIGKILL


@pytest.mark.parametrize("phase", ["during_resolution", "after_resolution"])
def test_resolution_sigkill_recovers_without_stale_contract_finalization(
    dispatch_scenario: Scenario, phase: str
) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        before = files(case), copy.deepcopy(case.client.calls)
        interrupted_bytes: tuple[bytes, bytes] | None = None
        with resolution_paused_at(case, identity, approval, phase):
            if phase == "during_resolution":
                sidecar = Path(str(case.journal) + "-journal")
                assert sidecar.read_bytes()[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
                interrupted_bytes = case.journal.read_bytes(), sidecar.read_bytes()
        if phase == "during_resolution":
            assert interrupted_bytes is not None
            with pytest.raises(ValueError, match=r"sidecar|rollback|recovery"):
                preview(case, identity)
            with pytest.raises(ValueError):
                dispatch.recover_journal(case.journal)
            repaired = dispatch.recover_journal(case.journal, authorize=True)
            assert repaired["status"] == "repaired", repaired
            backup = Path(repaired["recovery_backup"])
            assert (backup / "database.sqlite3").read_bytes() == interrupted_bytes[0]
            assert (backup / "database.sqlite3-journal").read_bytes() == interrupted_bytes[1]
            assert dispatch.show(case.journal, identity)["record"]["phase"] == "registration_prepared"
        report = resolve(case, identity, approval)
        assert report["status"] == "resolved", report
        assert (files(case), case.client.calls) == before
        assert dispatch.show(case.journal, identity)["record"]["phase"] == "resolved"
        assert case.client.calls.count("create") == 1


def resolver_worker(journal: Path, identity: str, authorization: Doc, control: Connection) -> None:
    try:
        control.send("ready")
        control.recv()
        control.send(("result", dispatch.resolve(journal, identity, authorization=authorization)))
    except BaseException as error:
        control.send(("error", repr(error)))
        raise
    finally:
        control.close()


def test_concurrent_resolvers_append_one_terminal_receipt(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        approval = preview(case, identity)["authorization_template"]
        original = dispatch.show(case.journal, identity)["record"]
        before = files(case)
        context = multiprocessing.get_context("fork")
        workers: list[tuple[BaseProcess, Connection]] = []
        try:
            for _ in range(2):
                parent, child = context.Pipe()
                process: BaseProcess = context.Process(
                    target=resolver_worker, args=(case.journal, identity, approval, child)
                )
                process.start()
                child.close()
                workers.append((process, parent))
            for _, control in workers:
                assert control.poll(15) and control.recv() == "ready"
            for _, control in workers:
                control.send("start")
            reports = []
            for process, control in workers:
                assert control.poll(15), "resolution did not finish"
                message = control.recv()
                assert message[0] == "result", message
                reports.append(message[1])
                process.join(10)
                assert process.exitcode == 0
            assert reports[0]["status"] == reports[1]["status"] == "resolved"
            assert reports[0]["receipt"] == reports[1]["receipt"]
            retained = dispatch.show(case.journal, identity)["record"]
            assert retained["events"][:-1] == original["events"]
            assert len(retained["events"]) == len(original["events"]) + 1
            assert files(case) == before
        finally:
            for process, control in workers:
                if process.is_alive():
                    process.kill()
                process.join(10)
                control.close()


def test_resolution_receipt_identifies_retained_content_by_digest(dispatch_scenario: Scenario) -> None:
    case = dispatch_scenario
    with stopped_registration(case) as identity:
        advance(case)
        result = resolve(case, identity, preview(case, identity)["authorization_template"])
        receipt = result["receipt"]
        assert receipt["digest"] == planning.digest({key: value for key, value in receipt.items() if key != "digest"})
        assert result["digest"] == planning.digest({key: value for key, value in result.items() if key != "digest"})
        # Serialization itself remains ordinary JSON; exporting it does not make it an approval.
        assert json.loads(json.dumps(receipt)) == receipt
