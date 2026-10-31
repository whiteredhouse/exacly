"""Real isolated inventory execution and local journals with controlled remote/producer transport.

This integration does not run GitHub CI, evaluate standing scope, claim resource containment or
establish REQ-034 through REQ-039/live unattended acceptance. Explicit trusted fixture decisions precede native
effects. Inventory output is an untrusted artifact; only the separate controlled producer response
can supply the evidence used by native completion.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import DATE, Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import (
    completion,
    dispatch,
    executions,
    handoff,
    handoff_git,
    handoff_store,
    isolation,
    output_association,
    planning,
    remote_publication,
    state_writer,
    worker_integration,
)
from exacly import standing_authorization as authority
from exacly.applicability import Target
from executor_fixtures import ExecutorScenario, make_executor_scenario
from handoff_fixtures import AUTHOR, read_git_file, seed_repository
from isolation_fixtures import isolation_runtime as isolation_runtime
from test_admission import ATTEMPT, BASE, RUN_ID, Source
from test_completion import NoNetwork
from test_handoff_end_to_end import observed_git_revision
from test_reconciliation import selected

Doc = dict[str, Any]
OTHER_TASK = "issues/unrelated-foundations-integration"


def units(attempts: int = 0, wall_time_ms: int = 0) -> Doc:
    return dict(attempts=attempts, wall_time_ms=wall_time_ms, model_requests=0, model_spend_microunits=0)


@dataclass
class Accounting:
    path: Path
    store_id: str
    objective_id: str
    policy_digest: str

    def reserve(self, name: str, grant: Doc, preconditions: Doc, *, cost: Doc | None = None) -> Doc:
        proposal = {
            "schema_version": "0.1.0",
            "id": planning.digest({"effect": name, "grant": grant, "preconditions": preconditions}),
            "delegation_id": "explicit-controlled-fixture-decisions",
            "policy_digest": self.policy_digest,
            "effect": {"operation": name},
            "preconditions": preconditions,
            "authorization": grant,
            "cost": cost if cost is not None else units(),
        }
        return authority.reserve(
            self.path,
            store_id=self.store_id,
            objective_id=self.objective_id,
            proposal=proposal,
            approved=True,
            reason="Explicit trusted fixture decision; no standing-policy evaluator is claimed.",
        )

    def settle(self, decision: Doc, receipt: Doc, *, failed: bool = False) -> Doc:
        return authority.settle(
            self.path,
            store_id=self.store_id,
            objective_id=self.objective_id,
            proposal_id=decision["request"]["proposal"]["id"],
            outcome={"status": "failed" if failed else "succeeded", "receipt": receipt},
        )

    def inspect(self) -> Doc:
        return authority.inspect(self.path, store_id=self.store_id, objective_id=self.objective_id)


@dataclass
class PreparedPath:
    item: ExecutorScenario
    repository: Path
    repository_identity: Doc
    base: str
    worker: worker_integration.PreparedWorker
    accounting: Accounting
    original_pending: list[Doc]


@pytest.fixture
def prepared_path(dispatch_scenario: Scenario, isolation_runtime: isolation.Runtime, tmp_path: Path) -> PreparedPath:
    """Prepare native storage and controlled dispatch, without starting candidate execution."""
    case = dispatch_scenario
    snapshot = state_writer.read(case.state_path)
    state = snapshot.document
    assert state is not None
    state["serial"] += 1
    state["pending"] = [
        {"record": "REQ-1", "dimension": "delivery", "task": OTHER_TASK, "opened_at": "2026-10-26T08:00:00Z"}
    ]
    state["x-foundations-integration"] = {"preserve": ["unrelated observations", {"reviewed": True}]}
    state_writer.publish(snapshot, state)
    directory = tmp_path / "git-operator"
    directory.mkdir(mode=0o700)
    repository = directory / "source.git"
    repository_identity = handoff_git.initialize_repository(repository)
    base = seed_repository(repository, case)
    case.request = replace(case.request, target=selected(base))
    worker = worker_integration.prepare(
        repository,
        repository_id=repository_identity["instance_id"],
        revision=base,
        allowed_paths=["src/service.py"],
        protected_paths=[".github/", "ci/", "operator/", "python/tests/", "spec/", "tests/"],
        model_digest=case.source.policy["model"]["digest"],
    )
    private = tmp_path / "accounting-operator"
    private.mkdir(mode=0o700)
    journal = private / "authority.sqlite3"
    store_id = authority.initialize(journal)["store_id"]
    objective = {
        "schema_version": "0.1.0",
        "id": planning.digest("controlled-foundations-objective"),
        "original_goal": planning.digest(case.plan["actions"][0]),
    }
    authority.enroll(journal, store_id=store_id, objective=objective, limits=units(1, 15000))
    accounting = Accounting(journal, store_id, objective["id"], planning.digest(case.source.policy))
    proposal = dispatch.preview(case.documents, case.state_path, case.request)
    decision = accounting.reserve(
        "dispatch", proposal["authorization_template"], {"state": planning.digest(case.state), "base": base}
    )
    assert decision["approved"]
    # This existing helper invokes native dispatch with the exact previewed authorization.
    item = make_executor_scenario(case, isolation_runtime, tmp_path)
    native = dispatch.show(case.journal, proposal["dispatch_id"])
    assert native["status"] == "registered"
    accounting.settle(decision, native["record"]["events"][-1]["data"]["receipt"])
    policy = {
        **copy.deepcopy(item.policy),
        "argv": list(worker.argv),
        "expected_outputs": list(worker.expected_outputs),
    }
    item.request = replace(item.request, executor_policy=policy, inputs=worker.inputs)
    return PreparedPath(
        item, repository, repository_identity, base, worker, accounting, copy.deepcopy(case.state["pending"])
    )


class ControlledRemote:
    """In-memory checked remote state; this is deliberately not a live transport."""

    adapter = "controlled-foundations-cas-v1"

    def __init__(self) -> None:
        self.revision: str | None = None
        self.operation_id: str | None = None
        self.sends = 0

    def inspect(self, operation: Doc, *, timeout: float) -> Doc:
        assert 0 < timeout <= 300
        return {
            "repository_id": operation["request"]["repository_id"],
            "ref": operation["request"]["ref"],
            "revision": self.revision,
            "commit_present": self.revision == operation["authorization"]["commit"],
            "operation_id": self.operation_id,
        }

    def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
        assert repository.is_dir() and 0 < timeout <= 300
        assert self.revision == operation["request"]["expected_revision"]
        self.revision = operation["authorization"]["commit"]
        self.operation_id = operation["id"]
        self.sends += 1


def fresh_controlled_producer(source: Source, repository: Path, commit: str, tree: str) -> Source:
    """Provide a distinct controlled producer run for the actual handoff revision."""
    fresh = copy.deepcopy(source)
    fresh.calls = []
    observed_git_revision(fresh, repository, commit, tree)
    new_run = RUN_ID + 1
    fresh.run["id"] = fresh.job["run_id"] = fresh.manifest["run_id"] = new_run
    fresh.run["html_url"] = fresh.run["html_url"].rsplit("/", 1)[0] + "/" + str(new_run)
    fresh.artifact["workflow_run"]["id"] = new_run
    fresh.artifact["name"] = f"{fresh.artifact_prefix}-{new_run}-{ATTEMPT}"
    for path in list(fresh.pages):
        prefix = f"{BASE}/actions/runs/{RUN_ID}/"
        if path.startswith(prefix):
            fresh.pages[path.replace(prefix, f"{BASE}/actions/runs/{new_run}/", 1)] = fresh.pages.pop(path)
    fresh.rebundle()
    return fresh


def test_fixed_worker_handoff_remote_association_and_native_completion_preserve_one_objective(
    prepared_path: PreparedPath, tmp_path: Path
) -> None:
    case = prepared_path
    item, accounting = case.item, case.accounting
    before_execution = item.authoritative_bytes()
    execution_grant = item.authorization
    decision = accounting.reserve(
        "execute-inventory",
        execution_grant,
        {"state": planning.digest(item.dispatch.state), "snapshot": case.worker.identity["snapshot_digest"]},
        cost=units(1, 15000),
    )
    assert decision["approved"]
    execution = item.apply(authorization=execution_grant)
    assert execution["status"] == "succeeded", execution
    accounting.settle(decision, execution["receipt"])
    assert item.authoritative_bytes() == before_execution
    bundle = Path(execution["receipt"]["output"])
    runtime_report = json.loads((bundle / "report.json").read_bytes())
    assert runtime_report["argv"] == list(case.worker.argv)
    assert runtime_report["isolation"]["verified"] is True
    assert runtime_report["isolation"]["namespace_terminated"] is True
    inventory_bytes = (bundle / "artifacts/source-digests.json").read_bytes()
    inventory = json.loads(inventory_bytes)
    assert inventory["snapshot_digest"] == case.worker.identity["snapshot_digest"]
    assert inventory["files"] == [
        {key: entry[key] for key in ("path", "size", "digest")} for entry in case.worker.identity["files"]
    ]
    assert inventory["verification_established"] is inventory["evidence_admitted"] is False

    handoff_journal = case.repository.parent / "handoff.sqlite3"
    initialized = handoff_store.initialize(handoff_journal, repository=case.repository_identity)
    policy: Doc = {
        "schema_version": "0.1.0",
        "name": "controlled-inventory-handoff",
        "version": "1",
        "adapter": "local-git-v1",
        "storage": {
            "journal": str(handoff_journal),
            "journal_id": initialized["journal_id"],
            "executor_journal": str(item.journal),
            "executor_journal_id": item.policy["storage"]["journal_id"],
            "repository": str(case.repository),
            "repository_id": case.repository_identity["instance_id"],
        },
        "allowed_paths": ["generated/"],
    }
    request = handoff.Request(
        policy,
        execution["execution_id"],
        1,
        case.base,
        "refs/heads/controlled-inventory",
        None,
        {"artifacts/source-digests.json": "generated/source-digests.json"},
        "Retain controlled inventory artifact\n",
        copy.deepcopy(AUTHOR),
    )
    handoff_grant = handoff.preview(request)["authorization_template"]
    decision = accounting.reserve("handoff", handoff_grant, {"execution": execution["receipt"]["digest"]})
    assert decision["approved"]
    published = handoff.apply(request, authorization=handoff_grant)
    assert published["status"] == "published", published
    receipt = published["receipt"]
    accounting.settle(decision, receipt)
    assert receipt["source_receipt"] == execution["receipt"]["digest"]
    assert read_git_file(case.repository, receipt["commit"], "generated/source-digests.json") == inventory_bytes

    remote_directory = tmp_path / "remote-operator"
    remote_directory.mkdir(mode=0o700)
    remote_journal = remote_directory / "remote.sqlite3"
    remote_request = remote_publication.Request(
        remote_journal,
        remote_publication.initialize(remote_journal),
        handoff_journal,
        initialized["journal_id"],
        published["handoff_id"],
        str(item.dispatch.source.policy["repository"]["id"]),
        ControlledRemote.adapter,
        "refs/heads/nonproduction-inventory",
        None,
    )
    remote_grant = remote_publication.preview(remote_request)["authorization_template"]
    decision = accounting.reserve("controlled-remote-publication", remote_grant, {"handoff": receipt["digest"]})
    assert decision["approved"]
    transport = ControlledRemote()
    remote = remote_publication.apply(remote_request, authorization=remote_grant, transport=transport)
    assert remote["status"] == "published", remote
    accounting.settle(decision, remote)
    assert transport.revision == receipt["commit"]

    association_directory = tmp_path / "association-operator"
    association_directory.mkdir(mode=0o700)
    storage = output_association.initialize(
        association_directory / "association.sqlite3",
        ledger=item.ledger,
        state_path=item.state_path,
        handoff_journal=handoff_journal,
        handoff_journal_id=initialized["journal_id"],
    )
    association_request = output_association.Request(
        storage,
        published["handoff_id"],
        receipt["digest"],
        item.request.binding_id,
        item.documents,
        item.request.plan,
        item.request.action_id,
        item.request.readiness_policy,
        item.request.trust_policy,
        DATE,
    )
    decision = accounting.reserve(
        "associate-output",
        {"association": output_association.identity(association_request)},
        {"state": planning.digest(item.dispatch.state), "binding": item.request.binding_id, "remote": remote["digest"]},
    )
    assert decision["approved"]
    associated = output_association.apply(association_request)
    assert associated["status"] == "associated", associated
    accounting.settle(decision, associated["receipt"])
    assert item.dispatch.state["pending"] == case.original_pending
    assert executions.get(item.ledger, item.request.binding_id)["active"] is False
    replacement = executions.get(item.ledger, associated["receipt"]["binding"])["binding"]
    assert replacement["contract"]["original_target"] == item.dispatch.plan["target"]
    target = Target(receipt["target"])
    arguments: Doc = {
        "source": item.dispatch.source,
        "plan_doc": item.dispatch.plan,
        "action_id": item.request.action_id,
        "readiness_policy": item.request.readiness_policy,
        "trust_policy": item.request.trust_policy,
        "output_target": target,
        "run_id": RUN_ID,
        "attempt": ATTEMPT,
        "date": DATE,
        "ledger": item.ledger,
        "binding_id": replacement["id"],
        "state_path": item.state_path,
    }
    grant = {"binding": replacement["id"], "target": target.data, "run_id": RUN_ID, "attempt": ATTEMPT}
    decision = accounting.reserve("complete-wrong-revision", grant, {"state": planning.digest(item.dispatch.state)})
    assert decision["approved"]
    before_rejected = item.authoritative_bytes()
    rejected = completion.complete(**arguments)
    assert rejected["status"] == "pending" and rejected["admission"]["status"] == "rejected", rejected
    accounting.settle(decision, rejected, failed=True)
    assert item.authoritative_bytes() == before_rejected
    assert item.dispatch.state["pending"] == case.original_pending

    fresh = fresh_controlled_producer(item.dispatch.source, case.repository, receipt["commit"], receipt["tree"])
    arguments.update(source=fresh, run_id=RUN_ID + 1)
    grant = {**grant, "run_id": RUN_ID + 1}
    completion_decision = accounting.reserve(
        "complete-fresh-controlled-producer", grant, {"state": planning.digest(item.dispatch.state)}
    )
    assert completion_decision["approved"]
    before_completion = item.dispatch.state
    completed = completion.complete(**arguments)
    assert completed["status"] == "completed" and completed["admission"]["status"] == "admitted", completed
    accounting.settle(completion_decision, completed["receipt"])
    assert fresh.calls and fresh.downloads
    after = item.dispatch.state
    assert after["pending"] == [entry for entry in case.original_pending if entry["task"] == OTHER_TASK]
    assert after["x-foundations-integration"] == before_completion["x-foundations-integration"]
    assert after["serial"] == before_completion["serial"] + 1
    assert completed["receipt"]["target"] == target.data

    # Native exact replay inspects retained outcomes rather than launching another effect.
    durable = (*item.authoritative_bytes(), item.journal.read_bytes(), accounting.path.read_bytes())
    assert item.apply(authorization=execution_grant)["receipt"] == execution["receipt"]
    assert handoff.apply(request, authorization=handoff_grant)["receipt"] == receipt
    assert remote_publication.apply(remote_request, authorization=remote_grant, transport=transport) == remote
    assert output_association.apply(association_request)["receipt"] == associated["receipt"]
    assert completion.complete(**{**arguments, "source": NoNetwork()})["receipt"] == completed["receipt"]
    assert durable == (*item.authoritative_bytes(), item.journal.read_bytes(), accounting.path.read_bytes())
    assert transport.sends == item.dispatch.client.calls.count("create") == 1
    assert len(execution["history"]["attempts"]) == len(list(item.output_root.rglob("report.json"))) == 1
    assert accounting.inspect()["reserved"] == units(1, 15000)
    exhausted = accounting.reserve(
        "new-revision-new-action",
        {"target": target.data, "action": planning.digest("replan")},
        {"binding": replacement["id"]},
        cost=units(1, 1),
    )
    assert exhausted["approved"] is False and exhausted["status"] == "denied"
    assert accounting.inspect()["reserved"] == units(1, 15000)
