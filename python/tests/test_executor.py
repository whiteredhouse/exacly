"""Approved executor outcomes retain identities without acquiring verification authority."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from exacly import executions, executor, executor_runtime, executor_store, history_maintenance, planning, state_writer
from executor_fixtures import ExecutorScenario
from executor_fixtures import dispatch_scenario as dispatch_scenario
from executor_fixtures import executor_scenario as executor_scenario
from executor_fixtures import isolation_runtime as isolation_runtime

Doc = dict[str, Any]


def test_preview_is_read_only_and_renders_exact_authorized_work(executor_scenario: ExecutorScenario) -> None:
    item = executor_scenario
    before = (*item.authoritative_bytes(), item.journal.read_bytes())
    calls = list(item.dispatch.source.calls), list(item.dispatch.client.calls)
    report = item.preview()
    assert report["status"] == "ready", report
    assert report["dry_run"] is True
    assert report["authorization_template"]["execution_id"] == report["execution_id"]
    assert report["authorization_template"]["attempt"] == 1
    rendered = executor.as_text(report)
    assert report["execution_id"] in rendered
    assert (*item.authoritative_bytes(), item.journal.read_bytes()) == before
    assert (list(item.dispatch.source.calls), list(item.dispatch.client.calls)) == calls
    assert list(item.output_root.iterdir()) == []


def test_real_executor_retains_host_identified_build_and_preserves_authoritative_work(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    before = item.authoritative_bytes()
    report = item.apply()
    assert report["status"] == "succeeded", report
    receipt = report["receipt"]
    assert receipt["attempt"] == 1
    assert receipt["binding_id"] == item.request.binding_id
    binding = executions.get(item.ledger, item.request.binding_id)["binding"]
    assert receipt["task"] == binding["contract"]["pending"]["task"]
    build = receipt["build"]
    assert build["kind"] == "build" and build["id"].startswith("sha256:")
    assert build["artifacts"] and len(build["artifacts"]) == 1
    artifact = build["artifacts"][0]
    assert artifact["path"].endswith("result.txt")
    output = Path(receipt["output"])
    assert (output / "artifacts/result.txt").read_bytes() == b"identified build\n"
    isolation_report = json.loads((output / "report.json").read_bytes())
    assert isolation_report["isolation"]["verified"] is True
    assert isolation_report["isolation"]["namespace_terminated"] is True
    assert receipt["isolation_report_digest"] == isolation_report["digest"]
    assert item.authoritative_bytes() == before
    assert all(
        value is False
        for name, value in receipt["claims"].items()
        if any(part in name for part in ("verification", "completion", "evidence", "trusted"))
    )
    assert item.dispatch.client.calls.count("create") == 1


def test_approved_task_context_is_read_only_snapshot_inside_real_runtime(executor_scenario: ExecutorScenario) -> None:
    item = executor_scenario
    request = item.command(
        "from pathlib import Path\n"
        "context=Path('/inputs/.exacly-task.json')\n"
        "data=context.read_bytes()\n"
        "try:\n context.write_bytes(b'candidate replacement'); writable=True\n"
        "except OSError:\n writable=False\n"
        "assert not writable\n"
        "Path('/outputs/result.txt').write_bytes(data)\n"
    )
    report = item.apply(request=request)
    assert report["status"] == "succeeded", report
    context = json.loads((Path(report["receipt"]["output"]) / "artifacts/result.txt").read_bytes())
    encoded = json.dumps(context)
    assert "REQ-1" in encoded and item.request.binding_id in encoded
    assert item.request.plan["actions"][0]["criteria_digest"] in encoded
    assert item.request.plan["actions"][0]["bindings_digest"] in encoded


def test_exact_success_replay_returns_receipt_after_model_and_state_advance(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    authorization = item.authorization
    first = item.apply(authorization=authorization)
    assert first["status"] == "succeeded", first
    document = item.documents[item.dispatch.source.model_file]
    requirement = next(record for record in document["records"] if record["id"] == "REQ-1")
    requirement["lifecycle"] = "retired"
    snapshot = state_writer.read(item.state_path)
    assert snapshot.document is not None
    state = copy.deepcopy(snapshot.document)
    state["serial"] += 1
    state["x-later-work"] = "preserved"
    state_writer.publish(snapshot, state)
    before = (*item.authoritative_bytes(), item.journal.read_bytes())
    repeated = item.apply(authorization=authorization)
    assert repeated["status"] == "succeeded" and repeated["receipt"] == first["receipt"]
    assert (*item.authoritative_bytes(), item.journal.read_bytes()) == before
    assert len(list(item.output_root.rglob("report.json"))) == 1


def test_changed_authorization_cannot_replace_a_successful_receipt(executor_scenario: ExecutorScenario) -> None:
    item = executor_scenario
    authorization = item.authorization
    first = item.apply(authorization=authorization)
    changed = {**authorization, "unreviewed": True}
    before = item.journal.read_bytes()
    with pytest.raises(ValueError):
        item.apply(authorization=changed)
    assert item.journal.read_bytes() == before
    assert executor.show(item.journal, first["execution_id"])["receipt"] == first["receipt"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter", "host-command"),
        ("max_attempts", 0),
        ("max_attempts", 11),
        ("max_attempts", True),
        ("max_attempts", 2.0),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", 3601),
        ("permitted_repository_effects", ["push"]),
        ("expected_outputs", []),
        ("expected_outputs", ["../escape"]),
        ("expected_outputs", ["/absolute"]),
        ("expected_outputs", ["."]),
        ("expected_outputs", ["result.txt", "result.txt"]),
        ("argv", []),
        ("extra", True),
    ],
)
def test_invalid_or_expanded_policy_never_reserves_an_attempt(
    executor_scenario: ExecutorScenario, field: str, value: Any
) -> None:
    item = executor_scenario
    chosen = {**copy.deepcopy(item.policy), field: value}
    request = replace(item.request, executor_policy=chosen)
    before = (*item.authoritative_bytes(), item.journal.read_bytes())
    with pytest.raises(ValueError):
        item.preview(request=request)
    assert (*item.authoritative_bytes(), item.journal.read_bytes()) == before
    assert list(item.output_root.iterdir()) == []


@pytest.mark.parametrize("change", ["input", "command", "runtime", "output", "binding", "store_instance"])
def test_authorization_cannot_be_reused_for_changed_execution(executor_scenario: ExecutorScenario, change: str) -> None:
    item = executor_scenario
    authorization = item.authorization
    policy = copy.deepcopy(item.policy)
    request = item.request
    if change == "input":
        request = replace(request, inputs={"candidate.py": b"raise SystemExit(0)"})
    elif change == "command":
        policy["argv"] += ["unreviewed"]
    elif change == "runtime":
        policy["runtime"]["rootfs_digest"] = "sha256:" + "0" * 64
    elif change == "output":
        other = item.output_root.parent / "other-output"
        other.mkdir(mode=0o700)
        policy["storage"]["output_root"] = str(other)
    elif change == "binding":
        request = replace(request, binding_id="sha256:" + "0" * 64)
    else:
        policy["storage"]["journal_id"] = "0" * 32
    if change not in ("input", "binding"):
        request = replace(request, executor_policy=policy)
    before = (*item.authoritative_bytes(), item.journal.read_bytes())
    with pytest.raises(ValueError):
        item.apply(request=request, authorization=authorization)
    assert (*item.authoritative_bytes(), item.journal.read_bytes()) == before


def test_changed_current_state_requires_fresh_review_without_discarding_unrelated_work(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    authorization = item.authorization
    snapshot = state_writer.read(item.state_path)
    assert snapshot.document is not None
    state = copy.deepcopy(snapshot.document)
    state["serial"] += 1
    state["x-unrelated"] = {"note": "later operator progress"}
    state_writer.publish(snapshot, state)
    before = item.authoritative_bytes()
    with pytest.raises(ValueError):
        item.apply(authorization=authorization)
    fresh = item.apply()
    assert fresh["status"] == "succeeded", fresh
    assert item.authoritative_bytes() == before


@pytest.mark.parametrize(
    "script,outcome",
    [
        ("raise SystemExit(7)\n", "failed"),
        ("print('successful process without required build')\n", "output_invalid"),
        ("from pathlib import Path\nPath('/outputs/result.txt').symlink_to('/etc/passwd')\n", "failed"),
    ],
)
def test_unsuccessful_real_outputs_are_retained_without_build_or_completion(
    executor_scenario: ExecutorScenario, script: str, outcome: str
) -> None:
    item = executor_scenario
    before = item.authoritative_bytes()
    report = item.apply(request=item.command(script))
    assert report["status"] == outcome, report
    assert report["receipt"]["build"] is None
    assert item.authoritative_bytes() == before


def test_retry_requires_new_review_and_stops_at_retained_budget(executor_scenario: ExecutorScenario) -> None:
    item = executor_scenario
    request = item.command("raise SystemExit(7)\n")
    first_authorization = item.preview(request=request)["authorization_template"]
    first = item.apply(request=request, authorization=first_authorization)
    assert first["status"] == "failed", first
    assert item.apply(request=request, authorization=first_authorization)["receipt"] == first["receipt"]
    second_preview = item.preview(request=request)
    assert second_preview["authorization_template"]["attempt"] == 2
    second = item.apply(request=request, authorization=second_preview["authorization_template"])
    assert second["status"] == "failed" and second["receipt"]["attempt"] == 2
    exhausted = item.preview(request=request)
    assert exhausted["status"] == "attempts_exhausted", exhausted
    assert exhausted["authorization_template"] is None
    before = item.journal.read_bytes()
    with pytest.raises(ValueError):
        item.apply(request=request, authorization=second_preview["authorization_template"] | {"attempt": 3})
    assert item.journal.read_bytes() == before


@pytest.mark.parametrize("change", ["policy_version", "attempts", "timeout", "output_root"])
def test_retained_contract_prevents_budget_reset_by_policy_or_destination_change(
    executor_scenario: ExecutorScenario, change: str
) -> None:
    item = executor_scenario
    request = item.command("raise SystemExit(1)\n")
    first = item.apply(request=request)
    assert first["status"] == "failed", first
    chosen = copy.deepcopy(request.executor_policy)
    if change == "policy_version":
        chosen["version"] = "2"
    elif change == "attempts":
        chosen["max_attempts"] = 3
    elif change == "timeout":
        chosen["timeout_seconds"] += 1
    else:
        other = item.output_root.parent / "reset-output"
        other.mkdir(mode=0o700)
        chosen["storage"]["output_root"] = str(other)
    changed = replace(request, executor_policy=chosen)
    before = item.journal.read_bytes()
    try:
        report = item.preview(request=changed)
    except ValueError:
        pass
    else:
        assert report["status"] == "conflict", report
        assert report["authorization_template"] is None
    assert item.journal.read_bytes() == before


def test_reserved_metadata_snapshot_cannot_be_supplied_by_candidate_caller(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    request = replace(item.request, inputs={**item.request.inputs, ".exacly-task.json": b'{"approved":true}'})
    with pytest.raises(ValueError):
        item.preview(request=request)
    assert list(item.output_root.iterdir()) == []


def test_copied_initialized_attempt_store_is_not_another_authorized_coordinator(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    authorization = item.authorization
    copied = item.journal.with_name("copied.sqlite3")
    copied.write_bytes(item.journal.read_bytes())
    copied.chmod(0o600)
    policy = copy.deepcopy(item.policy)
    policy["storage"]["journal"] = str(copied)
    changed = replace(item.request, executor_policy=policy)
    with pytest.raises(ValueError):
        executor.apply(
            copied,
            item.ledger,
            item.documents,
            item.state_path,
            changed,
            authorization=authorization,
        )
    assert list(item.output_root.iterdir()) == []


def test_history_contains_no_completion_receipts_or_changed_binding_ledger(
    executor_scenario: ExecutorScenario,
) -> None:
    item = executor_scenario
    before = item.ledger.read_bytes()
    report = item.apply()
    shown = executor.show(item.journal, report["execution_id"])
    assert shown["receipt"] == report["receipt"]
    with executor_store.opened(item.journal, write=False) as journal:
        retained = journal.get(report["execution_id"])
    assert retained is not None and len(retained["attempts"]) == 1
    receipt = report["receipt"]
    payload = {key: value for key, value in receipt.items() if key != "digest"}
    assert receipt["digest"] == planning.digest(payload)
    assert item.ledger.read_bytes() == before


def test_supported_operational_v4_binding_ledger_remains_byte_identical_during_execution(
    executor_scenario: ExecutorScenario, tmp_path: Path
) -> None:
    item = executor_scenario
    original = item.ledger.read_bytes()
    indexed = tmp_path / "indexed-operator"
    history_maintenance.activate(item.ledger, indexed)
    ledger = indexed / "operational" / "ledger.sqlite3"
    journal = item.journal.with_name("indexed-executor.sqlite3")
    initialized = executor_store.initialize(journal, ledger=ledger, max_concurrency=1)
    chosen = copy.deepcopy(item.policy)
    chosen["storage"].update(journal=str(journal), journal_id=initialized["journal_id"], ledger=str(ledger))
    request = replace(item.request, executor_policy=chosen)
    before = ledger.read_bytes(), item.state_path.read_bytes()
    authorization = executor.preview(item.documents, item.state_path, request)["authorization_template"]
    result = executor.apply(journal, ledger, item.documents, item.state_path, request, authorization=authorization)
    assert result["status"] == "succeeded", result
    assert (ledger.read_bytes(), item.state_path.read_bytes()) == before
    assert item.ledger.read_bytes() == original


def test_actual_state_is_rechecked_after_worker_start_before_candidate_release(
    executor_scenario: ExecutorScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = executor_scenario
    run = executor_runtime.run

    def concurrent_update(*args: Any, **kwargs: Any) -> Doc:
        callback = kwargs["on_worker"]

        def on_worker(identity: Doc) -> None:
            callback(identity)
            snapshot = state_writer.read(item.state_path)
            assert snapshot.document is not None
            state = copy.deepcopy(snapshot.document)
            state["serial"] += 1
            state["x-new-work"] = "newer observations retained"
            state_writer.publish(snapshot, state)

        kwargs["on_worker"] = on_worker
        return run(*args, **kwargs)

    monkeypatch.setattr(executor_runtime, "run", concurrent_update)
    ledger = item.ledger.read_bytes()
    report = item.apply()
    assert report["status"] == "failed", report
    assert report["receipt"]["build"] is None
    assert "changed before candidate release" in report["receipt"]["reason"]
    assert not list(item.output_root.rglob("report.json"))
    assert item.ledger.read_bytes() == ledger
    assert json.loads(item.state_path.read_bytes())["x-new-work"] == "newer observations retained"


def test_abandoned_invocation_can_be_recovered_in_same_still_living_api_process(
    executor_scenario: ExecutorScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = executor_scenario
    before = item.authoritative_bytes()
    terminate = executor_runtime.terminate

    def unavailable_runtime(*args: Any, **kwargs: Any) -> Doc:
        raise ValueError("controlled failure before worker startup")

    def unavailable_termination(*args: Any, **kwargs: Any) -> list[Doc]:
        raise ValueError("controlled interruption of termination inspection")

    monkeypatch.setattr(executor_runtime, "run", unavailable_runtime)
    monkeypatch.setattr(executor_runtime, "terminate", unavailable_termination)
    authorization = item.authorization
    result = item.apply(authorization=authorization)
    assert result["status"] == "interrupted" and result["receipt"] is None, result
    assert item.preview()["authorization_template"] is None
    monkeypatch.setattr(executor_runtime, "terminate", terminate)
    # The original PID is still this test process; only the invocation's lock has ended.
    recovered = executor.recover(item.journal, result["execution_id"])
    assert recovered["status"] == "interrupted" and recovered["receipt"] is not None
    assert item.apply(authorization=authorization)["receipt"] == recovered["receipt"]
    assert item.preview()["authorization_template"]["attempt"] == 2
    assert item.authoritative_bytes() == before


@pytest.mark.parametrize("outcome", ["timed_out", "output_limit", "cancelled"])
def test_published_bundle_cannot_override_an_already_known_trusted_stop_outcome(
    executor_scenario: ExecutorScenario, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    item = executor_scenario
    run = executor_runtime.run

    def stop_concurrent_with_publication(*args: Any, **kwargs: Any) -> Doc:
        result = run(*args, **kwargs)
        assert result["report"] is not None and result["report"]["status"] == "completed"
        # Model a trusted watchdog decision racing with publication. A complete untrusted
        # candidate report must not replace the supervisor's already established stop outcome.
        return {**result, "status": outcome, "reason": "trusted supervisor stop concurrent with publication"}

    monkeypatch.setattr(executor_runtime, "run", stop_concurrent_with_publication)
    before = item.authoritative_bytes()
    authorization = item.authorization
    result = item.apply(authorization=authorization)
    assert result["status"] == outcome, result
    assert result["receipt"]["build"] is None
    assert result["receipt"]["reason"] == "trusted supervisor stop concurrent with publication"
    assert item.apply(authorization=authorization)["receipt"] == result["receipt"]
    assert item.authoritative_bytes() == before
