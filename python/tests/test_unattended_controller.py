"""Controlled workflow scheduling; native completion/physical runtime evidence lives separately."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import automation_controller as controller
from exacly import planning

Doc = dict[str, Any]
CURRENT = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
DIGEST = planning.digest("selected-original-objective")


class Workflow:
    def __init__(self, contract: Doc) -> None:
        self.contract = contract
        self.applicability = "eligible"
        self.producer_status = "succeeded"
        self.observed = "satisfied"
        self.cancellations = 0

    def assess(self, correlations: Doc) -> Doc:
        return {"status": self.applicability, "reason": "Controlled native applicability assessment."}

    def proposal(self, phase: str, correlations: Doc) -> Doc:
        return {"phase": phase, "preconditions": planning.digest(correlations)}

    def accept(self, phase: str, request: Doc, receipt: Doc, correlations: Doc) -> Doc:
        assert receipt["native_phase"] == phase
        return {**correlations, phase: receipt}

    def producer_request(self, correlations: Doc) -> Doc:
        return {"correlations": copy.deepcopy(correlations)}

    def producer(self, correlations: Doc) -> Doc:
        return {"status": self.producer_status, "reason": "Controlled exact producer observation."}

    def reobserve(self, correlations: Doc) -> Doc:
        assert "complete" in correlations
        return {"status": self.observed, "reason": "Controlled fresh assessment after native completion."}

    def cancel(self, correlations: Doc) -> None:
        self.cancellations += 1


class Authority:
    def __init__(self, identity: Doc, directory: Path) -> None:
        self._identity = identity
        self.directory = directory
        self.available = True
        self.result = "completed"
        self.effects: list[str] = []
        self.recoveries: list[str] = []
        self.before_effect: Any = None
        self.after_effect: Any = None
        self.reader: Any = None
        self.reads: list[str] = []
        self.cancel_requests: list[str] = []

    def identity(self) -> Doc:
        return self._identity

    def check_available(self) -> Doc:
        return {"available": self.available, "reason": "Delegation expired or revoked."}

    def execute(self, kind: str, request: Doc, *, effect_id: str) -> Doc:
        if self.before_effect:
            self.before_effect()
        self.effects.append(effect_id)
        receipt = {"native_phase": kind, "operation": effect_id}
        result = {
            "status": self.result,
            "receipt": receipt if self.result == "completed" else None,
            "reservation": {"objective_id": DIGEST, "proposal_id": effect_id},
            "reason": "Controlled native result.",
        }
        if self.result == "completed":
            path = self.directory / effect_id.replace(":", "-")
            with path.open("w") as stream:
                json.dump(result, stream)
                stream.flush()
                os.fsync(stream.fileno())
        if self.after_effect:
            self.after_effect()
        return result

    def request_cancel(self, kind: str, request: Doc, *, effect_id: str) -> Doc:
        self.cancel_requests.append(effect_id)
        return {"status": "uncertain", "receipt": None, "reason": "Controlled cancellation request retained."}

    def call_metered(self, purpose: str, request: Doc, *, operation_id: str) -> Doc:
        self.reads.append(operation_id)
        return {
            "status": "completed",
            "receipt": {"result": self.reader(request["correlations"])},
            "reason": "Controlled metered observation.",
            "reservation": {"proposal_id": operation_id},
        }

    def reconcile(self, kind: str, request: Doc, *, effect_id: str) -> Doc:
        self.recoveries.append(effect_id)
        path = self.directory / effect_id.replace(":", "-")
        if path.exists():
            return dict(json.loads(path.read_text()))
        return {
            "status": "not_started",
            "receipt": None,
            "reservation": None,
            "reason": "Controlled authoritative absence.",
        }


@dataclass
class Case:
    journal: Path
    journal_id: str
    workflow: Workflow
    authority: Authority
    current: dt.datetime = CURRENT

    def step(self) -> Doc:
        return controller.step(
            self.journal, self.journal_id, workflow=self.workflow, authority=self.authority, clock=lambda: self.current
        )

    def show(self) -> Doc:
        return controller.show(self.journal, self.journal_id)

    def recover(self) -> Doc:
        return controller.recover(
            self.journal, self.journal_id, workflow=self.workflow, authority=self.authority, clock=lambda: self.current
        )


@pytest.fixture
def case(tmp_path: Path) -> Case:
    directory = tmp_path / "controller"
    directory.mkdir(mode=0o700)
    effects = tmp_path / "native-effects"
    effects.mkdir(mode=0o700)
    identity: Doc = {"objective": {"id": DIGEST, "original_goal": DIGEST}, "issuer": "trusted-fixture"}
    contract = {
        "schema_version": "0.1.0",
        "scope": "local-machine",
        "objective_id": DIGEST,
        "original_goal": DIGEST,
        "input_target": {"context": "pilot", "model_revision": "1" * 40, "revision": "2" * 40},
        "goal": {"record": "REQ-001", "dimension": "verification"},
        "contract": dict.fromkeys(("model", "criteria", "bindings", "readiness", "trust", "observer"), DIGEST),
        "stores": {"native": {"path": str(effects / "native.sqlite3"), "id": DIGEST}},
        "authorizer": identity,
        "host": controller.host_identity(run_id="controlled-run", job_id="controlled-job"),
        "deadline_at": (CURRENT + dt.timedelta(hours=1)).isoformat(),
        "poll_seconds": 1,
        "max_poll_seconds": 30,
        "initial_binding_id": DIGEST,
        "plan_digest": DIGEST,
    }
    initialized = controller.initialize(directory / "workflow.sqlite3", contract)
    workflow = Workflow(contract)
    authority = Authority(identity, effects)
    authority.reader = workflow.producer
    return Case(Path(initialized["journal"]), initialized["journal_id"], workflow, authority)


def test_native_phase_intent_precedes_effect_and_only_completion_then_reobservation_finishes(case: Case) -> None:
    def intent_retained() -> None:
        event = case.show()["event"]
        assert event["pending"] and event["pending"]["kind"] == event["phase"]

    case.authority.before_effect = intent_retained
    for expected in ("handoff", "publish", "producer", "associate", "complete", "reobserve"):
        result = case.step()
        assert result["event"]["phase"] == expected
        assert result["event"]["status"] == "active"
    assert len(case.authority.effects) == 5
    result = case.step()
    assert result["event"]["status"] == "completed"
    assert len(result["event"]["correlations"]["authorizations"]) == 5
    before = case.journal.read_bytes()
    assert case.step()["event"]["status"] == "completed"
    assert case.journal.read_bytes() == before


def test_failed_ci_preserves_pending_and_pauses_without_native_completion(case: Case) -> None:
    for _ in range(3):
        case.step()
    case.workflow.producer_status = "blocked"
    result = case.step()
    assert result["event"]["status"] == "paused" and result["event"]["phase"] == "producer"
    assert "complete" not in result["event"]["correlations"]
    assert len(case.authority.effects) == 3


def test_already_satisfied_work_is_superseded_without_worker(case: Case) -> None:
    case.workflow.applicability = "satisfied"
    assert case.step()["event"]["status"] == "superseded"
    assert case.authority.effects == []


@pytest.mark.parametrize("reason", ["blocked", "changed-criteria", "changed-binding"])
def test_current_applicability_blocks_new_effect(case: Case, reason: str) -> None:
    case.workflow.applicability = reason
    assert case.step()["event"]["status"] == "paused"
    assert case.authority.effects == []


def test_revocation_blocks_new_effect_and_does_not_replenish_objective(case: Case) -> None:
    case.step()
    case.authority.available = False
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert len(case.authority.effects) == 1
    assert result["identity"]["contract"]["objective_id"] == DIGEST


def test_pause_and_explicit_resume_recheck_delegated_authority(case: Case) -> None:
    controller.pause(case.journal, case.journal_id, reason="Inspect producer outage.")
    assert case.step()["event"]["status"] == "paused"
    assert not case.authority.effects
    controller.resume(case.journal, case.journal_id)
    case.authority.available = False
    assert case.step()["event"]["status"] == "paused"
    assert not case.authority.effects


def test_cancellation_during_native_effect_retains_receipt_and_stops_next_phase(case: Case) -> None:
    def request_cancel() -> None:
        controller.cancel(case.journal, case.journal_id, reason="Operator revoked work.", workflow=case.workflow)

    case.authority.after_effect = request_cancel
    result = case.step()
    assert result["event"]["status"] == "cancelled"
    assert "execute" in result["event"]["correlations"]
    assert case.workflow.cancellations == 0  # A workflow cancel never bypasses standing native cancellation scope.
    assert len(case.authority.effects) == 1
    assert case.step()["event"]["status"] == "cancelled"
    assert len(case.authority.effects) == 1


def test_producer_backoff_and_deadline_are_bounded(case: Case) -> None:
    for _ in range(3):
        case.step()
    case.workflow.producer_status = "waiting"
    result = case.step()
    assert result["event"]["status"] == "waiting"
    before = case.journal.read_bytes()
    assert case.step() == result
    assert case.journal.read_bytes() == before
    case.current += dt.timedelta(seconds=2)
    result = case.step()
    assert result["event"]["poll_count"] == 2
    case.current += dt.timedelta(hours=1)
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert len(case.authority.effects) == 3


def test_native_completion_without_fresh_satisfied_reobservation_does_not_finish(case: Case) -> None:
    for _ in range(6):
        case.step()
    case.workflow.observed = "blocked"
    assert case.step()["event"]["status"] == "paused"


def test_changed_authorizer_or_runtime_contract_cannot_resume_same_journal(case: Case) -> None:
    case.workflow.contract = copy.deepcopy(case.workflow.contract)
    case.workflow.contract["contract"]["criteria"] = planning.digest("changed")
    with pytest.raises(ValueError, match="contract changed"):
        case.step()
    assert case.authority.effects == []


@pytest.mark.parametrize("delay", ["assess", "proposal", "availability"])
def test_elapsed_preconditions_cannot_release_new_effect_after_workflow_deadline(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    delay: str,
) -> None:
    if delay == "availability":
        original = case.authority.check_available

        def delayed_available() -> Doc:
            case.current += dt.timedelta(hours=2)
            return original()

        monkeypatch.setattr(case.authority, "check_available", delayed_available)
    elif delay == "assess":
        original_assess = case.workflow.assess

        def delayed_assess(correlations: Doc) -> Doc:
            case.current += dt.timedelta(hours=2)
            return original_assess(correlations)

        monkeypatch.setattr(case.workflow, "assess", delayed_assess)
    else:
        original_proposal = case.workflow.proposal

        def delayed_proposal(phase: str, correlations: Doc) -> Doc:
            case.current += dt.timedelta(hours=2)
            return original_proposal(phase, correlations)

        monkeypatch.setattr(case.workflow, "proposal", delayed_proposal)
    assert case.step()["event"]["status"] == "paused"
    assert not case.authority.effects


def test_elapsed_producer_request_cannot_release_read_after_deadline(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(3):
        case.step()
    original = case.workflow.producer_request

    def delayed(correlations: Doc) -> Doc:
        case.current += dt.timedelta(hours=2)
        return original(correlations)

    monkeypatch.setattr(case.workflow, "producer_request", delayed)
    assert case.step()["event"]["status"] == "paused"
    assert not case.authority.reads


def test_elapsed_reconciliation_cannot_release_unsent_request_after_deadline(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise OSError("controlled stop before effect")

    case.authority.before_effect = fail
    with pytest.raises(OSError):
        case.step()
    case.authority.before_effect = None
    original = case.authority.reconcile

    def delayed(kind: str, request: Doc, *, effect_id: str) -> Doc:
        case.current += dt.timedelta(hours=2)
        return original(kind, request, effect_id=effect_id)

    monkeypatch.setattr(case.authority, "reconcile", delayed)
    assert case.step()["event"]["status"] == "paused"
    assert not case.authority.effects


def test_native_cancellation_uses_only_selected_authorizer_and_keeps_unknown_outcome(case: Case) -> None:
    case.authority.result = "uncertain"
    pending = case.step()["event"]["pending"]
    result = controller.cancel(
        case.journal,
        case.journal_id,
        reason="Cancel through approved native scope.",
        workflow=case.workflow,
        authority=case.authority,
    )
    assert result["event"]["status"] == "cancelled"
    assert result["event"]["pending"] == pending
    assert result["event"]["cancellation"]["status"] == "uncertain"
    assert case.authority.cancel_requests == [pending["effect_id"]]
    assert len(case.authority.effects) == 1
    assert case.workflow.cancellations == 0
