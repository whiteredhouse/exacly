"""One authorized local completion preserves other work and retains exact recoverable proof."""

from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from exacly import (
    completion,
    completion_store,
    execution_maintenance,
    executions,
    history_maintenance,
    planning,
    query,
    state_writer,
)
from exacly import execution_store as store
from exacly.refresh import Run, refresh
from test_admission import ATTEMPT, BASE, END, READINESS, RUN_ID, Source
from test_applicability import WHEN, A, B
from test_executions import OPENED, TASK, run
from test_reconciliation import change_live_outcome, make_plan, selected, update_model
from test_refresh import config, requirement

Doc = dict[str, Any]
OTHER_TASK = "issues/unrelated"
LATER = "2026-10-28T13:17:00Z"


class NoNetwork:
    def get_json(self, path: str) -> Doc:
        raise AssertionError("historical completion must not fetch GitHub")

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise AssertionError("historical completion must not download evidence")


@dataclass
class CompletionScenario:
    source: Source
    plan: Doc
    readiness: Doc
    state_path: Path
    ledger: Path
    binding: Doc

    @property
    def state(self) -> Doc:
        return dict(json.loads(self.state_path.read_bytes()))

    def complete(self, **changes: Any) -> Doc:
        return complete_scenario(self, **changes)


def prepare_scenario(
    tmp_path: Path,
    *,
    source: Source | None = None,
    readiness: Doc | None = None,
    legacy: bool = False,
    upgrade: bool = True,
) -> CompletionScenario:
    source = source or Source()
    readiness = copy.deepcopy(READINESS if readiness is None else readiness)
    documents = {source.model_file: source.config}
    previous = refresh(documents, {}, run(source))
    previous["pending"] = [
        {"record": "REQ-1", "task": TASK, "opened_at": OPENED, **({} if legacy else {"dimension": "verification"})},
        {"record": "REQ-1", "task": OTHER_TASK, "opened_at": OPENED, "dimension": "delivery"},
    ]
    previous["records"]["REQ-1"]["evidence"] = [
        {
            "locator": "observations/unrelated.log",
            "kind": "runtime_observation",
            "claim": "Retained observation",
            "observed_at": WHEN.isoformat(),
            "applies_to": A,
        }
    ]
    previous["x-operator-note"] = {"nested": ["preserved", {"value": True}]}
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    state_path = state_directory / "main.json"
    state_writer.publish(state_writer.read(state_path), previous)
    private = tmp_path / "original-operator"
    private.mkdir(mode=0o700)
    ledger = private / "ledger.sqlite3"
    plan = make_plan(source, readiness=readiness)
    binding = executions.register(
        ledger,
        executions.Registration(
            plan,
            plan["actions"][0]["id"],
            previous,
            TASK,
            OPENED,
            readiness,
            source.policy,
            selected(B),
            WHEN,
            adopt_verification=legacy,
        ),
    )
    if upgrade:
        destination = tmp_path / "upgraded-operator"
        execution_maintenance.upgrade(ledger, destination)
        ledger = destination / "upgraded" / "ledger.sqlite3"
    return CompletionScenario(source, plan, readiness, state_path, ledger, binding)


def complete_scenario(case: CompletionScenario, **changes: Any) -> Doc:
    fields: Doc = {
        "source": case.source,
        "plan_doc": case.plan,
        "action_id": case.plan["actions"][0]["id"],
        "readiness_policy": case.readiness,
        "trust_policy": case.source.policy,
        "output_target": selected(B),
        "run_id": RUN_ID,
        "attempt": ATTEMPT,
        "date": WHEN,
        "ledger": case.ledger,
        "binding_id": case.binding["id"],
        "state_path": case.state_path,
    }
    return completion.complete(**{**fields, **changes})


@pytest.fixture
def scenario(tmp_path: Path) -> CompletionScenario:
    return prepare_scenario(tmp_path)


def test_completion_preserves_unrelated_work_and_retains_exact_proof(
    scenario: CompletionScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_bytes = scenario.state_path.read_bytes()
    before = scenario.state
    inputs = copy.deepcopy((scenario.plan, scenario.readiness, scenario.source.policy))
    monkeypatch.setattr(completion, "now", lambda: LATER)
    report = scenario.complete()
    assert report["status"] == "completed", report
    jsonschema.validate(report, completion.SCHEMA)
    receipt = report["receipt"]
    jsonschema.validate(receipt, completion.RECEIPT_SCHEMA)
    assert receipt["digest"] == planning.digest({key: value for key, value in receipt.items() if key != "digest"})
    assert report["digest"] == planning.digest({key: value for key, value in report.items() if key != "digest"})
    after = scenario.state
    assert after["serial"] == before["serial"] + 1
    assert (after["namespace"], after["context"], after["lineage"]) == (
        before["namespace"],
        before["context"],
        before["lineage"],
    )
    assert after["pending"] == [before["pending"][1]]
    assert after["records"]["REQ-1"]["evidence"] == before["records"]["REQ-1"]["evidence"]
    assert after["x-operator-note"] == before["x-operator-note"]
    assert after["written_at"] == LATER
    assessment = after["records"]["REQ-1"]["assessments"][0]
    assert assessment["state"] == "passed" and assessment["subject"] == {"model_revision": A, "revision": B}
    assert assessment["at"] == END
    assert (scenario.plan, scenario.readiness, scenario.source.policy) == inputs
    with store.locked(scenario.ledger, write=False) as (ledger, _), store.database(ledger, create=False) as (conn, _):
        retained = completion_store.get(conn, report["completion_id"])
    assert retained["phase"] == "committed"
    assert retained["before_state"] == before_bytes
    assert retained["after_state"] == scenario.state_path.read_bytes()
    assert retained["archive"] == scenario.source.archive
    assert retained["report"] == scenario.source.report
    assert retained["receipt"] == receipt
    assert retained["admission_receipt"] == report["admission"]
    assert retained["admission_receipt"]["state_digest"] == planning.digest(retained["admitted_state"])
    assert retained["model_documents"] == {scenario.source.model_file: scenario.source.config}
    assert report["claims"]["fresh_evidence_admitted"] is True
    assert receipt["claims"]["task_completion_recorded"] is True
    assert not any(
        receipt["claims"][key]
        for key in (
            "test_honesty_verified",
            "coverage_verified",
            "executor_output_link_verified",
            "self_authenticating",
            "external_task_closed",
            "latest_state_verified",
        )
    )


def test_completed_replay_is_historical_offline_and_never_touches_later_state(scenario: CompletionScenario) -> None:
    first = scenario.complete()
    scenario.state_path.write_text("a later unrelated replacement does not invalidate a historical receipt")
    state_bytes = scenario.state_path.read_bytes()
    ledger_bytes = scenario.ledger.read_bytes()
    replay = scenario.complete(source=NoNetwork())
    assert replay["status"] == "completed" and replay["receipt"] == first["receipt"]
    assert replay["claims"]["fresh_evidence_admitted"] is False
    assert replay["claims"]["latest_state_verified"] is False
    assert scenario.state_path.read_bytes() == state_bytes and scenario.ledger.read_bytes() == ledger_bytes
    shown = completion.show(scenario.ledger, first["completion_id"])
    assert shown["receipt"] == first["receipt"] and shown["claims"]["operator_authorized"] is False
    assert shown["intent"]["request"]["state_path"] == str(scenario.state_path)
    assert shown["intent"]["evidence"]["archive_digest"] == first["receipt"]["evidence"]["archive_digest"]
    assert str(scenario.state_path) in completion.as_text(shown)
    assert "historical" in completion.as_text(shown)
    recovered = completion.recover(scenario.ledger, first["completion_id"], state_path=scenario.state_path)
    assert recovered["receipt"] == first["receipt"]
    jsonschema.validate(shown, completion.SCHEMA)
    jsonschema.validate(recovered, completion.SCHEMA)


@pytest.mark.parametrize("version", [2, 4])
def test_explicit_protection_waiver_completes_and_replays_with_retained_proof(tmp_path: Path, version: int) -> None:
    source = Source()
    source.policy["branch_protection"] = "not_required"
    source.pages[f"{BASE}/branches/main"]["protected"] = False
    case = prepare_scenario(tmp_path, source=source)
    if version == 4:
        destination = tmp_path / "operational-operator"
        history_maintenance.activate(case.ledger, destination)
        case.ledger = destination / "operational" / "ledger.sqlite3"
    before = case.state
    report = case.complete()
    assert report["status"] == "completed", report
    assert report["admission"]["claims"]["branch_protection_verified"] is False
    assert report["admission"]["policy"]["digest"] == planning.digest(source.policy)
    assert case.state["serial"] == before["serial"] + 1
    assert case.state["lineage"] == before["lineage"]
    assert case.state["pending"] == [before["pending"][1]]
    assert case.state["x-operator-note"] == before["x-operator-note"]
    after = case.ledger.read_bytes(), case.state_path.read_bytes()
    replay = case.complete(source=NoNetwork())
    assert replay["status"] == "completed" and replay["receipt"] == report["receipt"]
    assert (case.ledger.read_bytes(), case.state_path.read_bytes()) == after
    assert completion.show(case.ledger, report["completion_id"])["receipt"] == report["receipt"]
    assert execution_maintenance.inspect(case.ledger)["status"] == "clean"


@pytest.mark.parametrize("failure", ["run_revision", "failed_tests"])
def test_protection_waiver_does_not_bypass_other_completion_prerequisites(tmp_path: Path, failure: str) -> None:
    source = Source()
    source.policy["branch_protection"] = "not_required"
    source.pages[f"{BASE}/branches/main"]["protected"] = False
    case = prepare_scenario(tmp_path, source=source)
    if failure == "run_revision":
        source.run["head_sha"] = "c" * 40
    else:
        source.report = source.report.replace(b"</testcase>", b"<failure/></testcase>")
        change_live_outcome(source, "failure")
    before = case.ledger.read_bytes(), case.state_path.read_bytes()
    report = case.complete()
    assert report["status"] == "pending" and report["completion_id"] is None, report
    assert report["admission"]["claims"]["branch_protection_verified"] is False
    assert report["admission"]["status"] == ("rejected" if failure == "run_revision" else "admitted")
    assert (case.ledger.read_bytes(), case.state_path.read_bytes()) == before


def test_completed_binding_cannot_be_reused_for_different_evidence(scenario: CompletionScenario) -> None:
    scenario.complete()
    before = scenario.ledger.read_bytes(), scenario.state_path.read_bytes()
    with pytest.raises(ValueError, match=r"completed|different"):
        scenario.complete(source=NoNetwork(), attempt=ATTEMPT + 1)
    assert (scenario.ledger.read_bytes(), scenario.state_path.read_bytes()) == before


def test_completion_requires_explicit_v2_upgrade_without_initializing_or_migrating(tmp_path: Path) -> None:
    case = prepare_scenario(tmp_path, upgrade=False)
    before = case.ledger.read_bytes(), case.state_path.read_bytes()
    with pytest.raises(ValueError, match=r"upgrade|version|v2"):
        case.complete(source=NoNetwork())
    assert (case.ledger.read_bytes(), case.state_path.read_bytes()) == before


@pytest.mark.parametrize("case", ["origin", "failed", "skipped", "unrelated"])
def test_insufficient_fresh_evidence_preserves_state_and_journal(scenario: CompletionScenario, case: str) -> None:
    if case == "origin":
        scenario.source.pages[f"{BASE}/branches/main"]["protected"] = False
    elif case == "failed":
        scenario.source.report = scenario.source.report.replace(b"</testcase>", b"<failure/></testcase>")
        change_live_outcome(scenario.source, "failure")
    elif case == "skipped":
        scenario.source.report = scenario.source.report.replace(b"</testcase>", b"<skipped/></testcase>")
        change_live_outcome(scenario.source, "success")
    else:
        scenario.source.report = b'<testsuite><testcase name="unrelated"/></testsuite>'
        change_live_outcome(scenario.source, "success")
    before = scenario.ledger.read_bytes(), scenario.state_path.read_bytes()
    report = scenario.complete()
    assert report["status"] == "pending", report
    assert (scenario.ledger.read_bytes(), scenario.state_path.read_bytes()) == before
    assert report["completion_id"] is None
    jsonschema.validate(report, completion.SCHEMA)


@pytest.mark.parametrize("case", ["policy", "branch_protection", "target", "pending", "watermark", "superseded"])
def test_stale_contracts_fail_before_live_admission(scenario: CompletionScenario, case: str) -> None:
    changes: Doc = {"source": NoNetwork()}
    state = scenario.state
    if case == "policy":
        changed = copy.deepcopy(scenario.readiness)
        changed["version"] = "replacement"
        changes["readiness_policy"] = changed
    elif case == "branch_protection":
        changed = copy.deepcopy(scenario.source.policy)
        changed["branch_protection"] = "not_required"
        changes["trust_policy"] = changed
    elif case == "target":
        changes["output_target"] = selected("c" * 40)
    elif case == "pending":
        state["pending"][0]["task"] = "issues/changed"
        scenario.state_path.write_text(json.dumps(state))
    elif case == "watermark":
        state["x-rewrite"] = "same serial divergence"
        scenario.state_path.write_text(json.dumps(state))
    else:
        request = executions.Registration(
            scenario.plan,
            scenario.plan["actions"][0]["id"],
            state,
            TASK,
            OPENED,
            scenario.readiness,
            scenario.source.policy,
            selected("c" * 40),
            WHEN,
            supersedes=scenario.binding["id"],
        )
        executions.register(scenario.ledger, request)
    before = scenario.ledger.read_bytes(), scenario.state_path.read_bytes()
    result = scenario.complete(**changes)
    assert result["status"] == "stale", result
    assert (scenario.ledger.read_bytes(), scenario.state_path.read_bytes()) == before


@pytest.mark.parametrize("case", ["record", "relationship", "pending"])
def test_model_invalid_current_state_is_rejected_before_carry_can_prune_it(
    scenario: CompletionScenario, case: str
) -> None:
    state = scenario.state
    if case == "record":
        state["records"]["UNKNOWN"] = {"evidence": copy.deepcopy(state["records"]["REQ-1"]["evidence"])}
    elif case == "relationship":
        state["relationships"] = [
            {
                "source": "REQ-1",
                "type": "supports",
                "target": "UNKNOWN",
                "evidence": copy.deepcopy(state["records"]["REQ-1"]["evidence"]),
            }
        ]
    else:
        state["pending"].append({"record": "UNKNOWN", "task": "issues/unknown", "opened_at": OPENED})
    state["serial"] += 1
    scenario.state_path.write_text(json.dumps(state))
    before = scenario.ledger.read_bytes(), scenario.state_path.read_bytes()
    with pytest.raises(query.QueryError, match=r"current state.*conform"):
        scenario.complete()
    assert (scenario.ledger.read_bytes(), scenario.state_path.read_bytes()) == before


def test_retained_applicable_failure_blocks_completion_but_can_be_reobserved(tmp_path: Path) -> None:
    source = Source()
    record = next(record for record in source.config["records"] if record["id"] == "REQ-1")
    observed = copy.deepcopy(record["observe"]["tests"])
    other = {"workflow": source.workflow_path, "job": "other", "cases": ["other::required"]}
    record["observe"]["tests"] = [observed, other]
    update_model(source, source.config)
    case = prepare_scenario(tmp_path, source=source)
    state = refresh(
        {source.model_file: source.config},
        {"other::required": "failed"},
        Run(
            revision=B,
            model_revision=A,
            context="main",
            observed_at=dt.datetime.combine(WHEN, dt.time(9), tzinfo=dt.UTC),
            workflow=source.workflow_path,
            job="other",
            job_status="failure",
        ),
        previous=case.state,
    )
    case.state_path.write_text(json.dumps(state))
    before = case.ledger.read_bytes(), case.state_path.read_bytes()
    result = case.complete()
    assert result["status"] == "pending" and result["readiness"]["result"] == "not_satisfied"
    assert (case.ledger.read_bytes(), case.state_path.read_bytes()) == before
    passed = refresh(
        {source.model_file: source.config},
        {"other::required": "passed"},
        Run(
            revision=B,
            model_revision=A,
            context="main",
            observed_at=dt.datetime.combine(WHEN, dt.time(10), tzinfo=dt.UTC),
            workflow=source.workflow_path,
            job="other",
            job_status="success",
        ),
        previous=state,
    )
    case.state_path.write_text(json.dumps(passed))
    # Standalone admitted state has no result for the other binding; merged current state supplies it.
    result = case.complete()
    assert result["status"] == "completed", result
    assert len(case.state["records"]["REQ-1"]["assessments"]) == 2


def test_one_goal_can_complete_while_another_policy_requirement_is_unresolved(tmp_path: Path) -> None:
    source = Source()
    first = copy.deepcopy(next(record for record in source.config["records"] if record["id"] == "REQ-1"))
    update_model(
        source,
        config(
            first,
            requirement(
                "REQ-2",
                {
                    "workflow": source.workflow_path,
                    "job": source.job_id,
                    "cases": ["another::missing"],
                },
            ),
        ),
    )
    readiness = copy.deepcopy(READINESS)
    readiness["requirements"].append({"record": "REQ-2", "dimensions": ["verification"]})
    case = prepare_scenario(tmp_path, source=source, readiness=readiness)
    result = case.complete()
    assert result["status"] == "completed" and result["readiness"]["result"] != "satisfied"
    assert case.state["records"]["REQ-2"]["assessments"][0]["state"] == "not_checked"


def test_explicitly_adopted_legacy_pending_entry_can_complete(tmp_path: Path) -> None:
    case = prepare_scenario(tmp_path, legacy=True)
    assert case.binding["contract"]["adopted_verification"] is True
    assert case.complete()["status"] == "completed"
    assert case.state["pending"] == [
        {"record": "REQ-1", "task": OTHER_TASK, "opened_at": OPENED, "dimension": "delivery"}
    ]


def test_completion_removes_the_pending_field_when_no_other_work_remains(scenario: CompletionScenario) -> None:
    state = scenario.state
    state["pending"] = [state["pending"][0]]
    state["serial"] += 1
    scenario.state_path.write_text(json.dumps(state))
    assert scenario.complete()["status"] == "completed"
    assert "pending" not in scenario.state


def test_receipt_finalization_failure_retains_after_state_for_explicit_recovery(
    scenario: CompletionScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = completion_store.Journal.finalize

    def failed(*args: Any, **kwargs: Any) -> Doc:
        raise store.StoreError("simulated receipt transaction failure")

    monkeypatch.setattr(completion_store.Journal, "finalize", failed)
    with pytest.raises(completion.CompletionInterruptedError, match="explicit recovery"):
        scenario.complete()
    assert scenario.state["pending"][0]["task"] == OTHER_TASK
    with store.locked(scenario.ledger, write=False) as (ledger, _), store.database(ledger, create=False) as (conn, _):
        retained = completion_store.current(conn)
    assert retained is not None
    identifier = retained["intent"]["id"]
    before_recovery = scenario.state_path.read_bytes()
    monkeypatch.setattr(completion_store.Journal, "finalize", original)
    recovered = completion.recover(scenario.ledger, identifier, state_path=scenario.state_path)
    assert recovered["status"] == "completed"
    assert scenario.state_path.read_bytes() == before_recovery
    assert scenario.complete(source=NoNetwork())["receipt"] == recovered["receipt"]


def test_prepare_commit_followed_by_error_reports_the_retained_intent_id(
    scenario: CompletionScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = completion_store.Journal.prepare
    identities: list[str] = []
    before = scenario.state_path.read_bytes()

    def committed_then_failed(*args: Any, **kwargs: Any) -> Doc:
        retained = original(*args, **kwargs)
        identities.append(retained["intent"]["id"])
        raise OSError("simulated failure reading the already committed intent")

    with monkeypatch.context() as patched:
        patched.setattr(completion_store.Journal, "prepare", committed_then_failed)
        with pytest.raises(completion.CompletionInterruptedError, match="may contain a committed intent") as error:
            scenario.complete()
    assert len(identities) == 1 and identities[0] in str(error.value)
    assert scenario.state_path.read_bytes() == before
    shown = completion.show(scenario.ledger, identities[0])
    assert shown["status"] == "prepared" and shown["receipt"] is None
    recovered = completion.recover(scenario.ledger, identities[0], state_path=scenario.state_path)
    assert recovered["status"] == "aborted" and scenario.state_path.read_bytes() == before
    calls = len(scenario.source.calls)
    completed = scenario.complete()
    assert completed["status"] == "completed" and completed["completion_id"] != identities[0]
    assert len(scenario.source.calls) > calls


@pytest.mark.parametrize("case", ["state_path", "unknown_id"])
def test_recovery_requires_the_retained_operation_and_destination(scenario: CompletionScenario, case: str) -> None:
    result = scenario.complete()
    with pytest.raises(ValueError):
        completion.recover(
            scenario.ledger,
            "0" * 32 if case == "unknown_id" else result["completion_id"],
            state_path=scenario.state_path.with_name("different.json") if case == "state_path" else scenario.state_path,
        )


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True])
def test_invalid_lock_wait_does_not_mutate_any_input(scenario: CompletionScenario, bad: Any) -> None:
    before = scenario.ledger.read_bytes(), scenario.state_path.read_bytes()
    with pytest.raises(ValueError, match="lock timeout"):
        scenario.complete(source=NoNetwork(), lock_timeout=bad)
    assert (scenario.ledger.read_bytes(), scenario.state_path.read_bytes()) == before
