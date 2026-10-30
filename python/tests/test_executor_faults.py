"""Real supervisor death retains budget and cannot duplicate an already published build."""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import pytest
from exacly import executions, executor, executor_runtime, executor_store, state_writer
from executor_fixtures import ExecutorScenario
from executor_fixtures import dispatch_scenario as dispatch_scenario
from executor_fixtures import executor_scenario as executor_scenario
from executor_fixtures import isolation_runtime as isolation_runtime

Doc = dict[str, Any]


def _pause(control: Connection, phase: str) -> None:
    control.send(("paused", phase))
    control.recv()


def _supervise(item: ExecutorScenario, authorization: Doc, phase: str, control: Connection) -> None:
    reserve = executor_store.Journal.reserve
    append = executor_store.Journal.append
    run = executor_runtime.run

    def reserve_then_pause(*args: Any, **kwargs: Any) -> Doc:
        result = reserve(*args, **kwargs)
        if phase == "reserved":
            _pause(control, phase)
        return result

    def append_then_pause(*args: Any, **kwargs: Any) -> Doc:
        result = append(*args, **kwargs)
        selected_phase = args[3] if len(args) > 3 else kwargs.get("phase")
        if phase == "terminal_receipt" and selected_phase in executor_store.TERMINAL:
            _pause(control, phase)
        return result

    def supervised(*args: Any, **kwargs: Any) -> Doc:
        for key in ("on_worker", "on_namespace"):
            callback = kwargs[key]

            def retained(identity: Doc, callback: Any = callback, key: str = key) -> None:
                callback(identity)
                if phase == key:
                    _pause(control, phase)

            kwargs[key] = retained
        result = run(*args, **kwargs)
        if phase == "bundle_published":
            assert result["report"] is not None, result
            _pause(control, phase)
        return result

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(executor_store.Journal, "reserve", reserve_then_pause)
            patched.setattr(executor_store.Journal, "append", append_then_pause)
            patched.setattr(executor_runtime, "run", supervised)
            result = item.apply(authorization=authorization)
            control.send(("result", result))
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError):
            control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@contextmanager
def paused_at(
    item: ExecutorScenario, phase: str, *, expect_kill: bool = True
) -> Iterator[tuple[BaseProcess, Connection, Doc]]:
    authorization = item.authorization
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_supervise, args=(item, authorization, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30), f"supervisor did not reach {phase}"
        message = parent.recv()
        assert message == ("paused", phase), message
        yield process, parent, authorization
    finally:
        if not expect_kill:
            process.join(timeout=10)
        if process.is_alive():
            assert process.pid is not None
            os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        parent.close()
        assert process.exitcode == (-signal.SIGKILL if expect_kill else 0)


def _identities(value: Any) -> list[Doc]:
    result: list[Doc] = []
    if isinstance(value, dict):
        if set(value) == {"boot_id", "pid", "start_time", "pid_namespace"}:
            result.append(value)
        else:
            for nested in value.values():
                result.extend(_identities(nested))
    elif isinstance(value, list):
        for nested in value:
            result.extend(_identities(nested))
    return result


def _wait_for_candidate(item: ExecutorScenario, execution_id: str, process: BaseProcess, channel: Connection) -> Doc:
    expires = time.monotonic() + 30
    while time.monotonic() < expires:
        assert process.is_alive(), channel.recv() if channel.poll() else process.exitcode
        try:
            shown = executor.show(item.journal, execution_id)
        except ValueError as error:
            assert "does not exist" in str(error)
            time.sleep(0.02)
            continue
        owner_namespace = executor_runtime.process_identity()["pid_namespace"]
        for identity in _identities(shown["history"]):
            if identity["pid_namespace"] != owner_namespace:
                marker = Path(f"/proc/{identity['pid']}/root/outputs/started")
                if marker.exists():
                    assert marker.read_text() == "candidate released"
                    return identity
        time.sleep(0.02)
    raise AssertionError("candidate never passed the durable namespace-release gate")


@pytest.mark.parametrize("phase", ["reserved", "on_worker", "on_namespace"])
def test_sigkill_before_candidate_release_requires_recovery_and_consumes_attempt(
    executor_scenario: ExecutorScenario, phase: str
) -> None:
    item = executor_scenario
    before = item.authoritative_bytes()
    execution_id = item.preview()["execution_id"]
    with paused_at(item, phase) as (_, _, authorization):
        if phase != "reserved":
            observed = executor.show(item.journal, execution_id)
            assert observed["receipt"] is None
            assert item.preview()["status"] in ("reserved", "running", "recovery_required", "busy")
    recovered = executor.recover(item.journal, execution_id)
    assert recovered["status"] == "interrupted", recovered
    assert recovered["receipt"]["attempt"] == 1
    assert recovered["receipt"]["build"] is None
    assert item.apply(authorization=authorization)["receipt"] == recovered["receipt"]
    fresh = item.preview()
    assert fresh["status"] == "ready" and fresh["authorization_template"]["attempt"] == 2
    assert item.authoritative_bytes() == before
    assert not list(item.output_root.rglob("report.json"))


def test_sigkill_after_build_publication_recovers_original_build_without_second_execution(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    before = item.authoritative_bytes()
    execution_id = item.preview()["execution_id"]
    with paused_at(item, "bundle_published") as (_, _, authorization):
        reports = list(item.output_root.rglob("report.json"))
        assert len(reports) == 1
        original = reports[0].read_bytes()
        assert json.loads(original)["status"] == "completed"
    recovered = executor.recover(item.journal, execution_id)
    assert recovered["status"] == "succeeded", recovered
    assert recovered["receipt"]["build"] is not None
    assert item.apply(authorization=authorization)["receipt"] == recovered["receipt"]
    assert executor.recover(item.journal, execution_id)["receipt"] == recovered["receipt"]
    assert list(item.output_root.rglob("report.json")) == reports
    assert reports[0].read_bytes() == original
    assert item.authoritative_bytes() == before


def test_sigkill_after_terminal_receipt_returns_immutable_success_on_replay(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    before = item.authoritative_bytes()
    execution_id = item.preview()["execution_id"]
    with paused_at(item, "terminal_receipt") as (_, _, authorization):
        pass
    retained = executor.show(item.journal, execution_id)
    assert retained["status"] == "succeeded", retained
    journal = item.journal.read_bytes()
    repeated = item.apply(authorization=authorization)
    assert repeated["receipt"] == retained["receipt"]
    assert executor.recover(item.journal, execution_id)["receipt"] == retained["receipt"]
    assert item.journal.read_bytes() == journal
    assert len(list(item.output_root.rglob("report.json"))) == 1
    assert item.authoritative_bytes() == before


@pytest.mark.parametrize("changed", ["artifact", "report"])
def test_recovery_rejects_damaged_published_output_instead_of_claiming_success(
    executor_scenario: ExecutorScenario, changed: str
) -> None:
    item = executor_scenario
    execution_id = item.preview()["execution_id"]
    before = item.authoritative_bytes()
    with paused_at(item, "bundle_published"):
        pass
    report = next(item.output_root.rglob("report.json"))
    if changed == "artifact":
        (report.parent / "artifacts/result.txt").write_bytes(b"changed after publication")
    else:
        report.write_bytes(b'{"status":"completed","claims":{"verified":true}}\n')
    result = executor.recover(item.journal, execution_id)
    assert result["status"] == "output_invalid", result
    assert result["receipt"]["build"] is None
    assert item.authoritative_bytes() == before


def test_real_timeout_includes_staging_and_frees_slot_only_after_verified_termination(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    request = item.command("import time\ntime.sleep(60)\n", timeout_seconds=0.2)
    before = item.authoritative_bytes()
    started = time.monotonic()
    result = item.apply(request=request)
    assert result["status"] == "timed_out", result
    assert time.monotonic() - started < 10
    assert result["receipt"]["build"] is None
    assert item.authoritative_bytes() == before
    with executor_store.opened(item.journal, write=False) as journal:
        retained = journal.get(result["execution_id"])
    assert retained is not None
    for identity in _identities(retained):
        if identity["pid"] != os.getpid():
            assert not executor_runtime.alive(identity)
    assert item.preview(request=request)["authorization_template"]["attempt"] == 2


def test_cancel_preserves_current_state_and_stale_model_does_not_block_cleanup(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    execution_id = item.preview()["execution_id"]
    before = item.authoritative_bytes()
    with paused_at(item, "on_namespace", expect_kill=False) as (_, channel, _):
        document = item.documents[item.dispatch.source.model_file]
        record = next(entry for entry in document["records"] if entry["id"] == "REQ-1")
        record["lifecycle"] = "retired"
        cancelled = executor.cancel(item.journal, execution_id)
        assert cancelled["status"] in ("cancelled", "cancellation_requested", "running"), cancelled
        # The live supervisor is deliberately paused after retaining the exact namespace. The
        # cleanup path must be able to stop it without re-evaluating the now-retired contract.
        channel.send("continue")
        assert channel.poll(10)
        message = channel.recv()
        assert message[0] == "result", message
        assert message[1]["status"] == "cancelled", message
        assert message[1]["receipt"]["build"] is None
    assert executor.show(item.journal, execution_id)["status"] == "cancelled"
    assert item.authoritative_bytes() == before


def test_interrupted_attempt_recovery_survives_actual_current_state_lineage_change(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    execution_id = item.preview()["execution_id"]
    with paused_at(item, "on_namespace"):
        pass
    snapshot = state_writer.read(item.state_path)
    assert snapshot.document is not None
    state = copy.deepcopy(snapshot.document)
    state["serial"] += 1
    state["config_revision"] = "c" * 40
    state["x-new-model"] = "retain new model observations"
    state_writer.publish(snapshot, state)
    before = item.authoritative_bytes()
    result = executor.recover(item.journal, execution_id)
    assert result["status"] == "interrupted", result
    assert item.authoritative_bytes() == before


def test_changed_input_does_not_convert_an_unresolved_attempt_into_new_work(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    with paused_at(item, "on_namespace"):
        changed = replace(item.request, inputs={"candidate.py": b"print('different work')"})
        before = item.journal.read_bytes()
        try:
            report = item.preview(request=changed)
        except ValueError:
            pass
        else:
            assert report["status"] in ("conflict", "running", "recovery_required", "busy"), report
            assert report["authorization_template"] is None
        assert item.journal.read_bytes() == before


def test_sigkill_after_candidate_release_terminates_real_namespace_before_retry(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    item.request = item.command(
        "from pathlib import Path\nimport time\nPath('/outputs/started').write_text('candidate released')\n"
        "while True:\n time.sleep(1)\n",
        timeout_seconds=60,
    )
    authorization = item.authorization
    execution_id = authorization["execution_id"]
    before = item.authoritative_bytes()
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_supervise, args=(item, authorization, "after_release", child))
    process.start()
    pid = process.pid
    assert pid is not None
    child.close()
    try:
        namespace = _wait_for_candidate(item, execution_id, process, parent)
        os.kill(pid, signal.SIGKILL)
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
        # A dead supervisor does not itself clear the durable occupied slot.
        assert item.preview()["authorization_template"] is None
        recovered = executor.recover(item.journal, execution_id)
        assert recovered["status"] == "interrupted", recovered
        assert not executor_runtime.alive(namespace)
        assert recovered["receipt"]["build"] is None
        assert item.preview()["authorization_template"]["attempt"] == 2
        assert item.authoritative_bytes() == before
    finally:
        if process.is_alive():
            os.kill(pid, signal.SIGKILL)
        process.join(timeout=10)
        parent.close()


def test_worker_wall_limit_terminates_candidate_even_while_supervisor_is_stopped(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    item.request = item.command(
        "from pathlib import Path\nimport time\nPath('/outputs/started').write_text('candidate released')\n"
        "while True:\n time.sleep(1)\n",
        timeout_seconds=4,
    )
    authorization = item.authorization
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_supervise, args=(item, authorization, "after_release", child))
    process.start()
    pid = process.pid
    assert pid is not None
    child.close()
    stopped = False
    try:
        namespace = _wait_for_candidate(item, authorization["execution_id"], process, parent)
        os.kill(pid, signal.SIGSTOP)
        stopped = True
        expires = time.monotonic() + 6
        while executor_runtime.alive(namespace) and time.monotonic() < expires:
            time.sleep(0.02)
        assert not executor_runtime.alive(namespace), "stopping the parent extended the approved worker time budget"
        os.kill(pid, signal.SIGCONT)
        stopped = False
        assert parent.poll(10), "resumed supervisor did not retain its timeout outcome"
        message = parent.recv()
        assert message[0] == "result", message
        assert message[1]["status"] == "timed_out", message
        assert message[1]["receipt"]["build"] is None
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if stopped:
            os.kill(pid, signal.SIGCONT)
        if process.is_alive():
            os.kill(pid, signal.SIGKILL)
        process.join(timeout=10)
        parent.close()


def test_competing_registered_task_cannot_exceed_global_initialized_concurrency(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    with paused_at(item, "on_namespace"):
        snapshot = state_writer.read(item.state_path)
        assert snapshot.document is not None
        state = copy.deepcopy(snapshot.document)
        state["serial"] += 1
        state["pending"].append(
            {
                "record": "REQ-1",
                "dimension": "verification",
                "task": "issues/second-task",
                "opened_at": "2026-10-30T09:30:00+00:00",
            }
        )
        state_writer.publish(snapshot, state)
        request = item.request
        binding = executions.register(
            item.ledger,
            executions.Registration(
                request.plan,
                request.action_id,
                state,
                "issues/second-task",
                "2026-10-30T09:30:00+00:00",
                request.readiness_policy,
                request.trust_policy,
                request.target,
                request.date,
            ),
        )
        second = replace(request, binding_id=binding["id"])
        before = item.authoritative_bytes()
        report = item.preview(request=second)
        if report["authorization_template"] is not None:
            with pytest.raises(ValueError, match=r"concurr|capacity|limit"):
                item.apply(request=second, authorization=report["authorization_template"])
        else:
            assert report["status"] in ("concurrency_limit", "busy", "blocked"), report
        assert item.authoritative_bytes() == before
        assert not list(item.output_root.rglob("report.json"))
        with executor_store.opened(item.journal, write=False) as journal:
            second_work = journal.get(report["execution_id"])
        assert second_work is None or not second_work["attempts"]
