"""Real controller process ownership/storage failures with controlled native effects."""

from __future__ import annotations

import datetime as dt
import multiprocessing
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from exacly import automation_controller as controller
from exacly import controller_store, planning
from test_unattended_controller import Case
from test_unattended_controller import case as case


def test_crash_after_native_success_recovers_receipt_before_any_new_effect(case: Case) -> None:
    def exit_after_effect() -> None:
        os._exit(73)

    case.authority.after_effect = exit_after_effect
    process = multiprocessing.get_context("fork").Process(target=case.step)
    process.start()
    process.join(10)
    assert process.exitcode == 73
    event = case.show()["event"]
    assert event["phase"] == "execute" and event["pending"]
    effect_id = event["pending"]["effect_id"]
    case.authority.after_effect = None
    recovered = case.step()
    assert recovered["event"]["phase"] == "handoff"
    assert case.authority.recoveries == [effect_id]
    assert case.authority.effects == []
    assert len(list(case.authority.directory.iterdir())) == 1


def test_crash_before_authority_invocation_reuses_same_durable_phase_identity(case: Case) -> None:
    def exit_before_effect() -> None:
        os._exit(74)

    case.authority.before_effect = exit_before_effect
    process = multiprocessing.get_context("fork").Process(target=case.step)
    process.start()
    process.join(10)
    assert process.exitcode == 74
    pending = case.show()["event"]["pending"]
    assert list(case.authority.directory.iterdir()) == []
    case.authority.before_effect = None
    result = case.step()
    assert result["event"]["phase"] == "handoff"
    assert case.authority.recoveries == case.authority.effects == [pending["effect_id"]]


def test_duplicate_live_schedule_observes_invocation_owner_without_new_effect(case: Case) -> None:
    context = multiprocessing.get_context("fork")
    started, release = context.Event(), context.Event()

    def hold_effect() -> None:
        started.set()
        assert release.wait(10)

    case.authority.before_effect = hold_effect
    process = context.Process(target=case.step)
    process.start()
    assert started.wait(5)
    try:
        assert case.step()["status"] == "owned"
        assert list(case.authority.directory.iterdir()) == []
        controller.cancel(case.journal, case.journal_id, reason="Cancel active invocation.")
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0
    assert case.show()["event"]["status"] == "cancelled"
    assert len(list(case.authority.directory.iterdir())) == 1


def test_cancelled_uncertain_effect_can_reconcile_history_without_new_authority(case: Case) -> None:
    def cancelled_after_native() -> None:
        os._exit(75)

    case.authority.after_effect = cancelled_after_native
    process = multiprocessing.get_context("fork").Process(target=case.step)
    process.start()
    process.join(10)
    assert process.exitcode == 75
    controller.cancel(case.journal, case.journal_id, reason="Preserve uncertain effect and stop.")
    case.authority.after_effect = None
    case.authority.available = False
    result = case.recover()
    assert result["event"]["status"] == "cancelled"
    assert result["event"]["pending"] is None
    assert "execute" in result["event"]["correlations"]
    assert case.authority.effects == []


def test_revoked_unsent_recovery_does_not_invoke_new_effect(case: Case) -> None:
    def stop() -> None:
        os._exit(76)

    case.authority.before_effect = stop
    process = multiprocessing.get_context("fork").Process(target=case.step)
    process.start()
    process.join(10)
    assert process.exitcode == 76
    case.authority.before_effect = None
    case.authority.available = False
    assert case.step()["event"]["status"] == "paused"
    assert not case.authority.effects
    assert case.show()["event"]["pending"]


def test_uncertain_effect_reservation_survives_backoff_and_deadline(case: Case) -> None:
    case.authority.result = "uncertain"
    result = case.step()
    assert result["event"]["status"] == "recovering"
    pending = result["event"]["pending"]
    case.current += dt.timedelta(hours=2)
    # The controlled fake proves absence. Expired controller still cannot resend.
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert result["event"]["pending"] == pending
    assert len(case.authority.effects) == 1


def test_changed_host_boot_and_journal_instance_fail_closed(case: Case) -> None:
    with pytest.raises(ValueError, match="instance"):
        controller.show(case.journal, "00112233445546778899aabbccddeeff")
    original = case.workflow.contract["host"]["boot_id"]
    case.workflow.contract["host"]["boot_id"] = "another-runner"
    with pytest.raises(ValueError, match="contract changed"):
        case.step()
    case.workflow.contract["host"]["boot_id"] = original
    assert not case.authority.effects


def test_hot_journal_is_preserved_and_never_silently_recovered(case: Case) -> None:
    sidecar = Path(str(case.journal) + "-journal")
    sidecar.write_bytes(b"retain for explicit recovery")
    sidecar.chmod(0o600)
    before = case.journal.read_bytes()
    with pytest.raises(ValueError, match="sidecars"):
        case.step()
    assert case.journal.read_bytes() == before
    assert sidecar.read_bytes() == b"retain for explicit recovery"
    assert not case.authority.effects


def test_history_corruption_cannot_release_a_new_effect(case: Case) -> None:
    with sqlite3.connect(case.journal) as connection:
        connection.execute("UPDATE events SET digest=? WHERE sequence=0", (planning.digest("corrupt"),))
    with pytest.raises(ValueError, match="event chain"):
        case.step()
    assert not case.authority.effects


def test_failed_phase_directory_barrier_prevents_authority_invocation(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync

    def fail(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == case.journal.parent.stat().st_ino:
            raise OSError("injected phase directory barrier failure")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(OSError, match="phase directory"):
            case.step()
    assert not case.authority.effects
    assert case.show()["event"]["pending"]
    # Recovery consults native authority before any eventual invocation of this retained request.
    assert case.step()["event"]["phase"] == "handoff"
    assert len(case.authority.recoveries) == len(case.authority.effects) == 1


def test_private_initialization_and_closed_session_do_not_recreate_storage(case: Case) -> None:
    assert case.journal.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        controller.initialize(case.journal, case.workflow.contract)
    with controller_store.opened(case.journal, case.journal_id, write=False) as journal:
        pass
    with pytest.raises(ValueError, match="scope ended"):
        journal.read()
    missing = case.journal.parent / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        controller.show(missing, case.journal_id)
    assert not missing.exists()


def test_cancellation_before_delegated_release_prevents_authorizer_call(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = controller._release

    def cancel_before_release(journal: Path, journal_id: str, event: dict[str, Any]) -> dict[str, Any]:
        controller.cancel(journal, journal_id, reason="Cancel before delegated release.")
        return original(journal, journal_id, event)

    monkeypatch.setattr(controller, "_release", cancel_before_release)
    result = case.step()
    assert result["event"]["status"] == "cancelled"
    assert result["event"]["pending"].get("released") is not True
    assert not case.authority.effects


def test_real_owned_native_child_enforces_total_deadline_before_late_effect(tmp_path: Path) -> None:
    import time

    effect = tmp_path / "must-not-publish"
    started = tmp_path / "operation-started"

    def late_effect() -> dict[str, Any]:
        started.write_text("released inside owned child")
        time.sleep(1)
        effect.write_text("late unauthorized result")
        return {"status": "succeeded", "receipt": {"effect": str(effect)}}

    result = controller.bounded_native_call(late_effect, 0.5)
    assert result["status"] == "uncertain" and started.exists()
    time.sleep(1.1)
    assert not effect.exists()


def test_real_owned_native_child_returns_only_bounded_terminated_result() -> None:
    result = controller.bounded_native_call(lambda: {"status": "succeeded", "receipt": {"exact": True}}, 1)
    assert result == {"status": "succeeded", "receipt": {"exact": True}}


def test_real_owned_native_child_exception_preserves_uncertain_outcome() -> None:
    def interrupted() -> dict[str, Any]:
        raise OSError("trusted operation lost response")

    assert controller.bounded_native_call(interrupted, 1)["status"] == "uncertain"


def test_native_pidfd_setup_failure_cannot_release_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    marker = tmp_path / "unreleased-effect"
    original = os.pidfd_open
    parent = os.getpid()

    def reject_child(pid: int, flags: int = 0) -> int:
        if os.getpid() == parent and pid != parent:
            raise OSError("injected child pidfd ownership failure")
        return original(pid, flags)

    def effect() -> dict[str, Any]:
        marker.write_text("must not run")
        return {"status": "succeeded", "receipt": {"effect": True}}

    with monkeypatch.context() as patch:
        patch.setattr(os, "pidfd_open", reject_child)
        with pytest.raises(OSError, match="pidfd ownership"):
            controller.bounded_native_call(effect, 1)
    time.sleep(0.1)
    assert not marker.exists()


@pytest.mark.parametrize("parent_state", ["killed", "stopped"])
@pytest.mark.parametrize("detached", ["plain", "setsid", "double-fork"])
def test_native_watchdog_stops_owned_git_style_descendants_when_supervisor_is_unavailable(
    tmp_path: Path,
    parent_state: str,
    detached: str,
) -> None:
    import signal
    import subprocess
    import sys
    import time

    context = multiprocessing.get_context("fork")
    started = context.Event()
    marker = tmp_path / "late-descendant-publication"
    script = (
        "import time; from pathlib import Path; time.sleep(0.4); Path(" + repr(str(marker)) + ").write_text('late')"
    )

    if detached == "double-fork":
        script = "import os\nif os.fork(): os._exit(0)\nos.setsid()\nif os.fork(): os._exit(0)\n" + script

    def operation() -> dict[str, Any]:
        subprocess.Popen([sys.executable, "-c", script], start_new_session=detached == "setsid")
        started.set()
        time.sleep(2)
        return {"status": "succeeded", "receipt": {"effect": True}}

    def supervise() -> None:
        controller.bounded_native_call(operation, 0.2 if parent_state == "stopped" else 1)

    process = context.Process(target=supervise)
    process.start()
    assert started.wait(5)
    assert process.pid is not None
    os.kill(process.pid, signal.SIGKILL if parent_state == "killed" else signal.SIGSTOP)
    try:
        time.sleep(0.6)
        assert not marker.exists()
    finally:
        if parent_state == "stopped":
            os.kill(process.pid, signal.SIGCONT)
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode == (-signal.SIGKILL if parent_state == "killed" else 0)


def anchor_resources(tmp_path: Path) -> tuple[Path, Path]:
    """Separate private native/companion resources, without manufacturing pending work or state."""
    native = tmp_path / "native-ledger"
    companion = tmp_path / "ledger-anchor"
    native.mkdir(mode=0o700)
    companion.mkdir(mode=0o700)
    return companion / "anchor.json", native / "ledger.sqlite3"


def test_explicit_anchor_bootstrap_creates_only_empty_native_v2_ledger(tmp_path: Path) -> None:
    from exacly import completion_store, execution_store

    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    assert anchored["ledger"] == str(ledger)
    assert anchored["anchor_path"] == str(anchor_path)
    assert anchored["digest"] == planning.digest({key: value for key, value in anchored.items() if key != "digest"})
    assert controller_store.check_ledger_anchor(anchored) == anchored
    assert anchor_path.stat().st_mode & 0o777 == ledger.stat().st_mode & 0o777 == 0o600
    with (
        execution_store.locked(ledger, write=False) as (absolute, _),
        execution_store.database(absolute, create=False) as (connection, _created),
    ):
        assert connection.execute("PRAGMA user_version").fetchone() == (execution_store.COMPLETION_VERSION,)
        assert execution_store.validate(connection) == ({}, {}, {})
        completion_store.require_v2(connection)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["ledger-anchor", "native-ledger"]


def test_anchor_of_existing_native_ledger_does_not_change_ledger_bytes(tmp_path: Path) -> None:
    from test_completion import prepare_scenario

    native = prepare_scenario(tmp_path)
    companion = tmp_path / "ledger-anchor"
    companion.mkdir(mode=0o700)
    before = native.ledger.read_bytes(), native.state_path.read_bytes()
    anchored = controller_store.initialize_ledger_anchor(companion / "anchor.json", ledger=native.ledger)
    assert controller_store.check_ledger_anchor(anchored) == anchored
    assert before == (native.ledger.read_bytes(), native.state_path.read_bytes())


def test_anchor_accepts_native_in_place_binding_registration_without_reinitialization(tmp_path: Path) -> None:
    from exacly import executions
    from exacly.applicability import Target
    from test_completion import prepare_scenario

    native = prepare_scenario(tmp_path)
    companion = tmp_path / "ledger-anchor"
    companion.mkdir(mode=0o700)
    anchored = controller_store.initialize_ledger_anchor(companion / "anchor.json", ledger=native.ledger)
    original = native.binding
    pending = original["contract"]["pending"]
    replacement = executions.register(
        native.ledger,
        executions.Registration(
            native.plan,
            native.plan["actions"][0]["id"],
            native.state,
            pending["task"],
            pending["opened_at"],
            native.readiness,
            native.source.policy,
            Target(original["contract"]["target"]),
            dt.date.fromisoformat(original["as_of"]),
            supersedes=original["id"],
        ),
    )
    assert replacement["supersedes"] == original["id"]
    assert controller_store.check_ledger_anchor(anchored) == anchored


def test_anchor_initialization_never_replaces_existing_companion_or_native_file(tmp_path: Path) -> None:
    anchor_path, ledger = anchor_resources(tmp_path)
    controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    before = anchor_path.read_bytes(), ledger.read_bytes()
    with pytest.raises(ValueError, match="new destination"):
        controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    assert before == (anchor_path.read_bytes(), ledger.read_bytes())
    another = anchor_path.parent / "another.json"
    with pytest.raises(ValueError, match="new destination"):
        controller_store.initialize_ledger_anchor(another, ledger=ledger, create_ledger=True)
    assert not another.exists()
    assert before == (anchor_path.read_bytes(), ledger.read_bytes())


def test_missing_native_ledger_is_not_initialized_without_explicit_selection(tmp_path: Path) -> None:
    anchor_path, ledger = anchor_resources(tmp_path)
    with pytest.raises((ValueError, FileNotFoundError)):
        controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger)
    assert not ledger.exists() and not anchor_path.exists()


def test_legacy_v1_ledger_is_rejected_without_implicit_upgrade(tmp_path: Path) -> None:
    from ledger_store_fixtures import initialize_legacy_ledger

    anchor_path, ledger = anchor_resources(tmp_path)
    initialize_legacy_ledger(ledger)
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="completion-ready"):
        controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger)
    assert ledger.read_bytes() == before
    assert not anchor_path.exists()


def test_replaced_native_ledger_cannot_reuse_existing_anchor(tmp_path: Path) -> None:
    import shutil

    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    replacement = ledger.parent / "replacement.sqlite3"
    shutil.copyfile(ledger, replacement)
    replacement.chmod(0o600)
    os.replace(replacement, ledger)
    with pytest.raises(ValueError, match="physical identity"):
        controller_store.check_ledger_anchor(anchored)
    assert anchor_path.read_bytes() == planning.canonical(anchored).encode()


@pytest.mark.parametrize("resource", ["ledger", "anchor_path"])
def test_anchor_rejects_symlink_replacement_without_following_it(tmp_path: Path, resource: str) -> None:
    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    selected = Path(anchored[resource])
    retained = selected.with_name("retained-original")
    selected.rename(retained)
    selected.symlink_to(retained)
    with pytest.raises((ValueError, OSError)):
        controller_store.check_ledger_anchor(anchored)
    assert selected.is_symlink()


def test_anchor_copy_cannot_select_a_different_companion_path(tmp_path: Path) -> None:
    import shutil

    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    copied = anchor_path.with_name("copied.json")
    shutil.copyfile(anchor_path, copied)
    copied.chmod(0o600)
    changed = {**anchored, "anchor_path": str(copied)}
    changed["digest"] = planning.digest({key: value for key, value in changed.items() if key != "digest"})
    with pytest.raises(ValueError, match="anchor content changed"):
        controller_store.check_ledger_anchor(changed)
    assert controller_store.check_ledger_anchor(anchored) == anchored


def test_changed_anchor_content_cannot_replace_the_independently_pinned_identity(tmp_path: Path) -> None:
    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    changed = {**anchored, "instance_id": "00112233445546778899aabbccddeeff"}
    changed["digest"] = planning.digest({key: value for key, value in changed.items() if key != "digest"})
    anchor_path.write_text(planning.canonical(changed))
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="anchor content changed"):
        controller_store.check_ledger_anchor(anchored)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("resource", ["ledger", "anchor_path"])
def test_anchor_rejects_unsafe_or_shared_authority_files(tmp_path: Path, resource: str) -> None:
    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    selected = Path(anchored[resource])
    selected.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        controller_store.check_ledger_anchor(anchored)
    selected.chmod(0o600)
    os.link(selected, selected.with_name("shared"))
    with pytest.raises(ValueError, match="single-link"):
        controller_store.check_ledger_anchor(anchored)


def test_anchor_rejects_changed_native_schema_and_preserves_uncertain_sidecars(tmp_path: Path) -> None:
    anchor_path, ledger = anchor_resources(tmp_path)
    anchored = controller_store.initialize_ledger_anchor(anchor_path, ledger=ledger, create_ledger=True)
    with sqlite3.connect(ledger) as connection:
        connection.execute("ALTER TABLE bindings ADD COLUMN unapproved TEXT")
    with pytest.raises(ValueError, match="schema"):
        controller_store.check_ledger_anchor(anchored)
    sidecar = Path(str(ledger) + "-journal")
    sidecar.write_bytes(b"retain interrupted native database")
    sidecar.chmod(0o600)
    with pytest.raises(ValueError, match="sidecars"):
        controller_store.check_ledger_anchor(anchored)
    assert sidecar.read_bytes() == b"retain interrupted native database"


def test_large_native_result_cannot_hold_watchdog_past_deadline_when_controller_is_stopped(tmp_path: Path) -> None:
    import select
    import signal
    import time

    context = multiprocessing.get_context("fork")
    started = context.Event()
    watcher_file = tmp_path / "exact-watchdog-pid"

    def large_result() -> dict[str, Any]:
        watcher_file.write_text(str(os.getppid()))
        started.set()
        time.sleep(0.1)
        return {"status": "succeeded", "receipt": {"large": "x" * (2 * 1024 * 1024)}}

    def supervise() -> None:
        result = controller.bounded_native_call(large_result, 0.6)
        assert result["status"] == "uncertain"

    process = context.Process(target=supervise)
    process.start()
    assert started.wait(5) and process.pid is not None
    os.kill(process.pid, signal.SIGSTOP)
    try:
        time.sleep(0.9)
        watcher = int(watcher_file.read_text())
        descriptor = os.pidfd_open(watcher)
        try:
            poll = select.poll()
            poll.register(descriptor, select.POLLIN)
            assert poll.poll(0), "watchdog retained ownership beyond its total deadline"
        finally:
            os.close(descriptor)
    finally:
        os.kill(process.pid, signal.SIGCONT)
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode == 0


def test_owner_persistence_failure_before_gate_retains_proved_cleanup(tmp_path: Path) -> None:
    import json
    import time

    owner_file, cleanup_file, effect_file = (tmp_path / name for name in ("owner.json", "cleanup.json", "effect"))

    def owner(identity: dict[str, Any]) -> None:
        owner_file.write_text(json.dumps(identity))
        raise OSError("injected failure after durable owner record")

    def cleanup(complete: bool) -> None:
        cleanup_file.write_text(json.dumps({"complete": complete}))

    def effect() -> dict[str, Any]:
        effect_file.write_text("must never start")
        return {"status": "succeeded", "receipt": {"effect": True}}

    with pytest.raises(OSError, match="after durable owner"):
        controller.bounded_native_call(effect, 1, on_owner=owner, on_cleanup=cleanup)
    for _ in range(100):
        if cleanup_file.exists():
            break
        time.sleep(0.01)
    assert owner_file.exists()
    assert json.loads(cleanup_file.read_text()) == {"complete": True}
    assert not effect_file.exists()


def test_controller_death_after_owner_persistence_before_gate_has_durable_cleanup(tmp_path: Path) -> None:
    import json
    import signal
    import time

    context = multiprocessing.get_context("fork")
    owner_file, cleanup_file, effect_file = (tmp_path / name for name in ("owner.json", "cleanup.json", "effect"))
    persisted = context.Event()

    def owner(identity: dict[str, Any]) -> None:
        owner_file.write_text(json.dumps(identity))
        persisted.set()
        time.sleep(2)

    def cleanup(complete: bool) -> None:
        cleanup_file.write_text(json.dumps({"complete": complete}))

    def effect() -> dict[str, Any]:
        effect_file.write_text("must never start")
        return {"status": "succeeded", "receipt": {"effect": True}}

    def supervise() -> None:
        controller.bounded_native_call(effect, 1, on_owner=owner, on_cleanup=cleanup)

    process = context.Process(target=supervise)
    process.start()
    assert persisted.wait(5) and process.pid is not None
    os.kill(process.pid, signal.SIGKILL)
    process.join(5)
    assert process.exitcode == -signal.SIGKILL
    for _ in range(100):
        if cleanup_file.exists():
            break
        time.sleep(0.01)
    assert owner_file.exists()
    assert json.loads(cleanup_file.read_text()) == {"complete": True}
    assert not effect_file.exists()
