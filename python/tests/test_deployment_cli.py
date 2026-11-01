"""Actual installed CLI, owning publication consumer, static HTTP and independent observations."""

from __future__ import annotations

import copy
import ctypes
import datetime as dt
import json
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import delivery_automation as delivery
from exacly import delivery_store
from exacly import deployment_automation as deploy
from exacly import execution_store as store
from exacly import owning_publisher as publisher
from release_publication_fixtures import retained
from test_deployment_automation import (
    Environment,
    _published_artifact,
    _published_operation,
    _select_publication,
    identity,
)

Doc = dict[str, Any]
LIMITS = {"operations": 4, "wall_time_ms": 600000, "reads": 256, "rollbacks": 1}
CONTROLLER_IMPORTS = """
import copy, datetime as dt, json, sys
from exacly import cli, delivery_io, delivery_engine, deployment_automation, operational_observation
from exacly import execution_store
"""
CONTROLLER_PIN_BOOTSTRAP = (
    CONTROLLER_IMPORTS
    + """
packet = json.load(sys.stdin)
configuration = deployment_automation.validate_configuration(packet['configuration'])
policy = copy.deepcopy(packet['policy'])
policy['observer_configuration_digest'] = operational_observation.configuration_digest(configuration['observer'])
policy = deployment_automation.validate_policy(policy)
adapter = delivery_io.deployment_adapter(configuration, policy)
print(json.dumps(dict(
    observer_configuration_digest=policy['observer_configuration_digest'],
    operation_profile_digest=adapter.operation_profile_digest,
    configuration_digest=adapter.configuration_digest,
)))
"""
)
CONTROLLER_OBSERVE_BOOTSTRAP = (
    CONTROLLER_IMPORTS
    + """
packet = json.load(sys.stdin)
configuration = operational_observation.validate_configuration(packet['configuration'])
report = operational_observation.observe(packet['expected'], configuration, deadline=execution_store.Deadline(30))
operational_observation.verify(
    report, packet['expected'], configuration, now=dt.datetime.now(dt.UTC), deadline=execution_store.Deadline(30)
)
print(json.dumps(report))
"""
)
OFFLINE_BOOTSTRAP = """
import sys
from exacly import delivery_engine, delivery_io, deployment_automation
from exacly.cli import exacly_main
def forbidden(*args, **kwargs):
    raise AssertionError('historical inspection attempted a current clock, adapter or proof read')
delivery_engine._now = forbidden
delivery_io.deployment_adapter = forbidden
deployment_automation.LocalStaticDeploymentAdapter.validate_provenance = forbidden
sys.exit(exacly_main(sys.argv[1:]))
"""
BUDGET_BOOTSTRAP = """
import json, os, sys
from pathlib import Path
from exacly import delivery_io, delivery_store, local_static_deployment as owner
from exacly.cli import exacly_main
arguments = sys.argv[1:]
journal = Path(arguments[arguments.index('--journal') + 1])
journal_id = arguments[arguments.index('--journal-id') + 1]
request = delivery_io.read_document(Path(arguments[arguments.index('--request') + 1]))
original = owner._replace
def audit(path, value):
    history = delivery_store.show(journal, journal_id, request['operation_key'])
    operation = history['operation']
    assert request['stage'] == 'rollback'
    assert operation['intent']['request'] == request
    assert operation['intent']['cost']['rollbacks'] == 1
    assert history['limits']['rollbacks'] == history['reserved']['rollbacks'] == 1
    assert operation['phase'] == 'may_have_started'
    assert path.name == 'active.json'
    assert owner._document(path)['generation'] == request['subject']['expected_generation']
    result = dict(intent=operation['intent']['id'], original_budget=1, reserved_before_cas=1)
    descriptor = os.open(journal.with_name('rollback-budget-audit.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(result, output)
        output.flush()
        os.fsync(output.fileno())
    return original(path, value)
owner._replace = audit
sys.exit(exacly_main(arguments))
"""


def run_cli(
    arguments: list[str], *, offline: bool = False, bootstrap: str | None = None
) -> tuple[int, Doc | None, str]:
    # The real HTTP service lives in the test parent. The installed CLI gets its own single
    # coordinator thread, preserving the production native watchdog's process/thread boundary.
    selected_bootstrap = OFFLINE_BOOTSTRAP if offline else bootstrap
    invocation = (
        [sys.executable, "-I", "-c", selected_bootstrap]
        if selected_bootstrap is not None
        else [sys.executable, "-I", "-m", "exacly"]
    )
    completed = subprocess.run(
        [*invocation, "delivery", *arguments], capture_output=True, text=True, timeout=180, check=False
    )
    report = json.loads(completed.stdout) if completed.stdout else None
    return completed.returncode, report, completed.stderr


def controller_readonly(bootstrap: str, packet: Doc) -> Doc:
    """Run a fixed trusted test bootstrap in the actual fresh installed CLI controller role.

    The two scripts are test constants, never selectors from fixture JSON. They return actual
    fingerprints or real signed loopback measurements without invoking a delivery effect.
    """
    completed = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap],
        input=store.canonical(packet),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.stat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


@dataclass
class Operation:
    directory: Path
    journal: Path
    journal_id: str
    request: Doc
    payload: Doc
    policy: Doc
    approval: Doc

    def approval_args(self) -> list[str]:
        return [
            "--approval", str(self.directory / "approval.json"),
            "--approved-approval-digest", delivery.digest(self.approval),
            "--issuer", self.approval["issuer"], "--approval-id", self.approval["approval_id"],
        ]  # fmt: skip

    def arguments(self, *, approved: bool = True, apply: bool = True) -> list[str]:
        result = [
            self.request["stage"], "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--request", str(self.directory / "request.json"), "--payload", str(self.directory / "payload.json"),
            "--config", str(self.directory / "config.json"), "--policy", str(self.directory / "policy.json"), "--json",
        ]  # fmt: skip
        if approved:
            result.extend(self.approval_args())
        if apply:
            result.append("--apply")
        return result

    def reconcile_args(self) -> list[str]:
        return [
            "reconcile", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--operation-key", self.request["operation_key"],
            "--config", str(self.directory / "config.json"), "--policy", str(self.directory / "policy.json"),
            *self.approval_args(), "--json",
        ]  # fmt: skip

    def history(self) -> Doc:
        return delivery_store.show(self.journal, self.journal_id, self.request["operation_key"])


@dataclass
class Vertical:
    environment: Environment
    publication: Doc
    journal: Path
    journal_id: str
    operator: Path

    def operation(self, published: Doc | None = None, **kwargs: Any) -> Operation:
        selected = published or self.publication
        reference, request, payload, _intent, approval = _published_operation(self.environment, selected, **kwargs)
        assert reference._reader is deploy._published_bytes
        assert reference.policy["contract"] != reference.policy["publication_verification_contract"]
        configuration, policy = copy.deepcopy(self.environment.config), copy.deepcopy(reference.policy)
        # Pytest may already have loaded isolation-only native libraries. Its process identity is
        # not the fresh installed CLI's role. Select actual pins in that separate role, then make
        # the fixture's independent approval for these exact inputs; no product helper is weakened.
        pins = controller_readonly(CONTROLLER_PIN_BOOTSTRAP, {"configuration": configuration, "policy": policy})
        policy["observer_configuration_digest"] = pins["observer_configuration_digest"]
        payload["operation_profile_digest"] = pins["operation_profile_digest"]
        request["native_payload_digest"] = delivery.digest(payload)
        approval.update(
            journal={"path": str(self.journal), "id": self.journal_id},
            limits=copy.deepcopy(LIMITS),
            policy_digest=delivery.digest(policy),
            request_digest=delivery.digest(request),
            subject_digest=delivery.digest(request["subject"]),
            resource_key=delivery.resource_key(request),
            adapter={"id": deploy.PROFILE, "configuration_digest": pins["configuration_digest"]},
        )
        directory = self.operator / request["operation_key"].removeprefix("sha256:")
        directory.mkdir(mode=0o700)
        for name, document in {
            "request": request,
            "payload": payload,
            "policy": policy,
            "approval": approval,
            "config": configuration,
        }.items():
            retained(directory / (name + ".json"), document)
        return Operation(directory, self.journal, self.journal_id, request, payload, policy, approval)


@pytest.fixture
def vertical(tmp_path: Path) -> Iterator[Vertical]:
    selected = tmp_path / "environment"
    selected.mkdir(mode=0o700)
    environment = Environment(selected)
    environment.config["observer"]["max_age_ms"] = 180000
    try:
        published = _published_artifact(
            tmp_path / "publication", b"real approved published static bytes\x00\xff", version="1.2.3"
        )
        _select_publication(environment, published)
        operator = selected / "operator"
        operator.mkdir(mode=0o700)
        journal = operator / "delivery.sqlite3"
        metadata = delivery_store.initialize(
            journal,
            objective={"id": identity("objective"), "original_goal": identity("goal")},
            limits=copy.deepcopy(LIMITS),
            native_limits={"wall_time_ms": 60000, "read_calls": 12, "write_calls": 1},
        )
        yield Vertical(environment, published, journal, metadata["id"], operator)
    finally:
        environment.close()


def require_report(result: tuple[int, Doc | None, str], status: int) -> Doc:
    code, report, error = result
    assert code == status, (code, report, error)
    assert report is not None, error
    return report


@pytest.mark.parametrize("approved", [False, True])
def test_deployment_defaults_to_local_preview_without_sampling_or_effects(
    vertical: Vertical, monkeypatch: pytest.MonkeyPatch, approved: bool
) -> None:
    operation = vertical.operation()
    environment = vertical.environment
    sampled: list[str] = []
    original = environment.owner.served

    def measured(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        sampled.append("request")
        return original(deadline=deadline)

    monkeypatch.setattr(environment.owner, "served", measured)
    before_owner, before_journal = snapshot(environment.root), vertical.journal.read_bytes()
    report = require_report(run_cli(operation.arguments(approved=approved, apply=False)), 0 if approved else 1)
    assert report["status"] == ("preview" if approved else "approval_required")
    assert report["packet"]["adapter"]["id"] == deploy.PROFILE
    assert report["live_checked"] is report["effect_authorized"] is False
    assert report["operational_status"]["status"] == "unknown"
    assert report["operational_status"]["freshness"] == "not_sampled"
    assert sampled == [] and snapshot(environment.root) == before_owner
    assert vertical.journal.read_bytes() == before_journal


def test_real_published_artifact_deployment_and_signed_current_static_readiness(vertical: Vertical) -> None:
    # Reproduce the actual full-suite ambient state left by the earlier isolation tests. The
    # controller bootstrap and installed CLI stay fresh; this library is never forced into product.
    ctypes.CDLL("libseccomp.so.2")
    operation = vertical.operation()
    report = require_report(run_cli(operation.arguments()), 0)
    assert report["status"] == "applied" and report["historical"] is False
    assert report["receipt"]["stage"] == "deploy"
    assert report["receipt"]["effect"]["artifact"] == operation.request["subject"]["artifact"]
    health = report["operational_status"]
    assert health["status"] == "healthy" and health["freshness"] == "current"
    assert health["current_readiness_established"] is True
    assert health["generic_application_health"] is health["deployment_authority"] is False
    assert not any(report["receipt"]["claims"].values())
    native = operation.history()["operation"]
    facts = native["calls"][-1]["result"]["provenance"]["facts"]
    assert health == facts["operational_status"]
    assert health["evaluated_at"] == facts["operational_status"]["evaluated_at"]
    assert health["scope"] == "as-of-recorded-environment-observation"
    assert health["generation"] == {
        "expected": report["receipt"]["effect"]["generation"],
        "observed": facts["current_activation"]["active_generation"],
        "observed_at": facts["current_activation"]["observed_at"],
    }
    assert health["generation"]["expected"] == health["generation"]["observed"]
    observation = facts["observation"]
    assert observation["expected"]["artifact"] == operation.request["subject"]["artifact"]
    assert observation["expected"]["generation"] == report["receipt"]["effect"]["generation"]
    assert observation["expected"]["rollout_id"] == report["receipt"]["effect"]["rollout_id"]
    assert facts["evaluation"]["scope"] == "historical-static-measurements"
    assert facts["evaluation"]["current_readiness_established"] is False
    assert len(observation["samples"]) == vertical.environment.config["observer"]["samples"]
    assert all(sample["complete"] for sample in observation["samples"])
    record, content = vertical.environment.owner.served(deadline=store.Deadline(10))
    assert content == vertical.publication["artifact_bytes"]
    assert record["intent_id"] == report["intent"]["id"]
    assert len(list(vertical.environment.root.glob("*.intent.json"))) == 1
    assert stat.S_IMODE(vertical.journal.stat().st_mode) == 0o600


def test_unhealthy_actual_http_keeps_applied_receipt_and_cannot_automatically_rollback(
    vertical: Vertical, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = vertical.operation()
    original = vertical.environment.owner.served

    def unhealthy(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, _content = original(deadline=deadline)
        return record, b"actual HTTP response differs from the published artifact"

    monkeypatch.setattr(vertical.environment.owner, "served", unhealthy)
    report = require_report(run_cli(operation.arguments()), 1)
    assert report["status"] == report["receipt"]["outcome"] == "applied"
    assert report["operational_status"]["status"] == "unhealthy"
    assert report["operational_status"]["current_readiness_established"] is False
    history = operation.history()
    assert history["limits"]["rollbacks"] == 1 and history["reserved"]["rollbacks"] == 0
    assert len(list(vertical.environment.root.glob("*.intent.json"))) == 1
    assert history["operation"]["intent"]["request"]["stage"] == "deploy"
    record, content = original(deadline=store.Deadline(10))
    assert record["intent_id"] == report["intent"]["id"] and content == vertical.publication["artifact_bytes"]


def test_unavailable_http_keeps_definite_deployment_applied_with_unknown_readiness(vertical: Vertical) -> None:
    operation = vertical.operation()
    vertical.environment.close()
    report = require_report(run_cli(operation.arguments()), 1)
    assert report["status"] == report["receipt"]["outcome"] == "applied"
    assert report["operational_status"]["status"] == "unknown"
    assert report["operational_status"]["current_readiness_established"] is False
    assert operation.history()["reserved"]["rollbacks"] == 0
    assert len(list(vertical.environment.root.glob("*.intent.json"))) == 1


def test_actual_health_expiry_never_relabels_applied_deployment_as_uncertain(vertical: Vertical) -> None:
    vertical.environment.config["observer"]["max_age_ms"] = 1
    operation = vertical.operation()
    report = require_report(run_cli(operation.arguments()), 1)
    assert report["status"] == report["receipt"]["outcome"] == "applied"
    assert report["operational_status"]["status"] == "unknown"
    assert report["operational_status"]["freshness"] == "stale"
    assert report["operational_status"]["historical_status"] == "healthy"
    assert report["operational_status"]["current_readiness_established"] is False
    assert (
        vertical.environment.owner.snapshot(deadline=store.Deadline(10))["rollout"]["intent_id"]
        == report["intent"]["id"]
    )


@pytest.mark.parametrize("damage", ["environment", "artifact", "pin", "expired"])
def test_wrong_or_expired_deployment_scope_cannot_change_environment_or_reserve_delivery(
    vertical: Vertical, damage: str
) -> None:
    operation = vertical.operation()
    arguments = operation.arguments()
    if damage in {"environment", "artifact"}:
        request = copy.deepcopy(operation.request)
        if damage == "environment":
            request["subject"]["environment"] = "unapproved-environment"
        else:
            request["subject"]["artifact"]["digest"] = delivery.digest("unapproved artifact")
        retained(operation.directory / "request.json", request)
    elif damage == "pin":
        arguments[arguments.index("--approved-approval-digest") + 1] = delivery.digest("different approval")
    else:
        approval = copy.deepcopy(operation.approval)
        approval["expires_at"] = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
        retained(operation.directory / "approval.json", approval)
        arguments[arguments.index("--approved-approval-digest") + 1] = delivery.digest(approval)
    before_owner, before_journal = snapshot(vertical.environment.root), vertical.journal.read_bytes()
    registry = Path(vertical.publication["owner"]["registry"]["path"])
    before_registry = snapshot(registry)
    code, report, error = run_cli(arguments)
    assert code == 2 and report is None, (code, report, error)
    assert snapshot(vertical.environment.root) == before_owner and vertical.journal.read_bytes() == before_journal
    assert snapshot(registry) == before_registry


def test_default_publication_consumer_checks_actual_bytes_before_any_deployment_effect(vertical: Vertical) -> None:
    operation = vertical.operation()
    subject = vertical.publication["request"]["subject"]
    published = publisher.version_path(
        vertical.publication["owner"]["registry"], subject["destination"]["package"], subject["version"]
    )
    retained(published / subject["artifacts"][0]["name"], b"corrupted retained publication")
    before = snapshot(vertical.environment.root)
    report = require_report(run_cli(operation.arguments()), 1)
    assert report["status"] == "prepared" and report["receipt"] is None
    assert report["operational_status"]["current_readiness_established"] is False
    assert snapshot(vertical.environment.root) == before


def test_historical_deployment_inspection_is_offline_before_clock_factory_keys_and_provenance(
    vertical: Vertical,
) -> None:
    operation = vertical.operation()
    applied = require_report(run_cli(operation.arguments()), 0)
    later = vertical.operation()
    revoked = require_report(run_cli([
        "revoke", "--journal", str(vertical.journal), "--journal-id", vertical.journal_id,
        "--reason", "Withdraw new environment effects; retain historical inspection", "--json",
    ]), 0)  # fmt: skip
    assert revoked["revoked"] is True
    before_rejected = snapshot(vertical.environment.root)
    code, report, error = run_cli(later.arguments())
    assert code == 2 and report is None and "revoked" in error.lower()
    assert snapshot(vertical.environment.root) == before_rejected
    vertical.environment.close()
    before_owner, before_journal = snapshot(vertical.environment.root), vertical.journal.read_bytes()
    for path in (
        operation.directory / "config.json",
        operation.directory / "policy.json",
        Path(vertical.environment.config["owner_key"]["path"]),
        Path(vertical.environment.config["publisher_key"]["path"]),
        Path(vertical.environment.config["observer"]["key"]["path"]),
    ):
        path.unlink()
    for arguments in (operation.arguments(), operation.reconcile_args()):
        historical = require_report(run_cli(arguments, offline=True), 0)
        assert historical["historical"] is True and historical["live_checked"] is False
        assert historical["receipt"] == applied["receipt"]
        assert historical["operational_status"]["status"] == "unknown"
        assert historical["operational_status"]["freshness"] == "historical"
        assert historical["operational_status"]["current_readiness_established"] is False
    assert snapshot(vertical.environment.root) == before_owner
    assert vertical.journal.read_bytes() == before_journal


def test_real_published_artifact_rollback_requires_separate_pin_and_consumes_original_budget_once(
    vertical: Vertical, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior_operation = vertical.operation()
    prior_report = require_report(run_cli(prior_operation.arguments()), 0)
    prior_history = prior_operation.history()
    assert prior_history["limits"] == LIMITS
    assert prior_history["reserved"] == {"operations": 1, "wall_time_ms": 120000, "reads": 24, "rollbacks": 0}
    prior = prior_history["operation"]["calls"][-1]["result"]["provenance"]["facts"]["rollout"]
    current_publication = _published_artifact(
        tmp_path / "next-publication",
        b"next independently published static version",
        version="1.2.4",
        previous=vertical.publication,
    )
    current_operation = vertical.operation(current_publication)
    original = vertical.environment.owner.served

    def unhealthy(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, _content = original(deadline=deadline)
        return record, b"actual failing current rollout HTTP bytes"

    with monkeypatch.context() as failed_service:
        failed_service.setattr(vertical.environment.owner, "served", unhealthy)
        current_report = require_report(run_cli(current_operation.arguments()), 1)
    assert current_report["status"] == "applied" and current_report["operational_status"]["status"] == "unhealthy"
    current_history = current_operation.history()
    assert current_operation.journal == prior_operation.journal == vertical.journal
    assert current_operation.journal_id == prior_operation.journal_id == vertical.journal_id
    assert current_history["objective"] == prior_history["objective"] == prior_operation.request["objective"]
    assert current_history["limits"] == LIMITS
    assert current_history["reserved"] == {"operations": 2, "wall_time_ms": 240000, "reads": 48, "rollbacks": 0}
    current_facts = current_history["operation"]["calls"][-1]["result"]["provenance"]["facts"]
    current, failure = current_facts["rollout"], current_facts["observation"]
    conditions = {
        "current_rollout_id": current["rollout_id"],
        "prior_generation": prior["generation"],
        "failure_observation_digest": delivery.digest(failure),
        "maximum_rollbacks": 1,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=3)).isoformat(),
    }
    rollback = vertical.operation(stage="rollback", rollback=conditions, failure=failure)
    assert rollback.approval["stage"] == "rollback" and rollback.approval["limits"]["rollbacks"] == 1
    inherited = rollback.arguments()
    previous = current_operation.approval_args()
    for option in ("--approval", "--approved-approval-digest", "--issuer", "--approval-id"):
        inherited[inherited.index(option) + 1] = previous[previous.index(option) + 1]
    owner_before = snapshot(vertical.environment.root)
    assert run_cli(inherited)[0] == 2
    assert snapshot(vertical.environment.root) == owner_before
    registry = Path(vertical.publication["owner"]["registry"]["path"])
    registry_before = snapshot(registry)
    result = require_report(run_cli(rollback.arguments(), bootstrap=BUDGET_BOOTSTRAP), 0)
    assert result["status"] == "applied" and result["receipt"]["stage"] == "rollback"
    assert result["operational_status"]["status"] == "healthy"
    assert result["receipt"]["effect"]["artifact"] == prior_report["receipt"]["effect"]["artifact"]
    assert result["receipt"]["effect"]["generation"] not in (prior["generation"], current["generation"])
    assert vertical.environment.owner.served(deadline=store.Deadline(10))[1] == vertical.publication["artifact_bytes"]
    history = rollback.history()
    assert rollback.journal == vertical.journal and rollback.journal_id == vertical.journal_id
    assert history["objective"] == prior_history["objective"] == rollback.request["objective"]
    assert history["limits"] == LIMITS
    assert history["reserved"] == {"operations": 3, "wall_time_ms": 360000, "reads": 72, "rollbacks": 1}
    assert history["limits"]["rollbacks"] == history["reserved"]["rollbacks"] == 1
    assert history["operation"]["intent"]["cost"]["rollbacks"] == 1
    audit = vertical.journal.with_name("rollback-budget-audit.json")
    assert json.loads(audit.read_text()) == {
        "intent": result["intent"]["id"],
        "original_budget": 1,
        "reserved_before_cas": 1,
    }
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    assert len(list(vertical.environment.root.glob("*.intent.json"))) == 3
    assert snapshot(registry) == registry_before
    saved = vertical.journal.read_bytes()
    replay = require_report(run_cli(rollback.arguments(), offline=True), 0)
    assert replay["historical"] is True and replay["receipt"] == result["receipt"]
    assert vertical.journal.read_bytes() == saved

    # Fresh separate approval cannot enlarge the original objective's rollback allowance.
    rolled_back = history["operation"]["calls"][-1]["result"]["provenance"]["facts"]["rollout"]
    with monkeypatch.context() as failed_service:
        failed_service.setattr(vertical.environment.owner, "served", unhealthy)
        second_failure = controller_readonly(
            CONTROLLER_OBSERVE_BOOTSTRAP,
            {
                "expected": deploy.expected_observation(rolled_back),
                "configuration": vertical.environment.config["observer"],
            },
        )
    second_conditions = {
        "current_rollout_id": rolled_back["rollout_id"],
        "prior_generation": current["generation"],
        "failure_observation_digest": delivery.digest(second_failure),
        "maximum_rollbacks": 1,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=3)).isoformat(),
    }
    second = vertical.operation(
        current_publication, stage="rollback", rollback=second_conditions, failure=second_failure
    )
    before_second, journal_before_second = snapshot(vertical.environment.root), vertical.journal.read_bytes()
    packet = require_report(run_cli(second.arguments(apply=False)), 0)["packet"]
    next_cost = packet["intent"]["cost"]
    for dimension in ("operations", "wall_time_ms", "reads"):
        assert history["reserved"][dimension] + next_cost[dimension] <= LIMITS[dimension]
    assert history["reserved"]["rollbacks"] + next_cost["rollbacks"] > LIMITS["rollbacks"]
    code, failed, reason = run_cli(second.arguments())
    assert code == 2 and failed is None and "budget" in reason.lower(), (code, failed, reason)
    assert vertical.journal.read_bytes() == journal_before_second
    assert snapshot(vertical.environment.root) == before_second
    assert rollback.history()["reserved"]["rollbacks"] == 1


def test_lost_reply_after_actual_activation_reconciles_current_health_without_another_cas(vertical: Vertical) -> None:
    operation = vertical.operation()
    lose_reply = """
import sys
from exacly import local_static_deployment as owner
from exacly.cli import exacly_main
original = owner._replace
def lost_reply(path, value):
    original(path, value)
    raise OSError('controlled lost acknowledgement after durable activation')
owner._replace = lost_reply
sys.exit(exacly_main(sys.argv[1:]))
"""
    uncertain = require_report(run_cli(operation.arguments(), bootstrap=lose_reply), 1)
    assert uncertain["status"] == uncertain["receipt"]["outcome"] == "uncertain"
    active = vertical.environment.owner.snapshot(deadline=store.Deadline(10))["rollout"]
    assert active["intent_id"] == uncertain["intent"]["id"]
    owner_before = snapshot(vertical.environment.root)
    metadata_before = {
        path.name: (path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in vertical.environment.root.iterdir()
    }
    discovery_only = """
import sys
from exacly import local_static_deployment as owner
from exacly.cli import exacly_main
def forbidden(*args, **kwargs):
    raise AssertionError('reconciliation attempted another active-generation CAS')
owner._replace = forbidden
sys.exit(exacly_main(sys.argv[1:]))
"""
    discovered = require_report(run_cli(operation.reconcile_args(), bootstrap=discovery_only), 0)
    assert discovered["status"] == discovered["receipt"]["outcome"] == "applied"
    assert discovered["historical"] is False and discovered["live_checked"] is True
    assert discovered["intent"] == uncertain["intent"]
    assert discovered["receipt"]["intent_id"] == uncertain["intent"]["id"]
    assert discovered["operational_status"]["status"] == "healthy"
    assert discovered["operational_status"]["freshness"] == "current"
    assert discovered["operational_status"]["current_readiness_established"] is True
    assert snapshot(vertical.environment.root) == owner_before
    assert {
        path.name: (path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in vertical.environment.root.iterdir()
    } == metadata_before
    history = operation.history()
    assert history["operation"]["calls"][-1]["method"] == "reconcile"
    assert history["operation"]["calls"][-1]["result"]["outcome"] == "applied"
    facts = history["operation"]["calls"][-1]["result"]["provenance"]["facts"]
    health = discovered["operational_status"]
    assert health == facts["operational_status"]
    assert health["evaluated_at"] == facts["operational_status"]["evaluated_at"]
    assert health["scope"] == "as-of-recorded-environment-observation"
    assert health["generation"] == {
        "expected": discovered["receipt"]["effect"]["generation"],
        "observed": facts["current_activation"]["active_generation"],
        "observed_at": facts["current_activation"]["observed_at"],
    }
    assert health["generation"]["expected"] == health["generation"]["observed"]
    assert len(list(vertical.environment.root.glob("*.intent.json"))) == 1


def test_health_only_config_loss_keeps_applied_json_and_reports_unavailable_readiness(
    vertical: Vertical, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = vertical.operation()
    original = vertical.environment.owner.served

    def remove_current_config(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, content = original(deadline=deadline)
        assert record["request_digest"] == delivery.digest(operation.request)
        config = operation.directory / "config.json"
        if config.exists():
            config.unlink()
        return record, content

    monkeypatch.setattr(vertical.environment.owner, "served", remove_current_config)
    report = require_report(run_cli(operation.arguments()), 1)
    assert report["status"] == report["receipt"]["outcome"] == "applied"
    assert report["operational_status"]["status"] == "unknown"
    assert report["operational_status"]["freshness"] == "unavailable"
    assert report["operational_status"]["current_readiness_established"] is False
    assert not (operation.directory / "config.json").exists()
    history = operation.history()
    assert history["operation"]["receipt"] == report["receipt"]
    assert history["operation"]["calls"][-1]["result"]["provenance"]["facts"]["evaluation"]["status"] == "healthy"
    assert history["reserved"]["rollbacks"] == 0
    assert len(list(vertical.environment.root.glob("*.intent.json"))) == 1
