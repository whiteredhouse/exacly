"""Concrete native controller path with controlled worker/remote/producer services.

Real journals, exact Git artifact publication, standing authorization, output association, fresh
native admission and native completion are exercised. This is controlled integration: the worker
boundary, cgroup and producer/transport responses are test doubles and establish no live pilot.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import DATE, Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import (
    admission,
    automation_authorization,
    automation_revocations,
    controller_store,
    dispatch,
    executor,
    executor_runtime,
    executor_store,
    handoff_git,
    handoff_store,
    isolation,
    output_association,
    planning,
    producer_selection,
    remote_publication,
    resource_guard,
    standing_authorization,
    state_writer,
    worker_integration,
)
from exacly import automation_controller as controller
from exacly.refresh import Run, refresh
from executor_fixtures import ExecutorScenario, make_executor_scenario
from handoff_fixtures import AUTHOR, controlled_runtime, seed_repository
from test_admission import RUN_ID
from test_reconciliation import selected
from test_unattended_foundations_integration import ControlledRemote, fresh_controlled_producer

Doc = dict[str, Any]
INPUT = "fixtures/unattended-pilot/inputs/source.txt"
OUTPUT = "fixtures/unattended-pilot/results/source-digests.json"
CURRENT = dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC)


def units(attempts: int, milliseconds: int) -> Doc:
    return {"attempts": attempts, "wall_time_ms": milliseconds, "model_requests": 0, "model_spend_microunits": 0}


def controlled_guard(monkeypatch: pytest.MonkeyPatch) -> resource_guard.Guard:
    from dataclasses import asdict

    bounds = resource_guard.PILOT_BOUNDS
    identity: Doc = {
        "schema_version": "0.1.0",
        "backend": "linux-cgroup2-v1",
        "boot_id": "00000000-0000-4000-8000-000000000001",
        "cgroup": "/controlled-native-test",
        "mount_id": "1",
        "mount_device": "0:1",
        "device": 1,
        "inode": 2,
        "caps": asdict(bounds),
        "bounds": asdict(bounds),
    }
    monkeypatch.setattr(resource_guard, "_facts", lambda selected, path: copy.deepcopy(identity))
    monkeypatch.setattr(resource_guard.Guard, "check_process", lambda guard, pid: guard.check())
    return resource_guard.capture(bounds, expected_cgroup=identity["cgroup"])


@dataclass
class NativeCase:
    item: ExecutorScenario
    workflow: controller.NativeWorkflow
    authority: automation_authorization.Authorizer
    config: Doc
    journal: Path
    journal_id: str
    transport: ControlledRemote
    state_before: bytes
    current: dt.datetime = CURRENT

    def step(self) -> Doc:
        return controller.step(
            self.journal, self.journal_id, workflow=self.workflow, authority=self.authority, clock=lambda: self.current
        )

    def drive(self) -> Doc:
        for _ in range(20):
            result = self.step()
            event = result["event"]
            if event["status"] in {"paused", "cancelled", "completed", "superseded"}:
                return result
            self.current += dt.timedelta(seconds=30)
        raise AssertionError("controlled native workflow exceeded finite schedule budget")


@pytest.fixture
def native_case(
    dispatch_scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> NativeCase:
    case = dispatch_scenario
    # Select the reviewed nonproduction push branch before hashing its producer/observer policy.
    source = case.source
    original_branch = source.branch
    source.branch = "pilot/controlled-native"
    source.policy["branch"] = source.branch
    source.producer["branch"] = source.branch
    source.run["head_branch"] = source.artifact["workflow_run"]["head_branch"] = source.branch
    repository_path = "repos/" + source.policy["repository"]["full_name"]
    branch = source.pages.pop(f"{repository_path}/branches/{original_branch}")
    branch["name"] = source.branch
    source.pages[f"{repository_path}/branches/{source.branch}"] = branch
    source.workflow = source.workflow.replace(b"branches: [main]", b"branches: [pilot/controlled-native]")
    workflow_entry = source.add_blob(source.workflow_path, source.workflow)
    source.tree[:] = [workflow_entry if item["path"] == source.workflow_path else item for item in source.tree]
    source.policy["workflow"]["digest"] = admission.sha256(source.workflow)
    source.policy["observer"]["digest"] = admission.observer_digest(source.tree, source.observer_paths)
    directory = tmp_path / "git-operator"
    directory.mkdir(mode=0o700)
    repository = directory / "source.git"
    repository_identity = handoff_git.initialize_repository(repository)
    seed = seed_repository(repository, case)
    prepared_git = handoff_git.prepare(
        repository,
        seed,
        {INPUT: b"reviewed fixed pilot fixture\n"},
        author=AUTHOR,
        message="Prepare approved nonproduction consumer fixture",
    )
    handoff_git.write_objects(repository, prepared_git)
    base = prepared_git["commit"]
    handoff_git.compare_and_swap(repository, "refs/heads/base", seed, base)
    target = selected(base)
    plan = planning.create(
        {**case.documents, "__current_state__": case.state}, case.request.readiness_policy, target, DATE
    )
    case.plan = plan
    case.request = replace(case.request, plan=plan, action_id=plan["actions"][0]["id"], target=target)
    root = tmp_path / "controlled-unused-rootfs"
    root.mkdir(mode=0o700)
    runtime = isolation.Runtime(root, "sha256:" + "1" * 64, "/usr/bin/python3")
    fresh = getattr(request, "param", False) is True
    anchor_dir = tmp_path / "ledger-anchor"
    anchor_dir.mkdir(mode=0o700)
    if fresh:
        anchor = controller_store.initialize_ledger_anchor(
            anchor_dir / "anchor.json", ledger=case.ledger, create_ledger=True
        )
        executor_journal = case.journal.parent / "executor.sqlite3"
        initialized_executor = executor_store.initialize(executor_journal, ledger=case.ledger, max_concurrency=1)
        output_root = tmp_path / "executor-outputs"
        output_root.mkdir(mode=0o700)
        policy: Doc = {
            "schema_version": "0.1.0",
            "name": "controlled-fresh-native-executor",
            "version": "1",
            "adapter": "local-bubblewrap-v1",
            "storage": {
                "journal": str(executor_journal),
                "journal_id": initialized_executor["journal_id"],
                "ledger": str(case.ledger),
                "state": str(case.state_path),
                "output_root": str(output_root),
            },
            "runtime": {
                "schema_version": "0.1.0",
                "backend": isolation.BACKEND,
                "rootfs": str(runtime.rootfs),
                "rootfs_digest": runtime.rootfs_digest,
                "python": runtime.python,
                "bubblewrap": str(runtime.bubblewrap),
            },
            "argv": [runtime.python, "-I", "-S", "/inputs/candidate.py"],
            "expected_outputs": ["result.txt"],
            "max_attempts": 2,
            "timeout_seconds": 15,
            "permitted_repository_effects": [],
        }
        native_request = executor.Request(
            plan,
            plan["actions"][0]["id"],
            case.request.readiness_policy,
            case.request.trust_policy,
            policy,
            target,
            DATE,
            planning.digest("not-yet-dispatched"),
            {},
        )
        item = ExecutorScenario(case, runtime, executor_journal, output_root, policy, native_request)
    else:
        item = make_executor_scenario(case, runtime, tmp_path)
        anchor = controller_store.initialize_ledger_anchor(anchor_dir / "anchor.json", ledger=item.ledger)
    worker = worker_integration.prepare(
        repository,
        repository_id=repository_identity["instance_id"],
        revision=base,
        allowed_paths=[INPUT],
        protected_paths=[".github/", "ci/", "python/", "spec/"],
        model_digest=case.source.policy["model"]["digest"],
    )
    guard = controlled_guard(monkeypatch)
    policy = {
        **copy.deepcopy(item.policy),
        "argv": list(worker.argv),
        "expected_outputs": list(worker.expected_outputs),
        "resource_guard": guard.descriptor,
    }
    item.request = replace(item.request, executor_policy=policy, inputs=worker.inputs)
    item.policy = policy
    # Explicit controlled physical boundary. Native executor contracts/journals still run unchanged.
    payload = json.dumps(
        {
            "snapshot_digest": worker.identity["snapshot_digest"],
            "files": [{key: entry[key] for key in ("path", "size", "digest")} for entry in worker.identity["files"]],
        },
        sort_keys=True,
    ).encode()
    controlled = controlled_runtime(payload)

    def runtime_result(*args: Any, **kwargs: Any) -> Doc:
        result: Doc = controlled(*args, **kwargs)
        out = args[3]
        (out / "artifacts/result.txt").rename(out / "artifacts/source-digests.json")
        report = result["report"]
        report["artifacts"][0]["path"] = "artifacts/source-digests.json"
        report.pop("digest")
        report["digest"] = planning.digest(report)
        (out / "report.json").write_text(json.dumps(report))
        return result

    monkeypatch.setattr(executor_runtime, "run", runtime_result)
    handoff_journal = directory / "handoff.sqlite3"
    handoff_id = handoff_store.initialize(handoff_journal, repository=repository_identity)["journal_id"]
    handoff_policy = {
        "schema_version": "0.1.0",
        "name": "controlled-native-pilot",
        "version": "1",
        "adapter": "local-git-v1",
        "storage": {
            "journal": str(handoff_journal),
            "journal_id": handoff_id,
            "executor_journal": str(item.journal),
            "executor_journal_id": policy["storage"]["journal_id"],
            "repository": str(repository),
            "repository_id": repository_identity["instance_id"],
        },
        "allowed_paths": [OUTPUT],
    }
    remote_dir = tmp_path / "remote-operator"
    remote_dir.mkdir(mode=0o700)
    remote_path = remote_dir / "remote.sqlite3"
    remote_id = remote_publication.initialize(remote_path)
    assoc_dir = tmp_path / "association-operator"
    assoc_dir.mkdir(mode=0o700)
    association = output_association.initialize(
        assoc_dir / "association.sqlite3",
        ledger=item.ledger,
        state_path=item.state_path,
        handoff_journal=handoff_journal,
        handoff_journal_id=handoff_id,
    )
    authority_dir = tmp_path / "standing-operator"
    authority_dir.mkdir(mode=0o700)
    accounting_path, revocation_path = authority_dir / "accounting.sqlite3", authority_dir / "revocations.sqlite3"
    store_id = standing_authorization.initialize(accounting_path)["store_id"]
    revocation_id = automation_revocations.initialize(revocation_path)
    config_path = authority_dir / "delegation.json"
    action = plan["actions"][0]
    objective = {
        "schema_version": "0.1.0",
        "id": planning.digest("controlled-native-controller-objective"),
        "original_goal": planning.digest(action),
    }
    native_stores = {
        "execute": {"path": str(item.journal), "id": policy["storage"]["journal_id"]},
        "handoff": {"path": str(handoff_journal), "id": handoff_id},
        "publish": {"path": str(remote_path), "id": remote_id},
        "associate": {"path": association["journal"], "id": association["journal_id"]},
        "complete": {"path": str(item.ledger), "id": anchor["digest"]},
    }
    if fresh:
        native_stores["dispatch"] = {"path": str(case.journal), "id": case.policy["storage"]["journal_id"]}
    contracts = {
        "model": plan["inputs"]["config"]["digest"],
        "criteria": action["criteria_digest"],
        "bindings": action["bindings_digest"],
        "readiness": planning.digest(item.request.readiness_policy),
        "trust": planning.digest(item.request.trust_policy),
        "observer": item.request.trust_policy["observer"]["digest"],
    }
    costs = {kind: units(1, 30000) for kind in native_stores}
    capabilities = {
        kind: sorted(
            ["intent-before-effects-v1", controller.ADAPTERS[kind]]
            + (["finish-prepared-v1"] if kind in {"handoff", "associate"} else [])
        )
        for kind in native_stores
    }
    config: Doc = {
        "schema_version": "0.1.0",
        "id": "controlled-native-pilot",
        "issuer": "independent-controlled-test-review",
        "approval_id": "controlled-fixture-approval",
        "config_path": str(config_path),
        "accounting": {"path": str(accounting_path), "store_id": store_id},
        "revocations": {"path": str(revocation_path), "store_id": revocation_id},
        "objective": objective,
        "limits": units(6 if fresh else 5, 2000000),
        "not_before": (CURRENT - dt.timedelta(days=1)).isoformat(),
        "expires_at": (CURRENT + dt.timedelta(days=1)).isoformat(),
        "contract": contracts,
        "repository": case.source.policy["repository"],
        "base_revision": base,
        "allowed_paths": [INPUT, OUTPUT],
        "protected_paths": [".github/", "ci/", "python/", "spec/"],
        "destinations": [
            {"repository_id": case.source.policy["repository"]["id"], "ref": "refs/heads/pilot/controlled-native"}
        ],
        "actions": [action["id"]],
        "effects": {
            kind: {
                "adapter_id": controller.ADAPTERS[kind],
                "capabilities": capabilities[kind],
                "native_store": native_stores[kind],
                "max_cost": costs[kind],
                "cancel_inflight": False,
                "recover_after_revocation": True,
                "recover_after_expiry": True,
            }
            for kind in native_stores
        },
    }
    producer_contract = {
        "schema_version": "0.1.0",
        "workflow_id": case.source.run["workflow_id"],
        "policy": case.source.policy,
        "policy_digest": planning.digest(case.source.policy),
    }
    baseline_contract = {
        "schema_version": "0.1.0",
        "producer_contract_digest": planning.digest(producer_contract),
        "target": plan["target"],
        "plan_digest": plan["digest"],
        "action_id": action["id"],
        "readiness_digest": planning.digest(item.request.readiness_policy),
        "trust_digest": planning.digest(item.request.trust_policy),
        "run_id": RUN_ID,
        "attempt": 1,
    }
    negative_contract = {
        **{key: value for key, value in baseline_contract.items() if key not in {"run_id", "attempt"}},
        "cases": list(controller.NEGATIVE_CASES),
        "unavailable_run_id": 1,
        "wrong_attempt": 2,
        "criteria_model_digest": planning.digest("explicitly approved incompatible model expectation"),
        "verifier_digest": planning.digest("explicitly approved incompatible observer expectation"),
        "wall_time_ms": 45000,
    }
    config["reads"] = {
        "producer-observe": {
            "adapter_id": "native-producer-observer-v1",
            "max_wall_time_ms": 30000,
            "contract_digest": planning.digest(producer_contract),
        }
    }
    config["reads"]["baseline-admission"] = {
        "adapter_id": "native-baseline-admission-v1",
        "max_wall_time_ms": 30000,
        "contract_digest": planning.digest(baseline_contract),
    }
    config["reads"]["negative-admission"] = {
        "adapter_id": "native-negative-admission-v1",
        "max_wall_time_ms": 45000,
        "contract_digest": planning.digest(negative_contract),
    }
    config_path.write_text(planning.canonical(config))
    config_path.chmod(0o600)
    authority_identity = {
        "schema_version": "0.1.0",
        "delegation_id": config["id"],
        "delegation_digest": planning.digest(config),
        "issuer": config["issuer"],
        "approval_id": config["approval_id"],
        "store": {"path": str(accounting_path), "id": store_id},
        "objective": {key: objective[key] for key in ("id", "original_goal")},
    }
    control_dir = tmp_path / "controller-operator"
    control_dir.mkdir(mode=0o700)
    controller_identity = {"journal": str(control_dir / "workflow.sqlite3"), "journal_id": uuid.uuid4().hex}
    host = controller.host_identity(run_id="controlled-run", job_id="controlled-native-job")
    components: Doc = {
        "schema_version": "0.1.0",
        "profile": "single-producer-local-v1",
        "required_members": ["producer"],
        "controller": controller_identity,
        "baseline": baseline_contract,
        "negatives": negative_contract,
        "configuration_path": str(authority_dir / "native-components.json"),
        "date": DATE.isoformat(),
        "state": str(item.state_path),
        "ledger": str(item.ledger),
        "initial_binding_id": None if fresh else item.request.binding_id,
        "ledger_anchor": anchor,
        "dispatch_policy": case.policy,
        "executor_policy": policy,
        "handoff_policy": handoff_policy,
        "handoff": {
            "ref": "refs/heads/controlled-result",
            "expected_revision": None,
            "mappings": {"artifacts/source-digests.json": OUTPUT},
            "message": "Retain approved inventory fixture\n",
            "author": AUTHOR,
        },
        "remote": {
            "schema_version": "0.1.0",
            "journal": str(remote_path),
            "journal_id": remote_id,
            "handoff_journal": str(handoff_journal),
            "handoff_journal_id": handoff_id,
            "repository_id": str(case.source.policy["repository"]["id"]),
            "adapter": ControlledRemote.adapter,
            "ref": "refs/heads/pilot/controlled-native",
            "expected_revision": None,
        },
        "association": association,
        "producer": {
            "schema_version": "0.1.0",
            "workflow_id": case.source.run["workflow_id"],
            "policy": case.source.policy,
            "policy_digest": planning.digest(case.source.policy),
        },
        "native_stores": native_stores,
        "costs": costs,
    }
    if getattr(request, "param", None) == "preflight":
        components["publisher"] = {
            "environment": "EXACLY_REQ039_PUBLISH_TOKEN",
            "present": True,
            "scope_verified": True,
            "app_id": 42,
            "installation_id": 81,
            "expires_at": (CURRENT + dt.timedelta(days=1)).isoformat(),
            "issuer_evidence": "trusted-workflow-mint-metadata",
            "issuer_server_verified": False,
        }
        components["authority_preflight"] = {
            "schema_version": "0.1.0",
            "adapter_id": "req039-authority-preflight-v1",
            "repository": case.source.policy["repository"],
            "coordinator": {**host, "run_attempt": "1", "nonce": "1" * 32, "workflow_id": 101, "revision": base},
            "producer_policy_digest": planning.digest(case.source.policy),
            "resource_guard_digest": guard.digest,
            "publisher": components["publisher"],
            "approval_channel": {
                "repository": case.source.policy["repository"]["full_name"],
                "issue": 93,
                "user_id": 1679024,
            },
            "wall_time_ms": 45000,
            "max_producer_reads": 20,
        }
    components["configuration_digest"] = planning.digest(components)
    # Components precede independent delegation approval; their own digest is identification only.
    config["native_configuration_digest"] = components["configuration_digest"]
    config_path.write_text(planning.canonical(config))
    authority_identity["delegation_digest"] = planning.digest(config)
    contract = controller.native_contract(
        plan=plan,
        record="REQ-1",
        readiness_policy=item.request.readiness_policy,
        trust_policy=item.request.trust_policy,
        components=components,
        authorizer_identity=authority_identity,
        host=host,
        deadline_at=(CURRENT + dt.timedelta(hours=1)).isoformat(),
    )

    class Remote(ControlledRemote):
        def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
            super().publish(operation, repository, timeout=timeout)
            fresh = fresh_controlled_producer(
                case.source, repository, operation["authorization"]["commit"], operation["handoff"]["tree"]
            )
            repository_path = "repos/" + fresh.policy["repository"]["full_name"]
            fresh.pages[f"{repository_path}/actions/workflows/{fresh.run['workflow_id']}/runs"] = {
                "total_count": 1,
                "workflow_runs": [fresh.run],
            }
            fresh.pages[f"{repository_path}/actions/runs/{fresh.run['id']}"] = fresh.run
            case.source.__dict__.update(fresh.__dict__)

    transport = Remote()
    services = controller.NativeServices(
        case.source,
        transport,
        worker,
        guard,
        producer_selection,
        dispatch_client=case.client,
        boundary=lambda operation, timeout: operation(),
        clock=lambda: CURRENT,
    )
    workflow = controller.NativeWorkflow(
        contract=contract,
        documents=item.documents,
        plan=plan,
        readiness_policy=item.request.readiness_policy,
        trust_policy=item.request.trust_policy,
        components=components,
        services=services,
    )
    authority = automation_authorization.Authorizer(
        config_path,
        approved_digest=planning.digest(config),
        issuer=config["issuer"],
        approval_id=config["approval_id"],
        effects=workflow.effect_registry(),
        readers=workflow.reader_registry(),
        clock=lambda: CURRENT,
    )
    assert authority.identity() == authority_identity
    initialized = controller.initialize(control_dir / "workflow.sqlite3", contract)
    return NativeCase(
        item,
        workflow,
        authority,
        config,
        Path(initialized["journal"]),
        initialized["journal_id"],
        transport,
        item.state_path.read_bytes(),
    )


def test_concrete_controller_reaches_only_fresh_native_completion_and_reobservation(native_case: NativeCase) -> None:
    case = native_case
    result = case.drive()
    assert result["event"]["status"] == "completed", result["event"]["reason"]
    correlations = result["event"]["correlations"]
    assert set(("execute", "handoff", "publish", "associate", "complete")) <= correlations.keys()
    assert correlations["complete"]["target"] == correlations["handoff"]["target"]
    assert correlations["producer_selection"]["run_id"] == RUN_ID + 1
    assert correlations["complete"]["evidence"]["admission_digest"]
    assert case.item.dispatch.source.calls and case.item.dispatch.source.downloads
    assert case.transport.sends == 1
    after = case.item.dispatch.state
    assert not after.get("pending")
    assert after["serial"] == json.loads(case.state_before)["serial"] + 1
    accounting = standing_authorization.inspect(
        Path(case.config["accounting"]["path"]),
        store_id=case.config["accounting"]["store_id"],
        objective_id=case.config["objective"]["id"],
    )
    assert accounting["reserved"]["attempts"] == 5
    assert 150000 < accounting["reserved"]["wall_time_ms"] <= case.config["limits"]["wall_time_ms"]
    assert len(correlations["authorizations"]) == 5
    assert case.step()["event"]["status"] == "completed"
    assert case.transport.sends == 1


def test_concrete_pipeline_does_not_complete_from_worker_git_or_remote_success(native_case: NativeCase) -> None:
    case = native_case
    for phase in ("handoff", "publish", "producer"):
        result = case.step()
        assert result["event"]["phase"] == phase
        assert case.item.state_path.read_bytes() == case.state_before
    # Exact CI selection is separate from publication and fresh native admission is still required.
    result = case.step()
    assert result["event"]["phase"] == "producer"
    assert "producer_selection" in result["event"]["correlations"]
    assert case.item.state_path.read_bytes() == case.state_before


def test_revocation_stops_concrete_next_phase_while_retaining_original_objective(native_case: NativeCase) -> None:
    case = native_case
    case.step()
    automation_revocations.revoke(
        Path(case.config["revocations"]["path"]),
        store_id=case.config["revocations"]["store_id"],
        delegation_id=case.config["id"],
        reason="Controlled independent revocation.",
    )
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert result["event"]["phase"] == "handoff"
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


def advance_to_producer(case: NativeCase) -> None:
    for _ in range(3):
        case.step()
    assert controller.show(case.journal, case.journal_id)["event"]["phase"] == "producer"


@pytest.mark.parametrize("failure", ["failed-ci", "wrong-revision", "wrong-attempt", "unavailable"])
def test_exact_producer_failures_keep_native_pending_without_completion(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from exacly.github_evidence import GitHubError

    case = native_case
    advance_to_producer(case)
    source = case.item.dispatch.source
    if failure == "failed-ci":
        source.run["conclusion"] = source.job["conclusion"] = "failure"
    elif failure == "wrong-revision":
        source.run["head_sha"] = "f" * 40
    elif failure == "wrong-attempt":
        source.run["run_attempt"] = 2
    else:

        def unavailable(path: str) -> Doc:
            raise GitHubError("controlled producer unavailable")

        monkeypatch.setattr(source, "get_json", unavailable)
    result = case.drive()
    assert result["event"]["status"] == "paused", result
    assert result["event"]["phase"] == "producer"
    assert "complete" not in result["event"]["correlations"]
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 1


def test_changed_approved_criteria_pauses_before_next_native_effect(native_case: NativeCase) -> None:
    case = native_case
    case.step()
    documents = copy.deepcopy(case.workflow.documents)
    for document in documents.values():
        for record in document.get("records", []):
            if record["id"] == "REQ-1":
                record["statement"] += " Changed after original approval."
    case.workflow.documents = documents
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert result["event"]["phase"] == "handoff"
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


def test_native_objective_budget_exhaustion_blocks_later_phase_without_controller_reset(
    native_case: NativeCase,
) -> None:
    case = native_case
    case.step()
    accounting = standing_authorization.inspect(
        Path(case.config["accounting"]["path"]),
        store_id=case.config["accounting"]["store_id"],
        objective_id=case.config["objective"]["id"],
    )
    remaining_wall = case.config["limits"]["wall_time_ms"] - accounting["reserved"]["wall_time_ms"]
    proposal = {
        "schema_version": "0.1.0",
        "id": planning.digest("another-independent-approved-reservation"),
        "delegation_id": "independent-trusted-fixture-effect",
        "policy_digest": planning.digest(case.config),
        "effect": {"operation": "other-approved-effect"},
        "preconditions": {},
        "authorization": {"scope": "explicit-controlled-test"},
        "cost": units(4, remaining_wall),
    }
    reserved = standing_authorization.reserve(
        Path(case.config["accounting"]["path"]),
        store_id=case.config["accounting"]["store_id"],
        objective_id=case.config["objective"]["id"],
        proposal=proposal,
        approved=True,
        reason="Independent controlled reservation consumes same objective capacity.",
    )
    assert reserved["approved"] is True
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert result["event"]["phase"] == "handoff"
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


def test_worker_failure_cannot_be_salvaged_from_complete_artifact(
    native_case: NativeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = native_case
    controlled = executor_runtime.run

    def failed(*args: Any, **kwargs: Any) -> Doc:
        result = controlled(*args, **kwargs)
        result["status"] = "candidate_failed"
        return result

    monkeypatch.setattr(executor_runtime, "run", failed)
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert result["event"]["phase"] == "execute"
    assert "handoff" not in result["event"]["correlations"]
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


def test_lost_handoff_response_reconciles_native_journal_without_duplicate_effect(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exacly import handoff

    case = native_case
    case.step()
    original = handoff.apply
    calls = 0

    def lost(*args: Any, **kwargs: Any) -> Doc:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        raise OSError("controlled response lost after exact native publication")

    with monkeypatch.context() as patch:
        patch.setattr(handoff, "apply", lost)
        result = case.step()
    assert result["event"]["status"] == "recovering"
    case.current += dt.timedelta(seconds=30)
    result = case.step()
    assert result["event"]["phase"] == "publish"
    assert calls == 1
    assert "handoff" in result["event"]["correlations"]
    assert case.drive()["event"]["status"] == "completed"
    assert case.transport.sends == 1


@pytest.mark.parametrize("revoke", [False, True])
def test_prepared_association_finishes_only_under_current_explicit_recovery_scope(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
) -> None:
    from exacly import execution_store

    case = native_case
    advance_to_producer(case)
    case.step()  # Persist exact producer selection.
    case.current += dt.timedelta(seconds=30)
    assert case.step()["event"]["phase"] == "associate"

    def stop(*args: Any, **kwargs: Any) -> Doc:
        raise OSError("controlled interruption after association intent before native binding publication")

    with monkeypatch.context() as patch:
        patch.setattr(execution_store, "put_locked", stop)
        result = case.step()
    assert result["event"]["status"] == "recovering"
    pending = result["event"]["pending"]
    if revoke:
        automation_revocations.revoke(
            Path(case.config["revocations"]["path"]),
            store_id=case.config["revocations"]["store_id"],
            delegation_id=case.config["id"],
            reason="Revoke prepared-phase completion authority.",
        )
    case.current += dt.timedelta(seconds=30)
    result = case.step()
    if revoke:
        assert result["event"]["status"] == "paused"
        assert result["event"]["pending"] == pending
        assert case.item.state_path.read_bytes() == case.state_before
    else:
        assert result["event"]["phase"] == "complete", result
        assert case.drive()["event"]["status"] == "completed"
    accounting = standing_authorization.inspect(
        Path(case.config["accounting"]["path"]),
        store_id=case.config["accounting"]["store_id"],
        objective_id=case.config["objective"]["id"],
    )
    assert accounting["reserved"]["attempts"] == (4 if revoke else 5)
    assert case.transport.sends == 1


def test_replacement_handoff_journal_cannot_prove_original_operation_absent(native_case: NativeCase) -> None:
    import sqlite3

    case = native_case
    result = case.step()
    request = case.workflow.proposal("handoff", result["event"]["correlations"])
    native = case.workflow.components["native_stores"]["handoff"]
    with sqlite3.connect(native["path"]) as connection:
        connection.execute(
            "UPDATE handoff_journal SET value=? WHERE key='journal_id'", ("00112233445546778899aabbccddeeff",)
        )
    with pytest.raises(ValueError, match="handoff journal instance"):
        case.workflow.effect_registry()["handoff"].recover(request, request["authorization"])
    assert case.item.state_path.read_bytes() == case.state_before


@pytest.mark.parametrize("component", ["remote", "dispatch"])
def test_cross_repository_component_cannot_be_relabelled_as_approved_producer(
    native_case: NativeCase,
    component: str,
) -> None:
    case = native_case
    configuration = copy.deepcopy(case.workflow.components)
    if component == "remote":
        configuration["remote"]["repository_id"] = "999999"
    else:
        configuration["dispatch_policy"] = copy.deepcopy(case.item.dispatch.policy)
        configuration["dispatch_policy"]["repository"] = {"id": 999999, "full_name": "another/unapproved"}
    configuration.pop("configuration_digest")
    configuration["configuration_digest"] = planning.digest(configuration)
    contract = copy.deepcopy(case.workflow.contract)
    contract["stores"]["configuration"]["id"] = configuration["configuration_digest"]
    with pytest.raises(ValueError, match="repository differs"):
        controller.NativeWorkflow(
            contract=contract,
            documents=case.workflow.documents,
            plan=case.workflow.plan,
            readiness_policy=case.workflow.readiness,
            trust_policy=case.workflow.trust,
            components=configuration,
            services=case.workflow.services,
        )
    assert case.transport.sends == 0


def test_cancel_after_delegation_release_before_native_handoff_vetoes_worker_without_fake_receipt(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exacly import executor_store

    case = native_case
    original = controller.NativeEffect.invoke

    def cancel_before_native(effect: controller.NativeEffect, request: Doc, authorization: Doc) -> Doc:
        event = controller.show(case.journal, case.journal_id)["event"]
        assert event["pending"]["released"] is True
        assert event["pending"].get("native_released") is not True
        controller.cancel(
            case.journal,
            case.journal_id,
            reason="Cancel after delegated release before worker launch.",
            workflow=case.workflow,
        )
        return original(effect, request, authorization)

    with monkeypatch.context() as patch:
        patch.setattr(controller.NativeEffect, "invoke", cancel_before_native)
        result = case.step()
    assert result["event"]["status"] == "cancelled"
    pending = result["event"]["pending"]
    assert pending and "execute" not in result["event"]["correlations"]
    native = case.workflow.components["native_stores"]["execute"]
    with executor_store.opened(Path(native["path"]), write=False) as journal:
        assert journal.get(pending["request"]["authorization"]["execution_id"]) is None
    result = controller.recover(
        case.journal, case.journal_id, workflow=case.workflow, authority=case.authority, clock=lambda: case.current
    )
    assert result["event"]["status"] == "cancelled"
    assert result["event"]["last_authorization"]["status"] == "not_started"
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


@pytest.mark.parametrize("native_case", [True], indirect=True)
def test_fresh_refresh_dispatch_through_native_completion_uses_anchored_ledger(native_case: NativeCase) -> None:
    case = native_case
    assert not json.loads(case.state_before).get("pending")
    assert case.workflow.contract["initial_binding_id"] is None
    result = case.drive()
    assert result["event"]["status"] == "completed", result["event"]["reason"]
    correlations = result["event"]["correlations"]
    assert correlations["dispatch"]["binding_id"] == correlations["original_binding_id"]
    assert case.item.dispatch.client.calls.count("create") == 1
    assert case.transport.sends == 1
    assert not case.item.dispatch.state.get("pending")
    assert case.item.dispatch.state["serial"] == json.loads(case.state_before)["serial"] + 2
    assert controller_store.check_ledger_anchor(case.workflow.components["ledger_anchor"])
    assert len(correlations["authorizations"]) == 6


@pytest.mark.parametrize("native_case", [True], indirect=True)
def test_replaced_empty_ledger_blocks_dispatch_before_external_effect(native_case: NativeCase) -> None:
    import shutil

    case = native_case
    ledger = case.item.ledger
    backup = ledger.with_name("retained-original.sqlite3")
    ledger.rename(backup)
    shutil.copyfile(backup, ledger)
    ledger.chmod(0o600)
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert "physical identity" in result["event"]["reason"]
    assert not case.item.dispatch.client.issues
    assert case.transport.sends == 0
    assert case.item.state_path.read_bytes() == case.state_before


@pytest.mark.parametrize("at_phase", ["associate", "complete"])
def test_remote_ref_drift_after_exact_ci_selection_blocks_new_native_effect(
    native_case: NativeCase,
    at_phase: str,
) -> None:
    case = native_case
    advance_to_producer(case)
    case.step()
    case.current += dt.timedelta(seconds=30)
    assert case.step()["event"]["phase"] == "associate"
    if at_phase == "complete":
        assert case.step()["event"]["phase"] == "complete"
    case.transport.revision = "f" * 40
    result = case.step()
    assert result["event"]["status"] == "paused", result["event"]["reason"]
    assert result["event"]["phase"] == at_phase
    assert case.transport.revision == "f" * 40
    assert case.item.state_path.read_bytes() == case.state_before
    assert "complete" not in result["event"]["correlations"]


def test_real_supervisor_identity_and_cleanup_are_durable_before_native_receipt_acceptance(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = native_case
    case.workflow.services = replace(case.workflow.services, boundary=controller.bounded_native_call)
    requests: list[Doc] = []
    original = controller.NativeEffect.invoke

    def remember(effect: controller.NativeEffect, request: Doc, authorization: Doc) -> Doc:
        requests.append(request)
        return original(effect, request, authorization)

    monkeypatch.setattr(controller.NativeEffect, "invoke", remember)
    result = case.step()
    assert result["event"]["phase"] == "handoff", result["event"]["reason"]
    supervisors = result["event"]["supervisors"]
    assert len(supervisors) == 1 and supervisors[0]["phase"] == "terminated"
    assert supervisors[0]["identity"]["process"]["boot_id"] == case.workflow.contract["host"]["boot_id"]
    assert supervisors[0]["request_digest"] == planning.digest(requests[0])
    # Missing cleanup is an uncertainty even when the component already has a retained receipt.
    with controller_store.opened(case.journal, case.journal_id, write=True) as journal:
        event = journal.read()["event"]
        journal.append({**event, "supervisors": [{**supervisors[0], "phase": "unresolved"}]})
    outcome = case.workflow.effect_registry()["execute"].recover(requests[0], requests[0]["authorization"])
    assert outcome["status"] == "uncertain" and "cleanup" in outcome["reason"]


def test_real_controller_death_before_native_intent_recovers_watchdog_cleanup_before_retry(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import multiprocessing
    import os
    import signal
    import time

    from exacly import executor_store

    case = native_case
    case.workflow.services = replace(case.workflow.services, boundary=controller.bounded_native_call)
    context = multiprocessing.get_context("fork")
    released = context.Event()
    original = controller.NativeEffect._invoke

    def stop_before_component(effect: controller.NativeEffect, request: Doc, authorization: Doc) -> Doc:
        effect._release_native(request)
        released.set()
        time.sleep(2)
        return original(effect, request, authorization)

    with monkeypatch.context() as patch:
        patch.setattr(controller.NativeEffect, "_invoke", stop_before_component)
        process = context.Process(target=case.step)
        process.start()
        assert released.wait(10)
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(5)
        assert process.exitcode == -signal.SIGKILL
    for _ in range(100):
        event = controller.show(case.journal, case.journal_id)["event"]
        if event.get("supervisors") and all(item["phase"] == "terminated" for item in event["supervisors"]):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("independent watchdog did not retain exact descendant cleanup")
    assert event["pending"]
    request = event["pending"]["request"]
    native = case.workflow.components["native_stores"]["execute"]
    with executor_store.opened(Path(native["path"]), write=False) as journal:
        assert journal.get(request["authorization"]["execution_id"]) is None
    # Same phase/effect ID recovers authoritative absence only after the old supervisor cleanup.
    result = case.step()
    assert result["event"]["phase"] == "handoff", result["event"]["reason"]
    with executor_store.opened(Path(native["path"]), write=False) as journal:
        work = journal.get(request["authorization"]["execution_id"])
        assert work is not None and len(work["attempts"]) == 1


def test_explicit_metered_baseline_reader_authenticates_failure_without_completing_or_writing_state(
    native_case: NativeCase,
) -> None:
    from test_handoff_end_to_end import observed_git_revision
    from test_reconciliation import change_live_outcome

    case = native_case
    source = case.item.dispatch.source
    prepared = case.workflow.services.prepared.identity
    observed_git_revision(source, Path(prepared["repository"]["path"]), prepared["revision"], prepared["tree"])
    test_file, test_name = source.case.split("::")
    source.report = (
        f'<testsuite><testcase file="{test_file}" name="{test_name}"><failure/></testcase></testsuite>'.encode()
    )
    change_live_outcome(source, "failure")
    state_before = case.item.state_path.read_bytes()
    request = case.workflow.baseline_request({"original_binding_id": case.item.request.binding_id}, run_id=RUN_ID)
    result = case.authority.call_metered(
        "baseline-admission", request, operation_id=planning.digest("initial-failed-producer")
    )
    assert result["status"] == "completed", result
    observation = result["receipt"]["result"]
    assert observation["status"] == "observed" and observation["pending_unchanged"] is True
    assert observation["negative_established"] is True
    assert observation["producer"]["conclusion"] == "failure"
    import base64

    assert (
        admission.sha256(base64.b64decode(observation["evidence"]["archive_b64"]))
        == observation["report"]["admission"]["origin"]["archive_digest"]
    )
    assert (
        admission.sha256(base64.b64decode(observation["evidence"]["report_b64"]))
        == observation["report"]["admission"]["origin"]["report_digest"]
    )
    assert observation["report"]["status"] == "pending"
    assert observation["report"]["admission"]["status"] == "admitted"
    assert observation["report"]["readiness"]["result"] != "satisfied"
    assert case.item.state_path.read_bytes() == state_before
    assert case.transport.sends == 0


def accounting_state(case: NativeCase) -> Doc:
    return standing_authorization.inspect(
        Path(case.config["accounting"]["path"]),
        store_id=case.config["accounting"]["store_id"],
        objective_id=case.config["objective"]["id"],
    )


def test_each_waiting_producer_poll_consumes_fresh_same_objective_wall_authority(native_case: NativeCase) -> None:
    case = native_case
    advance_to_producer(case)
    case.item.dispatch.source.run["status"] = "in_progress"
    before = accounting_state(case)["reserved"]
    for _ in range(3):
        result = case.step()
        assert result["event"]["status"] == "waiting"
        case.current += dt.timedelta(seconds=30)
    after = accounting_state(case)["reserved"]
    assert after["attempts"] == before["attempts"] == 3
    assert after["wall_time_ms"] == before["wall_time_ms"] + 90000
    assert case.item.state_path.read_bytes() == case.state_before
    assert "complete" not in result["event"]["correlations"]
    # A distinct approved fixture reservation consumes all remaining read time; IDs cannot reset it.
    remaining = case.config["limits"]["wall_time_ms"] - after["wall_time_ms"]
    proposal = {
        "schema_version": "0.1.0",
        "id": planning.digest("consume-remaining-objective-read-time"),
        "delegation_id": "explicit-controlled-reservation",
        "policy_digest": planning.digest(case.config),
        "effect": {"operation": "independently-approved-time"},
        "preconditions": {},
        "authorization": {"controlled": True},
        "cost": units(0, remaining),
    }
    assert standing_authorization.reserve(
        Path(case.config["accounting"]["path"]),
        store_id=case.config["accounting"]["store_id"],
        objective_id=case.config["objective"]["id"],
        proposal=proposal,
        approved=True,
        reason="Explicit independent controlled capacity reservation.",
    )["approved"]
    calls = len(case.item.dispatch.source.calls)
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert len(case.item.dispatch.source.calls) == calls
    assert case.item.state_path.read_bytes() == case.state_before


def test_retained_effect_reconciliation_spends_new_wall_capacity_without_another_native_attempt(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exacly import handoff

    case = native_case
    case.step()
    original = handoff.apply

    def interrupted(*args: Any, **kwargs: Any) -> Doc:
        original(*args, **kwargs)
        raise OSError("controlled lost response")

    with monkeypatch.context() as patch:
        patch.setattr(handoff, "apply", interrupted)
        assert case.step()["event"]["status"] == "recovering"
    before = accounting_state(case)["reserved"]
    case.current += dt.timedelta(seconds=30)
    assert case.step()["event"]["phase"] == "publish"
    after = accounting_state(case)["reserved"]
    assert after["attempts"] == before["attempts"] == 2
    assert after["wall_time_ms"] > before["wall_time_ms"]


def test_another_controller_alias_cannot_supply_native_absence_proof(native_case: NativeCase) -> None:
    case = native_case
    result = case.step()
    request = case.workflow.proposal("handoff", result["event"]["correlations"])
    aliased = {**request, "controller": {**request["controller"], "journal_id": "00112233445546778899aabbccddeeff"}}
    with pytest.raises(ValueError, match="another initialized controller"):
        case.workflow.effect_registry()["handoff"].recover(aliased, request["authorization"])
    assert case.item.state_path.read_bytes() == case.state_before


def test_self_hashed_component_changes_cannot_replace_independently_approved_configuration(
    native_case: NativeCase,
) -> None:
    case = native_case
    components = copy.deepcopy(case.workflow.components)
    components["remote"]["operation_marker_ref"] = "refs/tags/req039-operations/unapproved-marker"
    components["remote"]["operation_key"] = str(case.journal.parent / "not-an-approved-key")
    components["remote"]["operation_key_id"] = planning.digest("not-the-approved-key")
    components.pop("configuration_digest")
    components["configuration_digest"] = planning.digest(components)
    contract = copy.deepcopy(case.workflow.contract)
    contract["stores"]["configuration"]["id"] = components["configuration_digest"]
    alternative = controller.NativeWorkflow(
        contract=contract,
        documents=case.workflow.documents,
        plan=case.workflow.plan,
        readiness_policy=case.workflow.readiness,
        trust_policy=case.workflow.trust,
        components=components,
        services=case.workflow.services,
    )
    authorizer = automation_authorization.Authorizer(
        Path(case.config["config_path"]),
        approved_digest=planning.digest(case.config),
        issuer=case.config["issuer"],
        approval_id=case.config["approval_id"],
        effects=alternative.effect_registry(),
        readers=alternative.reader_registry(),
        clock=lambda: CURRENT,
    )
    correlations = controller.show(case.journal, case.journal_id)["event"]["correlations"]
    request = alternative.proposal("execute", correlations)
    result = authorizer.execute("execute", request, effect_id=planning.digest("unapproved-component-replacement"))
    assert result["status"] in {"denied", "paused"}
    assert "configuration" in result["reason"].lower()
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


@pytest.mark.parametrize("unsupported", ["composition", "targets", "distributed", "extra-member"])
def test_closed_single_producer_profile_rejects_unsupported_requirements_before_effects(
    native_case: NativeCase,
    unsupported: str,
) -> None:
    case = native_case
    components = copy.deepcopy(case.workflow.components)
    if unsupported == "extra-member":
        components["required_members"] = ["producer", "another-required-member"]
    elif unsupported == "distributed":
        components["profile"] = "distributed-workflow-v1"
    else:
        components[unsupported] = {"required": ["unimplemented"]}
    components.pop("configuration_digest")
    components["configuration_digest"] = planning.digest(components)
    contract = copy.deepcopy(case.workflow.contract)
    contract["stores"]["configuration"]["id"] = components["configuration_digest"]
    with pytest.raises(ValueError, match=r"unsupported|exactly one"):
        controller.NativeWorkflow(
            contract=contract,
            documents=case.workflow.documents,
            plan=case.workflow.plan,
            readiness_policy=case.workflow.readiness,
            trust_policy=case.workflow.trust,
            components=components,
            services=case.workflow.services,
        )
    assert case.item.state_path.read_bytes() == case.state_before
    assert case.transport.sends == 0


@pytest.mark.parametrize("native_case", [True], indirect=True)
def test_native_current_satisfaction_supersedes_fresh_work_before_dispatch_or_execution(
    native_case: NativeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = native_case
    workflow, source = case.workflow, case.item.dispatch.source
    snapshot = state_writer.read(case.item.state_path)
    target = workflow.contract["input_target"]
    observed = refresh(
        {**workflow.documents, "__current_state__": snapshot.document},
        {source.case: "passed"},
        Run(
            revision=target["revision"],
            model_revision=target["model_revision"],
            context=target["context"],
            observed_at=CURRENT,
            workflow=source.workflow_path,
            job=source.job_id,
            job_status="success",
            url="https://example.invalid/controlled-native-observation/1",
        ),
        previous=snapshot.document,
    )
    state_writer.publish(snapshot, observed)
    unchanged = case.item.state_path.read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("satisfied native work must not invoke a new dispatch or execution")

    monkeypatch.setattr(dispatch, "apply", forbidden)
    monkeypatch.setattr(executor, "apply", forbidden)
    result = case.step()
    assert result["event"]["status"] == "superseded", result
    assert result["event"]["assessment"]["classification"] == "satisfied"
    assert result["event"]["pending"] is None
    assert case.item.dispatch.client.calls == []
    assert accounting_state(case)["decisions"] == []
    assert case.transport.sends == 0
    assert case.item.state_path.read_bytes() == unchanged
    assert case.step()["head"] == result["head"]


@pytest.mark.parametrize("native_case", [True], indirect=True)
def test_native_planner_selects_evidence_then_escalates_new_failure_for_review(native_case: NativeCase) -> None:
    case = native_case
    assessment = case.workflow.assess({})
    assert assessment["status"] == "eligible" and assessment["classification"] == "collect_evidence"
    assert "missing evidence does not establish missing implementation" in assessment["reason"].lower()
    source = case.item.dispatch.source
    snapshot = state_writer.read(case.item.state_path)
    target = case.workflow.contract["input_target"]
    failed = refresh(
        {**case.workflow.documents, "__current_state__": snapshot.document},
        {source.case: "failed"},
        Run(
            revision=target["revision"],
            model_revision=target["model_revision"],
            context=target["context"],
            observed_at=CURRENT,
            workflow=source.workflow_path,
            job=source.job_id,
            job_status="failure",
            url="https://example.invalid/controlled-native-observation/2",
        ),
        previous=snapshot.document,
    )
    state_writer.publish(snapshot, failed)
    result = case.step()
    assert result["event"]["status"] == "paused"
    assert result["event"]["assessment"]["classification"] == "investigate_failure"
    assert "review a new plan and recipe" in result["event"]["reason"]
    assert result["event"]["pending"] is None
    assert case.item.dispatch.client.calls == []
    assert accounting_state(case)["decisions"] == []


@pytest.mark.parametrize("variant", controller.NEGATIVE_CASES)
def test_declared_negative_reader_uses_fresh_actual_admission_without_state_or_effects(
    native_case: NativeCase,
    variant: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = native_case
    advance_to_producer(case)
    case.step()
    case.current += dt.timedelta(seconds=30)
    event = case.step()["event"]
    assert event["phase"] == "associate"
    before = accounting_state(case)["reserved"]["wall_time_ms"]
    state = case.item.state_path.read_bytes()
    calls = len(case.item.dispatch.source.calls)
    if variant in {"unavailable-producer", "wrong-attempt"}:
        from exacly.github_evidence import GitHubError

        source = case.item.dispatch.source
        original = source.get_json
        missing = f"/repos/{source.policy['repository']['full_name']}/actions/runs/" + (
            "1/attempts/1" if variant == "unavailable-producer" else f"{RUN_ID + 1}/attempts/2"
        )

        def explicit_controlled_http404(path: str) -> Doc:
            if path == missing:
                source.calls.append(path)
                raise GitHubError("controlled expected HTTP404 for the explicit unavailable run or attempt")
            return original(path)

        monkeypatch.setattr(source, "get_json", explicit_controlled_http404)
    request = case.workflow.negative_request(event["correlations"], case=variant)
    result = case.authority.call_metered("negative-admission", request, operation_id=planning.digest(variant))
    assert result["status"] == "completed", result
    observed = result["receipt"]["result"]
    assert observed["negative_established"] is True, observed
    assert observed["pending_unchanged"] is True
    assert observed["control_admission"]["status"] == "admitted"
    assert observed["admission"]["status"] == "rejected"
    assert len(case.item.dispatch.source.calls) > calls
    assert accounting_state(case)["reserved"]["wall_time_ms"] == before + 45000
    assert case.item.state_path.read_bytes() == state
    assert case.transport.sends == 1
    assert "associate" not in controller.show(case.journal, case.journal_id)["event"]["correlations"]
    assert all(value is False for value in observed["claims"].values())


def test_negative_reader_rejects_unapproved_case_and_changed_contract_before_source_read(
    native_case: NativeCase,
) -> None:
    case = native_case
    with pytest.raises(ValueError, match="explicit approval"):
        case.workflow.negative_request({}, case="mutate-remote")
    request = case.workflow.negative_request({}, case="wrong-attempt")
    request["contract"]["wrong_attempt"] = 3
    calls = len(case.item.dispatch.source.calls)
    result = case.authority.call_metered("negative-admission", request, operation_id=planning.digest("changed-probe"))
    assert result["status"] in {"denied", "paused"}
    assert len(case.item.dispatch.source.calls) == calls


@pytest.mark.parametrize("native_case", ["preflight"], indirect=True)
def test_closed_preflight_pins_runtime_and_native_producer_cap_survives_repeated_ids(native_case: NativeCase) -> None:
    case = native_case
    selected = case.workflow.components["authority_preflight"]
    assert selected["producer_policy_digest"] == planning.digest(case.workflow.trust)
    advance_to_producer(case)
    event = controller.show(case.journal, case.journal_id)["event"]
    request = case.workflow.producer_request(event["correlations"])
    operation = planning.digest("same-caller-id-cannot-replenish-producer-read-authority")
    for _ in range(20):
        result = case.authority.call_metered("producer-observe", request, operation_id=operation)
        assert result["status"] == "completed", result
    calls = len(case.item.dispatch.source.calls)
    result = case.authority.call_metered("producer-observe", request, operation_id=operation)
    assert result["status"] != "completed"
    assert "producer-read limit" in result["reason"]
    assert len(case.item.dispatch.source.calls) == calls
    history = accounting_state(case)
    slots = [
        item
        for item in history["decisions"]
        if item["request"]["proposal"]["effect"].get("meter", {}).get("purpose") == "producer-observe"
    ]
    assert len(slots) == 21  # The denied post-reservation slot stays charged; it made no API call.
    again = case.authority.call_metered("producer-observe", request, operation_id=operation)
    assert again["status"] != "completed"
    assert len(accounting_state(case)["decisions"]) == len(history["decisions"])


@pytest.mark.parametrize("native_case", ["preflight"], indirect=True)
def test_preflight_contract_rejects_unknown_or_changed_runtime_scope(native_case: NativeCase) -> None:
    case = native_case
    components = copy.deepcopy(case.workflow.components)
    components["authority_preflight"]["provider"] = "unapproved-loader"
    with pytest.raises(ValueError, match="closed finite"):
        controller.validate_components(components)
    case.workflow.components["authority_preflight"]["coordinator"]["run_id"] = "different-run"
    case.workflow.components.pop("configuration_digest")
    case.workflow.components["configuration_digest"] = planning.digest(case.workflow.components)
    case.workflow.contract["stores"]["configuration"]["id"] = case.workflow.components["configuration_digest"]
    with pytest.raises(ValueError, match="runtime preflight changes"):
        case.workflow._check_config()
