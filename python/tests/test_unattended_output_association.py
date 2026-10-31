"""Native handoff/ledger association with controlled worker execution and real local storage.

These cases exercise no live remote producer, physical sandbox or unattended completion claim.
"""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import DATE
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import execution_store as store
from exacly import executions, planning, state_writer
from exacly import output_association as association
from handoff_fixtures import HandoffCase
from handoff_fixtures import handoff_case as handoff_case

Doc = dict[str, Any]


@dataclass
class Case:
    handoff: HandoffCase
    request: association.Request

    @property
    def storage(self) -> Doc:
        return dict(self.request.storage)

    @property
    def journal(self) -> Path:
        return Path(self.storage["journal"])

    def apply(self, **changes: Any) -> Doc:
        return association.apply(replace(self.request, **changes))

    def retained(self) -> tuple[str, Doc, str | None]:
        with sqlite3.connect(self.journal) as connection:
            row = connection.execute("SELECT id, intent, receipt FROM associations").fetchone()
            assert row is not None
            return row[0], json.loads(row[1]), row[2]

    def recover(self) -> Doc:
        return association.recover(self.storage, self.retained()[0])


@pytest.fixture
def case(handoff_case: HandoffCase, tmp_path: Path) -> Case:
    native = handoff_case.apply()
    assert native["status"] == "published"
    item = handoff_case.item
    private = tmp_path / "association-operator"
    private.mkdir(mode=0o700)
    storage = association.initialize(
        private / "association.sqlite3",
        ledger=item.ledger,
        state_path=item.state_path,
        handoff_journal=handoff_case.journal,
        handoff_journal_id=handoff_case.request.policy["storage"]["journal_id"],
    )
    request = association.Request(
        storage=storage,
        handoff_id=native["handoff_id"],
        handoff_receipt_digest=native["receipt"]["digest"],
        binding_id=item.request.binding_id,
        documents=item.documents,
        plan=item.request.plan,
        action_id=item.request.action_id,
        readiness_policy=item.request.readiness_policy,
        trust_policy=item.request.trust_policy,
        date=DATE,
    )
    return Case(handoff_case, request)


def test_exact_native_handoff_replaces_only_output_binding_and_retains_original_goal(case: Case) -> None:
    item = case.handoff.item
    before = item.state_path.read_bytes(), item.journal.read_bytes(), case.handoff.journal.read_bytes()
    original = executions.get(item.ledger, case.request.binding_id)["binding"]
    result = case.apply()
    assert result["status"] == "associated", result
    receipt = result["receipt"]
    native = executions.get(item.ledger, receipt["binding"])
    binding = native["binding"]
    assert native["active"] is True
    assert binding["supersedes"] == original["id"]
    assert binding["pending_key"] == original["pending_key"]
    for name in ("goal", "pending", "original_target", "plan", "policy", "trust_policy"):
        assert binding["contract"][name] == original["contract"][name]
    assert binding["contract"]["target"]["revision"] == case.handoff.preview()["receipt"]["commit"]
    assert binding["contract"]["target"]["revision"] != original["contract"]["target"]["revision"]
    assert receipt["claims"] == {"binding_associated": True, "evidence_admitted": False, "completion_authorized": False}
    assert before == (item.state_path.read_bytes(), item.journal.read_bytes(), case.handoff.journal.read_bytes())
    assert case.retained()[1]["replacement"] == binding
    assert receipt["digest"] == planning.digest({key: value for key, value in receipt.items() if key != "digest"})


def test_duplicate_calls_replay_without_new_registration_or_state_effect(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = case.apply()
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("historical replay must not rebuild or republish a registration")

    monkeypatch.setattr(association, "_prepare", forbidden)
    monkeypatch.setattr(store, "put_locked", forbidden)
    assert case.apply()["receipt"] == result["receipt"]
    assert case.recover()["receipt"] == result["receipt"]
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())


@pytest.mark.parametrize("field", ["journal_id", "handoff_journal_id"])
def test_replacement_storage_identity_cannot_reuse_approved_association(case: Case, field: str) -> None:
    storage = {**case.storage, field: "00112233445546778899aabbccddeeff"}
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError, match="instance"):
        case.apply(storage=storage)
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())


@pytest.mark.parametrize("field", ["handoff_id", "handoff_receipt_digest", "binding_id"])
def test_wrong_native_correlation_is_rejected_before_intent(case: Case, field: str) -> None:
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError):
        case.apply(**{field: "sha256:" + "f" * 64})
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())


def test_changed_model_is_rejected_before_association(case: Case) -> None:
    documents = copy.deepcopy(case.request.documents)
    record = documents[next(iter(documents))]["records"][0]
    record["description"] += " changed"
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError, match="approved model"):
        case.apply(documents=documents)
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())


def test_changed_policy_does_not_authorize_original_provenance(case: Case) -> None:
    policy = {**case.request.readiness_policy, "version": "unreviewed"}
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError, match="policy"):
        case.apply(readiness_policy=policy)
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())


def test_state_change_during_lock_acquisition_blocks_stale_snapshot(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = state_writer.read_locked

    def changed(snapshot: state_writer.Snapshot, parent_fd: int) -> state_writer.Snapshot:
        document = dict(snapshot.document or {})
        document["serial"] += 1
        document["pending"] = []
        snapshot.path.write_bytes(state_writer.serialize(document))
        return original(snapshot, parent_fd)

    monkeypatch.setattr(state_writer, "read_locked", changed)
    ledger = case.handoff.item.ledger.read_bytes()
    with pytest.raises(ValueError, match="pending"):
        case.apply()
    assert case.handoff.item.ledger.read_bytes() == ledger
    with sqlite3.connect(case.journal) as connection:
        assert connection.execute("SELECT count(*) FROM associations").fetchone() == (0,)


def test_stale_pending_state_is_rejected_without_native_replacement(case: Case) -> None:
    item = case.handoff.item
    snapshot = state_writer.read(item.state_path)
    state = dict(snapshot.document or {})
    state["serial"] += 1
    state["pending"] = []
    state_writer.publish(snapshot, state)
    before = case.journal.read_bytes(), item.authoritative_bytes()
    with pytest.raises(ValueError, match="pending"):
        case.apply()
    assert before == (case.journal.read_bytes(), item.authoritative_bytes())


def crash(case: Case, monkeypatch: pytest.MonkeyPatch, *, after: bool) -> None:
    original = store.put_locked

    def stop(*args: Any, **kwargs: Any) -> Doc:
        if after:
            original(*args, **kwargs)
        os._exit(73)

    with monkeypatch.context() as patch:
        patch.setattr(store, "put_locked", stop)
        process = multiprocessing.get_context("fork").Process(target=case.apply)
        process.start()
        process.join(10)
        assert process.exitcode == 73


@pytest.mark.parametrize("after", [False, True])
def test_process_exit_around_native_binding_effect_recovers_exact_prepared_record_once(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    after: bool,
) -> None:
    state = case.handoff.item.state_path.read_bytes()
    crash(case, monkeypatch, after=after)
    _, intent, receipt = case.retained()
    assert receipt is None
    replacement = intent["replacement"]
    recovered = case.recover()
    assert recovered["status"] == "associated", recovered
    assert recovered["receipt"]["binding"] == replacement["id"]
    native = executions.get(case.handoff.item.ledger, replacement["id"])
    assert native["binding"] == replacement and native["active"] is True
    assert case.recover()["receipt"] == recovered["receipt"]
    assert case.apply()["receipt"] == recovered["receipt"]
    assert case.handoff.item.state_path.read_bytes() == state
    with sqlite3.connect(case.handoff.item.ledger) as connection:
        assert connection.execute("SELECT count(*) FROM bindings").fetchone() == (2,)


def test_recovery_preserves_divergent_state_and_retained_intent(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    crash(case, monkeypatch, after=False)
    item = case.handoff.item
    snapshot = state_writer.read(item.state_path)
    state = dict(snapshot.document or {})
    state["serial"] += 1
    state["x-new-observation"] = "preserve"
    state_writer.publish(snapshot, state)
    before = case.journal.read_bytes(), item.authoritative_bytes()
    result = case.recover()
    assert result["status"] == "conflict", result
    assert before == (case.journal.read_bytes(), item.authoritative_bytes())
    assert executions.get(item.ledger, case.request.binding_id)["active"] is True


def test_recovery_does_not_overwrite_another_active_binding(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    crash(case, monkeypatch, after=False)
    item = case.handoff.item
    original = executions.get(item.ledger, case.request.binding_id)["binding"]
    pending = original["contract"]["pending"]
    other = executions.register(
        item.ledger,
        executions.Registration(
            case.request.plan,
            case.request.action_id,
            item.dispatch.state,
            pending["task"],
            pending["opened_at"],
            case.request.readiness_policy,
            case.request.trust_policy,
            item.request.target,
            DATE,
            supersedes=case.request.binding_id,
        ),
    )
    before = case.journal.read_bytes(), item.authoritative_bytes()
    assert case.recover()["status"] == "conflict"
    assert before == (case.journal.read_bytes(), item.authoritative_bytes())
    assert executions.get(item.ledger, other["id"])["active"] is True


def test_parallel_duplicate_schedules_coalesce_to_one_native_binding(case: Case) -> None:
    context = multiprocessing.get_context("fork")
    queue = context.Queue()

    def invoke() -> None:
        queue.put(case.apply())

    processes = [context.Process(target=invoke) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    results = [queue.get(timeout=2) for _ in processes]
    assert results[0]["receipt"] == results[1]["receipt"]
    with sqlite3.connect(case.journal) as connection:
        assert connection.execute("SELECT count(*) FROM associations").fetchone() == (1,)
    with sqlite3.connect(case.handoff.item.ledger) as connection:
        assert connection.execute("SELECT count(*) FROM bindings").fetchone() == (2,)


def test_journal_is_private_explicitly_initialized_and_not_recovered_implicitly(case: Case) -> None:
    assert case.journal.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        association.initialize(
            case.journal,
            ledger=case.handoff.item.ledger,
            state_path=case.handoff.item.state_path,
            handoff_journal=case.handoff.journal,
            handoff_journal_id=case.storage["handoff_journal_id"],
        )
    rollback = Path(str(case.journal) + "-journal")
    rollback.write_bytes(b"retain ambiguous storage")
    rollback.chmod(0o600)
    before = case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError, match="sidecars"):
        case.apply()
    assert case.handoff.item.authoritative_bytes() == before
    assert rollback.read_bytes() == b"retain ambiguous storage"


def test_identity_and_show_expose_prepared_intent_without_releasing_new_authority(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = association.identity(case.request)
    assert association.show(case.storage, identity)["status"] == "missing"
    crash(case, monkeypatch, after=False)
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()
    prepared = association.show(case.storage, identity)
    assert prepared["status"] == "prepared"
    assert prepared["intent"]["replacement"] == case.retained()[1]["replacement"]
    assert prepared["claims"]["binding_associated"] is False
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())
    recovered = case.recover()
    assert association.show(case.storage, identity)["receipt"] == recovered["receipt"]


def test_recovery_rechecks_initialized_native_handoff_before_unpublished_effect(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(case, monkeypatch, after=False)
    with sqlite3.connect(case.handoff.journal) as connection:
        connection.execute(
            "UPDATE handoff_journal SET value=? WHERE key='journal_id'", ("00112233445546778899aabbccddeeff",)
        )
    before = case.journal.read_bytes(), case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError, match="handoff journal instance"):
        case.recover()
    assert before == (case.journal.read_bytes(), case.handoff.item.authoritative_bytes())


def test_corrupt_retained_intent_cannot_publish_native_binding(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    crash(case, monkeypatch, after=False)
    identity, intent, _ = case.retained()
    intent["replacement"]["registered_at"] = "2026-10-30T00:00:00Z"
    with sqlite3.connect(case.journal) as connection:
        connection.execute("UPDATE associations SET intent=? WHERE id=?", (planning.canonical(intent), identity))
    before = case.handoff.item.authoritative_bytes()
    with pytest.raises(ValueError, match="intent identity"):
        case.recover()
    assert before == case.handoff.item.authoritative_bytes()


def test_candidate_artifact_storage_cannot_hold_authoritative_association_journal(case: Case) -> None:
    item = case.handoff.item
    directory = item.output_root / "candidate-authority"
    directory.mkdir(mode=0o700)
    storage = association.initialize(
        directory / "association.sqlite3",
        ledger=item.ledger,
        state_path=item.state_path,
        handoff_journal=case.handoff.journal,
        handoff_journal_id=case.storage["handoff_journal_id"],
    )
    before = item.authoritative_bytes()
    with pytest.raises(ValueError, match="artifact storage"):
        case.apply(storage=storage)
    assert before == item.authoritative_bytes()


def test_intent_directory_sync_precedes_native_effect_and_receipt_sync_precedes_success(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    original_sync, original_put = os.fsync, store.put_locked

    def sync(descriptor: int) -> None:
        info, parent = os.fstat(descriptor), case.journal.parent.stat()
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            events.append("association_directory_sync")
        original_sync(descriptor)

    def put(*args: Any, **kwargs: Any) -> Doc:
        events.append("native_binding_effect")
        return original_put(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(store, "put_locked", put)
    assert case.apply()["status"] == "associated"
    assert events == ["association_directory_sync", "native_binding_effect", "association_directory_sync"]


def test_failed_intent_directory_sync_blocks_native_effect_and_recovery_resynchronizes(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_sync = os.fsync
    before = case.handoff.item.authoritative_bytes()

    def fail(descriptor: int) -> None:
        info, parent = os.fstat(descriptor), case.journal.parent.stat()
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            raise OSError("injected association directory synchronization failure")
        original_sync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(OSError, match="synchronization failure"):
            case.apply()
    assert case.retained()[2] is None
    assert case.handoff.item.authoritative_bytes() == before
    assert case.recover()["status"] == "associated"


@pytest.mark.parametrize("replay", ["apply", "recover"])
def test_receipt_directory_sync_failure_requires_successful_resync_before_replay_success(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    replay: str,
) -> None:
    original_sync = os.fsync
    synchronizations = 0

    def fail_receipt(descriptor: int) -> None:
        nonlocal synchronizations
        info, parent = os.fstat(descriptor), case.journal.parent.stat()
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            synchronizations += 1
            if synchronizations > 1:
                raise OSError("injected receipt directory synchronization failure")
        original_sync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_receipt)
        with pytest.raises(OSError, match="receipt directory"):
            case.apply()
        assert case.retained()[2] is not None
        with pytest.raises(OSError, match="receipt directory"):
            getattr(case, replay)()
    before = case.handoff.item.authoritative_bytes()
    assert getattr(case, replay)()["status"] == "associated"
    assert case.handoff.item.authoritative_bytes() == before
