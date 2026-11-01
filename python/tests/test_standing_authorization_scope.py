"""Scope and native-grant decisions using an installed, controlled intent-before-effects adapter."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import automation_authorization as standing
from exacly import automation_revocations as revocations
from exacly import execution_store, planning
from exacly import standing_authorization as accounting

Doc = dict[str, Any]
NOW = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
NATIVE_CONFIGURATION = planning.digest("controlled installed native configuration")


def units(attempts: int = 8, wall_time_ms: int = 8000) -> Doc:
    return {"attempts": attempts, "wall_time_ms": wall_time_ms, "model_requests": 0, "model_spend_microunits": 0}


class InstalledEffect:
    """Controlled trusted adapter with an actual private intent/outcome file, not live execution."""

    adapter_id = "native-worker-v1"
    native_configuration_digest = NATIVE_CONFIGURATION

    def __init__(self, facts: Doc, native_path: Path) -> None:
        self.facts = facts
        self.native_path = native_path
        self.inspections = 0
        self.invocations = 0
        self.recoveries = 0
        self.resumes = 0
        self.cancellations = 0
        self.before_inspect: Callable[[int], None] | None = None
        self.before_invoke: Callable[[], None] | None = None
        self.after_send: Callable[[], None] | None = None
        self.recovery_override: Doc | None = None
        self.invocation_override: Doc | None = None

    def bound_ms(self, method: str, request: Doc) -> int:
        # This controlled adapter performs local file reads, never external requests or waits.
        return 0 if method in ("inspect", "recover") else 1000

    def inspect(self, request: Doc) -> Doc:
        self.inspections += 1
        if self.before_inspect is not None:
            self.before_inspect(self.inspections)
        return copy.deepcopy(self.facts)

    def _write(self, value: Doc) -> None:
        descriptor = os.open(self.native_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())

    def invoke(self, request: Doc, authorization: Doc) -> Doc:
        self.invocations += 1
        assert authorization == self.facts["authorization"]
        if self.before_invoke is not None:
            self.before_invoke()
        self._write({"request": request, "authorization": authorization, "status": "prepared", "send_count": 0})
        return self.resume(request, authorization)

    def resume(self, request: Doc, authorization: Doc) -> Doc:
        self.resumes += 1
        native = json.loads(self.native_path.read_text())
        assert native["authorization"] == authorization and native["request"] == request
        native.update(status="sent", send_count=native["send_count"] + 1)
        self._write(native)
        if self.after_send is not None:
            self.after_send()
        outcome = self.invocation_override or {
            "status": "succeeded",
            "receipt": {"native_operation": self.facts["native"]},
        }
        native.update(status="terminal", outcome=outcome)
        self._write(native)
        return outcome

    def recover(self, request: Doc, authorization: Doc) -> Doc:
        self.recoveries += 1
        if self.recovery_override is not None:
            return copy.deepcopy(self.recovery_override)
        proof = {"native": self.facts["native"], "authorization_digest": standing.digest(authorization)}
        if not self.native_path.exists():
            return {"status": "not_started", "proof": proof}
        native = json.loads(self.native_path.read_text())
        if native["authorization"] != authorization or native["request"] != request:
            return {"status": "uncertain", "reason": "Conflicting native intent."}
        if native["status"] == "terminal":
            return native["outcome"]  # type: ignore[no-any-return]
        if native["status"] == "prepared":
            return {"status": "recoverable", "proof": proof}
        return {"status": "uncertain", "reason": "Native request may already have been sent."}

    def cancel(self, request: Doc, authorization: Doc) -> Doc:
        self.cancellations += 1
        native = json.loads(self.native_path.read_text())
        assert native["request"] == request and native["authorization"] == authorization
        native.update(status="terminal", outcome={"status": "cancelled", "receipt": {"native_cancelled": True}})
        self._write(native)
        return native["outcome"]  # type: ignore[no-any-return]


@dataclass
class Scenario:
    config: Doc
    config_path: Path
    effect: InstalledEffect
    now: dt.datetime = NOW

    def write(self) -> None:
        descriptor = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(self.config, output)

    def authorizer(self, **overrides: Any) -> standing.Authorizer:
        self.write()
        arguments: Doc = {
            "approved_digest": standing.digest(self.config),
            "issuer": "independent-maintainer",
            "approval_id": "reviewed-packet-001",
            "effects": {"execute": self.effect},
            "clock": lambda: self.now,
        }
        arguments.update(overrides)
        return standing.Authorizer(self.config_path, **arguments)

    def execute(self, authorizer: standing.Authorizer, name: str = "effect", request: Doc | None = None) -> Doc:
        return authorizer.execute(
            "execute", request or {"task": "fixed fixture inventory"}, effect_id=planning.digest(name)
        )

    def history(self) -> Doc:
        result = accounting.inspect(
            Path(self.config["accounting"]["path"]),
            store_id=self.config["accounting"]["store_id"],
            objective_id=self.config["objective"]["id"],
        )
        result["decisions"] = [
            item for item in result["decisions"] if "meter" not in item["request"]["proposal"]["effect"]
        ]
        return result

    def revoke(self) -> Doc:
        return revocations.revoke(
            Path(self.config["revocations"]["path"]),
            store_id=self.config["revocations"]["store_id"],
            delegation_id=self.config["id"],
            reason="operator revoked standing authority",
        )


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    config_path = private / "delegation.json"
    accounting_path = private / "accounting.sqlite3"
    revocations_path = private / "revocations.sqlite3"
    native_path = private / "native-intent.json"
    accounting_id = accounting.initialize(accounting_path)["store_id"]
    revocation_id = revocations.initialize(revocations_path)
    objective = {
        "schema_version": "0.1.0",
        "id": planning.digest("objective"),
        "original_goal": planning.digest("original"),
    }
    contract = {key: planning.digest(key) for key in standing.CONTRACT_FIELDS}
    native = {
        "store": {"path": str(native_path), "id": planning.digest("original native binding")},
        "operation_id": planning.digest("native operation"),
    }
    facts = {
        "schema_version": "0.1.0",
        "objective_id": objective["id"],
        "original_goal": objective["original_goal"],
        "action_id": planning.digest("original action"),
        "contract": contract,
        "repository": {"id": 1411700094, "full_name": "whiteredhouse/exacly"},
        "base_revision": "a" * 40,
        "source_paths": ["fixtures/unattended-pilot/inputs/inventory.json"],
        "write_paths": ["fixtures/unattended-pilot/results/source-digests.json"],
        "destination": None,
        "native": native,
        "capabilities": ["intent-before-effects-v1", "local-receipt-read-v1", "native-worker-v1"],
        "cost": units(1, 1000),
        "preconditions": {
            "binding": planning.digest("binding"),
            "state": planning.digest("state"),
            "native_configuration_digest": NATIVE_CONFIGURATION,
        },
        "authorization": {
            "execution_id": planning.digest("execution"),
            "attempt": 1,
            "state_digest": planning.digest("state"),
        },
    }
    config = {
        "schema_version": "0.1.0",
        "id": "standing-pilot-objective",
        "issuer": "independent-maintainer",
        "approval_id": "reviewed-packet-001",
        "config_path": str(config_path),
        "accounting": {"path": str(accounting_path), "store_id": accounting_id},
        "revocations": {"path": str(revocations_path), "store_id": revocation_id},
        "objective": objective,
        "limits": units(),
        "not_before": "2026-10-31T00:00:00Z",
        "expires_at": "2026-11-02T00:00:00Z",
        "contract": contract,
        "repository": facts["repository"],
        "base_revision": facts["base_revision"],
        "allowed_paths": list(standing.PILOT_ROOTS),
        "protected_paths": ["fixtures/unattended-pilot/model.yaml", "verification/"],
        "destinations": [{"repository_id": 1411700094, "ref": "refs/heads/pilot/req039-consumer"}],
        "actions": [facts["action_id"]],
        "reads": {},
        "native_configuration_digest": NATIVE_CONFIGURATION,
        "effects": {
            "execute": {
                "adapter_id": "native-worker-v1",
                "capabilities": facts["capabilities"],
                "native_store": native["store"],
                "max_cost": units(1, 1000),
                "cancel_inflight": False,
                "recover_after_revocation": True,
                "recover_after_expiry": True,
            }
        },
    }
    return Scenario(config, config_path, InstalledEffect(facts, native_path))


def test_trusted_selection_rejects_candidate_approval_digest_issuer_provider_or_store(scenario: Scenario) -> None:
    choices: tuple[Doc, ...] = (
        {"approved_digest": planning.digest("copied template")},
        {"issuer": "candidate"},
        {"approval_id": "self-approved"},
        {"effects": {}},
    )
    for overrides in choices:
        with pytest.raises(execution_store.StoreError):
            scenario.authorizer(**overrides)
    assert not scenario.effect.native_path.exists()
    assert scenario.effect.invocations == 0


def test_exact_native_grant_and_full_approved_contract_are_retained_before_invocation(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()

    def inspect_accounting() -> None:
        decision = scenario.history()["decisions"][0]
        assert decision["status"] == "uncertain"
        proposal = decision["request"]["proposal"]
        assert proposal["authorization"] == scenario.effect.facts["authorization"]
        assert proposal["preconditions"] == scenario.effect.facts["preconditions"]
        assert proposal["effect"]["delegation"] == scenario.config
        assert proposal["effect"]["facts"] == scenario.effect.facts
        assert scenario.history()["reserved"] == units(1, 1000)

    scenario.effect.before_invoke = inspect_accounting
    result = scenario.execute(authorizer)
    assert result["status"] == "completed" and result["reservation"]["status"] == "settled"
    assert result["receipt"] == {"native_operation": scenario.effect.facts["native"]}
    assert scenario.execute(authorizer) == result
    assert scenario.effect.invocations == 1
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1
    descriptor = authorizer.identity()
    descriptor["objective"]["id"] = "caller mutation"
    assert authorizer.identity()["objective"]["id"] == scenario.config["objective"]["id"]


@pytest.mark.parametrize("contract", sorted(standing.CONTRACT_FIELDS))
def test_changed_success_contract_cannot_receive_standing_grant(scenario: Scenario, contract: str) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.facts["contract"][contract] = planning.digest("changed " + contract)
    result = scenario.execute(authorizer)
    assert result["status"] == "denied" and "contract changed" in result["reason"]
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(0, 0)


@pytest.mark.parametrize(
    "change",
    [
        "objective",
        "original",
        "action",
        "repository",
        "revision",
        "store",
        "source",
        "write",
        "destination",
        "cost",
        "capability",
    ],
)
def test_current_native_scope_must_match_every_exact_permission(scenario: Scenario, change: str) -> None:
    authorizer = scenario.authorizer()
    facts = scenario.effect.facts
    if change in ("objective", "original", "action"):
        key = {"objective": "objective_id", "original": "original_goal", "action": "action_id"}[change]
        facts[key] = planning.digest("another identity")
    elif change == "repository":
        facts["repository"] = {"id": 999, "full_name": "candidate/other"}
    elif change == "revision":
        facts["base_revision"] = "b" * 40
    elif change == "store":
        facts["native"]["store"]["id"] = planning.digest("replacement store")
    elif change == "source":
        facts["source_paths"] = ["verification/req039_inventory_test.py"]
    elif change == "write":
        facts["write_paths"] = ["fixtures/unattended-pilot/model.yaml"]
    elif change == "destination":
        facts["destination"] = {"repository_id": 1411700094, "ref": "refs/heads/pilot/unapproved"}
    elif change == "cost":
        facts["cost"]["wall_time_ms"] = 1001
    else:
        facts["capabilities"] = ["cancel-inflight-v1", *facts["capabilities"]]
    result = scenario.execute(authorizer)
    assert result["status"] == "denied"
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(0, 0)


@pytest.mark.parametrize("kind", ["merge", "release", "deploy", "destroy", "model_service", "unknown"])
def test_unknown_and_later_delivery_effects_have_no_authority(scenario: Scenario, kind: str) -> None:
    authorizer = scenario.authorizer()
    result = authorizer.execute(kind, {"approve": True}, effect_id=planning.digest(kind))
    assert result["status"] == "paused"
    assert scenario.effect.invocations == 0 and scenario.history()["decisions"] == []


def test_candidate_authorization_claims_cannot_choose_provider_or_replace_native_grant(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    candidate = {
        "authorization": {"approved": True},
        "provider": "candidate_module:Provider",
        "config": "/candidate/policy.json",
    }
    result = scenario.execute(authorizer, request=candidate)
    assert result["status"] == "completed"
    retained = json.loads(scenario.effect.native_path.read_text())
    assert retained["authorization"] == scenario.effect.facts["authorization"]
    assert retained["authorization"] != candidate["authorization"]


@pytest.mark.parametrize("field", ["extra", "capabilities", "cost", "authorization", "native"])
def test_malformed_or_unsupported_native_facts_fail_closed(scenario: Scenario, field: str) -> None:
    authorizer = scenario.authorizer()
    if field == "extra":
        scenario.effect.facts["candidate_approval"] = True
    elif field == "capabilities":
        scenario.effect.facts[field] = ["network-anywhere-v1"]
    elif field == "cost":
        scenario.effect.facts[field]["model_requests"] = 1
    elif field == "authorization":
        scenario.effect.facts[field] = {}
    else:
        scenario.effect.facts[field]["store"]["id"] = "uninitialized"
    result = scenario.execute(authorizer)
    assert result["status"] == "paused"
    assert scenario.effect.invocations == 0


@pytest.mark.parametrize("now", [dt.datetime(2026, 10, 30, tzinfo=dt.UTC), dt.datetime(2026, 11, 2, tzinfo=dt.UTC)])
def test_not_effective_or_expired_delegations_cannot_issue_new_grants(scenario: Scenario, now: dt.datetime) -> None:
    scenario.now = now
    authorizer = scenario.authorizer()
    assert not authorizer.check_available()["available"]
    assert scenario.execute(authorizer)["status"] == "denied"
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(0, 0)


def test_expiry_is_rechecked_immediately_before_new_native_invocation(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()

    def expire_after_reservation(count: int) -> None:
        if count == 2:
            scenario.now = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)

    scenario.effect.before_inspect = expire_after_reservation
    result = scenario.execute(authorizer)
    assert result["status"] == "paused" and "expired" in result["reason"]
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(1, 1000)
    assert scenario.history()["decisions"][0]["status"] == "uncertain"


def test_revocation_between_reservation_and_invocation_stops_new_effect(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()

    def revoke_after_reservation(count: int) -> None:
        if count == 2:
            scenario.revoke()

    scenario.effect.before_inspect = revoke_after_reservation
    result = scenario.execute(authorizer)
    assert result["status"] == "paused" and "revoked" in result["reason"]
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(1, 1000)
    assert not authorizer.check_available()["available"]


def test_current_preconditions_and_exact_grant_are_checked_again_after_reservation(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()

    def change_state(count: int) -> None:
        if count == 2:
            scenario.effect.facts["preconditions"]["state"] = planning.digest("new state")

    scenario.effect.before_inspect = change_state
    result = scenario.execute(authorizer)
    assert result["status"] == "paused" and "preconditions" in result["reason"]
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(1, 1000)
    assert scenario.execute(authorizer)["status"] == "paused"
    assert scenario.history()["reserved"] == units(1, 1000)


def test_ambiguous_native_outcome_is_not_retried_or_salvaged_from_candidate_output(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(RuntimeError("native response lost"))
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.effect.invocations == 1
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1
    assert scenario.history()["reserved"] == units(1, 1000)


def test_historical_native_terminal_receipt_can_be_recovered_after_revocation(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    scenario.revoke()
    result = authorizer.reconcile("execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect"))
    assert result["status"] == "completed"
    assert scenario.effect.invocations == 1
    assert scenario.execute(authorizer, "new-effect")["status"] == "denied"


def test_revocation_requests_only_separately_approved_inflight_cancellation(scenario: Scenario) -> None:
    scope = scenario.config["effects"]["execute"]
    scope["cancel_inflight"] = True
    scope["capabilities"] = ["cancel-inflight-v1", *scope["capabilities"]]
    scenario.effect.facts["capabilities"] = scope["capabilities"]
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(RuntimeError("response lost"))
    assert scenario.execute(authorizer)["status"] == "uncertain"
    scenario.revoke()
    result = authorizer.reconcile("execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect"))
    assert result["status"] == "cancelled" and scenario.effect.cancellations == 1
    assert scenario.history()["reserved"] == units(1, 2000)
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1


def test_revocation_does_not_invent_inflight_cancellation_permission(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(RuntimeError("response lost"))
    scenario.execute(authorizer)
    scenario.revoke()
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.effect.cancellations == 0 and scenario.effect.invocations == 1


def test_authoritative_not_started_proof_allows_only_the_same_reserved_invocation(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.before_invoke = lambda: (_ for _ in ()).throw(RuntimeError("crash before native intent"))
    assert scenario.execute(authorizer)["status"] == "uncertain"
    scenario.effect.before_invoke = None
    result = scenario.execute(authorizer)
    assert result["status"] == "completed"
    assert scenario.history()["reserved"] == units(1, 2000)
    assert len(scenario.history()["decisions"]) == 1
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1


@pytest.mark.parametrize("wrong", ["store", "authorization"])
def test_unproven_absence_never_authorizes_a_duplicate_uncertain_request(scenario: Scenario, wrong: str) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(RuntimeError("response lost"))
    scenario.execute(authorizer)
    proof = {
        "native": copy.deepcopy(scenario.effect.facts["native"]),
        "authorization_digest": standing.digest(scenario.effect.facts["authorization"]),
    }
    if wrong == "store":
        proof["native"]["store"]["id"] = planning.digest("another store")
    else:
        proof["authorization_digest"] = planning.digest("another grant")
    scenario.effect.recovery_override = {"status": "not_started", "proof": proof}
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.effect.invocations == 1


def test_changed_request_cannot_reuse_one_retained_approval(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.execute(authorizer)
    assert scenario.execute(authorizer, request={"task": "different work"})["status"] == "uncertain"
    assert scenario.effect.invocations == 1


def test_new_policy_revision_and_action_ids_do_not_refill_objective_budget(scenario: Scenario) -> None:
    scenario.config["limits"] = units(1, 1000)
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    scenario.config["id"] = "independently-reviewed-policy-v2"
    scenario.config["approval_id"] = "reviewed-packet-002"
    second = scenario.authorizer(approval_id="reviewed-packet-002")
    assert scenario.execute(second, "another-action")["status"] == "denied"
    assert scenario.history()["reserved"] == units(1, 1000)


def test_alias_replacement_accounting_and_changed_limits_require_reviewed_migration(scenario: Scenario) -> None:
    scenario.authorizer()
    scenario.config["objective"]["id"] = planning.digest("workflow alias")
    with pytest.raises(execution_store.StoreError, match="already enrolled"):
        scenario.authorizer()
    scenario.config["objective"]["id"] = planning.digest("objective")
    scenario.config["limits"] = units(999, 99999)
    with pytest.raises(execution_store.StoreError, match="immutable"):
        scenario.authorizer()


def test_missing_revocation_or_accounting_storage_stops_new_effects(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    Path(scenario.config["revocations"]["path"]).unlink()
    assert not authorizer.check_available()["available"]
    assert scenario.execute(authorizer)["status"] == "paused"
    assert scenario.effect.invocations == 0


def test_reconcile_without_decision_never_creates_authority_or_invokes_an_effect(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    result = authorizer.reconcile("execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect"))
    assert result["status"] == "not_started" and result["reservation"] is None
    assert scenario.effect.invocations == 0 and scenario.history()["decisions"] == []


@pytest.mark.parametrize("state", ["allowed", "revoked", "unapproved"])
def test_prepared_native_recovery_needs_its_current_separately_approved_scope(scenario: Scenario, state: str) -> None:
    if state != "unapproved":
        capabilities = sorted(["finish-prepared-v1", *scenario.config["effects"]["execute"]["capabilities"]])
        scenario.config["effects"]["execute"]["capabilities"] = capabilities
        scenario.effect.facts["capabilities"] = capabilities
    authorizer = scenario.authorizer()

    def prepared_before_crash() -> None:
        scenario.effect._write(
            {
                "request": {"task": "fixed fixture inventory"},
                "authorization": scenario.effect.facts["authorization"],
                "status": "prepared",
                "send_count": 0,
            }
        )
        raise RuntimeError("crash after native intent")

    scenario.effect.before_invoke = prepared_before_crash
    assert scenario.execute(authorizer)["status"] == "uncertain"
    scenario.effect.before_invoke = None
    if state == "revoked":
        scenario.revoke()
    discovered = authorizer.reconcile(
        "execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect")
    )
    assert discovered["status"] == "recoverable" and scenario.effect.resumes == 0
    result = scenario.execute(authorizer)
    assert result["status"] == ("completed" if state == "allowed" else "paused" if state == "revoked" else "uncertain")
    assert scenario.effect.resumes == (1 if state == "allowed" else 0)
    assert scenario.history()["reserved"] == units(1, 2000 if state == "allowed" else 1000)


@pytest.mark.parametrize(
    "field", ["extra", "merge", "model_service", "protected", "overlap", "main_ref", "lifetime", "duplicate_action"]
)
def test_closed_delegation_rejects_unsupported_or_ambiguous_authority(scenario: Scenario, field: str) -> None:
    config = scenario.config
    if field == "extra":
        config["blanket_retry"] = True
    elif field == "merge":
        config["effects"]["merge"] = config["effects"]["execute"]
    elif field == "model_service":
        config["limits"]["model_requests"] = 1
    elif field == "protected":
        config["allowed_paths"] = ["python/src/exacly/"]
    elif field == "overlap":
        config["protected_paths"] = ["fixtures/unattended-pilot/results/"]
    elif field == "main_ref":
        config["destinations"][0]["ref"] = "refs/heads/main"
    elif field == "lifetime":
        config["expires_at"] = config["not_before"]
    else:
        config["actions"] *= 2
    with pytest.raises(execution_store.StoreError):
        scenario.authorizer()
    assert scenario.effect.invocations == 0


def test_config_cannot_be_copied_or_substituted_after_trusted_selection(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    changed = copy.deepcopy(scenario.config)
    changed["approval_id"] = "candidate-approved"
    scenario.config_path.write_text(json.dumps(changed))
    assert scenario.execute(authorizer)["status"] == "paused"
    assert scenario.effect.invocations == 0
    scenario.write()
    copied = scenario.config_path.with_name("copied.json")
    copied.write_bytes(scenario.config_path.read_bytes())
    copied.chmod(0o600)
    with pytest.raises(execution_store.StoreError, match="another path"):
        standing.Authorizer(
            copied,
            approved_digest=standing.digest(scenario.config),
            issuer="independent-maintainer",
            approval_id="reviewed-packet-001",
            effects={"execute": scenario.effect},
        )


def test_lost_native_terminal_history_never_turns_a_settled_decision_into_retry_permission(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    scenario.effect.native_path.unlink()
    replay = scenario.execute(authorizer)
    assert replay["status"] == "uncertain" and "settled outcome" in replay["reason"]
    assert scenario.effect.invocations == 1 and not scenario.effect.native_path.exists()
    assert scenario.history()["decisions"][0]["status"] == "settled"
    assert scenario.history()["reserved"] == units(1, 1000)


def test_native_operation_cannot_be_aliased_under_a_new_standing_effect_identity(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    scenario.effect.native_path.unlink()  # Even native history loss cannot make the operation new.
    result = scenario.execute(authorizer, "new-workflow-alias")
    assert result["status"] == "denied" and "another stable" in result["reason"]
    assert scenario.effect.invocations == 1
    assert scenario.history()["reserved"] == units(1, 1000)


def test_unaccounted_preexisting_native_operation_requires_original_authority_review(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect._write(
        {
            "request": {"task": "fixed fixture inventory"},
            "authorization": scenario.effect.facts["authorization"],
            "status": "sent",
            "send_count": 1,
        }
    )
    result = scenario.execute(authorizer)
    assert result["status"] == "denied" and "original authority" in result["reason"]
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(0, 0)


def test_missing_decision_reconciliation_checks_the_pinned_native_scope_before_discovery(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.facts["native"]["store"]["id"] = planning.digest("another journal")
    result = authorizer.reconcile("execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect"))
    assert result["status"] == "paused"
    assert scenario.effect.recoveries == 0 and scenario.effect.invocations == 0


class ControlledReader:
    """A local controlled read adapter; no live producer or physical deadline claim."""

    adapter_id = "native-producer-observer-v1"
    native_configuration_digest = NATIVE_CONFIGURATION

    def __init__(self, repository: Doc, contract_digest: str, wall_time_ms: int, log: Path | None = None) -> None:
        self.repository = repository
        self.contract_digest = contract_digest
        self.wall_time_ms = wall_time_ms
        self.log = log
        self.calls = 0
        self.before_read: Callable[[], None] | None = None
        self.after_read: Callable[[], None] | None = None

    def inspect(self, request: Doc) -> Doc:
        return {
            "schema_version": "0.1.0",
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "wall_time_ms": self.wall_time_ms,
            "native_configuration_digest": self.native_configuration_digest,
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        assert wall_time_ms == self.wall_time_ms
        if self.before_read is not None:
            self.before_read()
        self.calls += 1
        if self.log is not None:
            descriptor = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, b"read\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if self.after_read is not None:
            self.after_read()
        return {"selection": {"run_id": self.calls, "attempt": 1}, "request": request}


def metered_reader(scenario: Scenario, *, budget: int = 2000, bound: int = 1000) -> ControlledReader:
    scenario.config["limits"]["wall_time_ms"] = budget
    contract = planning.digest("independently approved exact producer contract")
    scenario.config["reads"] = {
        "producer-observe": {
            "adapter_id": "native-producer-observer-v1",
            "max_wall_time_ms": bound,
            "contract_digest": contract,
        }
    }
    return ControlledReader(scenario.config["repository"], contract, bound)


def raw_history(scenario: Scenario) -> Doc:
    return accounting.inspect(
        Path(scenario.config["accounting"]["path"]),
        store_id=scenario.config["accounting"]["store_id"],
        objective_id=scenario.config["objective"]["id"],
    )


def test_every_fresh_poll_including_same_correlation_and_restart_spends_authoritative_capacity(
    scenario: Scenario,
) -> None:
    reader = metered_reader(scenario)
    first = scenario.authorizer(readers={"producer-observe": reader})
    operation_id = planning.digest("durable controller phase")
    initial = first.call_metered("producer-observe", {"revision": "a" * 40}, operation_id=operation_id)
    second = first.call_metered("producer-observe", {"revision": "a" * 40}, operation_id=operation_id)
    assert initial["status"] == second["status"] == "completed"
    assert initial["receipt"]["result"]["selection"]["run_id"] == 1
    assert second["receipt"]["result"]["selection"]["run_id"] == 2
    assert initial["reservation"]["proposal_id"] != second["reservation"]["proposal_id"]
    resumed = scenario.authorizer(readers={"producer-observe": reader})
    exhausted = resumed.call_metered(
        "producer-observe", {"revision": "a" * 40}, operation_id=planning.digest("new alias")
    )
    assert exhausted["status"] == "denied" and "budget exhausted" in exhausted["reason"]
    assert reader.calls == 2
    assert raw_history(scenario)["reserved"] == units(0, 2000)


def test_read_slot_and_uncertainty_are_durable_before_the_bounded_reader_is_released(scenario: Scenario) -> None:
    reader = metered_reader(scenario, budget=1000)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})

    def inspect_reservation() -> None:
        history = raw_history(scenario)
        assert history["reserved"] == units(0, 1000)
        assert len(history["decisions"]) == 1
        decision = history["decisions"][0]
        assert decision["status"] == "uncertain"
        assert decision["request"]["proposal"]["authorization"]["wall_time_ms"] == 1000

    reader.before_read = inspect_reservation
    report = authorizer.call_metered(
        "producer-observe", {"read": "exact producer"}, operation_id=planning.digest("poll")
    )
    assert report["status"] == "completed" and report["reservation"]["status"] == "settled"
    assert raw_history(scenario)["decisions"][0]["status"] == "settled"


def test_interrupted_read_retains_full_capacity_and_cannot_be_replayed_as_a_free_fresh_read(scenario: Scenario) -> None:
    reader = metered_reader(scenario)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    reader.after_read = lambda: (_ for _ in ()).throw(RuntimeError("producer response lost"))
    first = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))
    assert first["status"] == "uncertain" and first["reservation"]["status"] == "uncertain"
    assert raw_history(scenario)["reserved"] == units(0, 1000)
    reader.after_read = None
    second = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))
    assert second["status"] == "completed"
    assert second["reservation"]["proposal_id"] != first["reservation"]["proposal_id"]
    assert (
        authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("another alias"))["status"]
        == "denied"
    )
    assert reader.calls == 2 and raw_history(scenario)["reserved"] == units(0, 2000)


def test_native_inspection_and_historical_recovery_each_consume_their_full_enforced_ceiling(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario.config["limits"]["wall_time_ms"] = 1600
    monkeypatch.setattr(
        scenario.effect, "bound_ms", lambda method, request: {"inspect": 100, "recover": 200}.get(method, 1000)
    )
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    assert raw_history(scenario)["reserved"] == units(1, 1400)
    assert scenario.execute(authorizer)["status"] == "completed"
    assert raw_history(scenario)["reserved"] == units(1, 1600)
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert "budget exhausted" in scenario.execute(authorizer)["reason"]
    assert scenario.effect.recoveries == 2 and scenario.effect.invocations == 1
    assert raw_history(scenario)["reserved"] == units(1, 1600)


def test_nominal_native_cost_cannot_underreserve_the_actual_compound_call_bound(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario.config["effects"]["execute"]["max_cost"]["wall_time_ms"] = 3000
    monkeypatch.setattr(scenario.effect, "bound_ms", lambda method, request: 2000 if method == "invoke" else 0)
    authorizer = scenario.authorizer()
    result = scenario.execute(authorizer)
    assert result["status"] == "denied" and "undercharges" in result["reason"]
    assert scenario.effect.invocations == 0 and raw_history(scenario)["reserved"] == units(0, 0)


def test_prepared_resume_must_reserve_new_recovery_time_in_the_same_exhausted_objective(scenario: Scenario) -> None:
    scenario.config["limits"]["wall_time_ms"] = 1000
    capabilities = sorted(["finish-prepared-v1", *scenario.config["effects"]["execute"]["capabilities"]])
    scenario.config["effects"]["execute"]["capabilities"] = capabilities
    scenario.effect.facts["capabilities"] = capabilities
    authorizer = scenario.authorizer()

    def retain_prepared() -> None:
        scenario.effect._write(
            {
                "request": {"task": "fixed fixture inventory"},
                "authorization": scenario.effect.facts["authorization"],
                "status": "prepared",
                "send_count": 0,
            }
        )
        raise RuntimeError("coordinator stopped after native intent")

    scenario.effect.before_invoke = retain_prepared
    assert scenario.execute(authorizer)["status"] == "uncertain"
    scenario.effect.before_invoke = None
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.effect.resumes == 0
    assert raw_history(scenario)["reserved"] == units(1, 1000)
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 0


@pytest.mark.parametrize("permitted", [False, True])
def test_revoked_historical_recovery_needs_explicit_scope_and_still_exhausts_its_objective_budget(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, permitted: bool
) -> None:
    scenario.config["limits"]["wall_time_ms"] = 1500
    scenario.config["effects"]["execute"]["recover_after_revocation"] = permitted
    monkeypatch.setattr(scenario.effect, "bound_ms", lambda method, request: 1000 if method == "invoke" else 100)
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    assert raw_history(scenario)["reserved"] == units(1, 1300)
    scenario.revoke()
    outcomes = [scenario.execute(authorizer)["status"] for _ in range(3)]
    assert outcomes == (["completed", "completed", "uncertain"] if permitted else ["uncertain"] * 3)
    assert scenario.effect.invocations == 1
    assert scenario.effect.recoveries == (3 if permitted else 1)
    assert raw_history(scenario)["reserved"] == units(1, 1500 if permitted else 1300)


def test_new_producer_reads_stop_after_revocation_or_expiry_without_spending_or_releasing_more_requests(
    scenario: Scenario,
) -> None:
    reader = metered_reader(scenario)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    scenario.revoke()
    result = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))
    assert result["status"] == "paused" and reader.calls == 0
    assert raw_history(scenario)["reserved"] == units(0, 0)


@pytest.mark.parametrize("changed", ["repository", "contract", "bound", "provider"])
def test_read_only_scope_pins_repository_contract_installed_adapter_and_actual_bound(
    scenario: Scenario, changed: str
) -> None:
    reader = metered_reader(scenario)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    if changed == "repository":
        reader.repository = {"id": 999, "full_name": "another/repository"}
    elif changed == "contract":
        reader.contract_digest = planning.digest("unapproved producer")
    elif changed == "bound":
        reader.wall_time_ms += 1
    else:
        reader.adapter_id = "candidate-provider"
    result = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))
    assert result["status"] == "paused" and reader.calls == 0
    assert raw_history(scenario)["reserved"] == units(0, 0)


def test_changed_approved_policy_version_does_not_refill_read_capacity(scenario: Scenario) -> None:
    reader = metered_reader(scenario, budget=1000)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    assert (
        authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))["status"] == "completed"
    )
    scenario.config["id"] = "independently-reviewed-v2"
    scenario.config["approval_id"] = "reviewed-packet-002"
    replacement = scenario.authorizer(readers={"producer-observe": reader}, approval_id="reviewed-packet-002")
    assert (
        replacement.call_metered("producer-observe", {}, operation_id=planning.digest("new policy alias"))["status"]
        == "denied"
    )
    assert reader.calls == 1 and raw_history(scenario)["reserved"] == units(0, 1000)


def test_revocation_after_read_reservation_is_rechecked_before_releasing_the_request(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = metered_reader(scenario, budget=1000)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    original = accounting.mark_uncertain

    def revoke_after_commit(*args: Any, **kwargs: Any) -> Doc:
        result = original(*args, **kwargs)
        scenario.revoke()
        return result

    monkeypatch.setattr(accounting, "mark_uncertain", revoke_after_commit)
    report = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))
    assert report["status"] == "uncertain" and reader.calls == 0
    assert "revoked" in report["reason"]
    assert raw_history(scenario)["reserved"] == units(0, 1000)


def test_meter_slot_collision_cannot_replay_a_previous_request_as_fresh_authority(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    reader = metered_reader(scenario)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    selected = uuid.UUID("0123456789ab4def8123456789abcdef")
    monkeypatch.setattr(uuid, "uuid4", lambda: selected)
    assert (
        authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))["status"] == "completed"
    )
    replay = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("another poll"))
    assert replay["status"] == "paused" and "already exists" in replay["reason"]
    assert reader.calls == 1 and raw_history(scenario)["reserved"] == units(0, 1000)


def test_zero_bound_cannot_hide_an_external_or_unsupported_control_request(scenario: Scenario) -> None:
    capabilities = [item for item in scenario.effect.facts["capabilities"] if item != "local-receipt-read-v1"]
    scenario.config["effects"]["execute"]["capabilities"] = capabilities
    scenario.effect.facts["capabilities"] = capabilities
    authorizer = scenario.authorizer()
    report = scenario.execute(authorizer)
    assert report["status"] == "paused" and "purely local" in report["reason"]
    assert scenario.effect.inspections == 0 and raw_history(scenario)["reserved"] == units(0, 0)


def test_repeated_not_started_invocations_each_reserve_another_full_enforced_wall_ceiling(scenario: Scenario) -> None:
    scenario.config["limits"]["wall_time_ms"] = 2000
    authorizer = scenario.authorizer()
    scenario.effect.before_invoke = lambda: (_ for _ in ()).throw(RuntimeError("spent bounded time before intent"))
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.effect.invocations == 2
    stopped = scenario.execute(authorizer)
    assert stopped["status"] == "uncertain" and "budget exhausted" in stopped["reason"]
    assert scenario.effect.invocations == 2 and not scenario.effect.native_path.exists()
    assert raw_history(scenario)["reserved"] == units(1, 2000)
    assert len(scenario.history()["decisions"]) == 1  # Same native permission; time is charged for every call.


def test_baseline_admission_uses_its_true_separately_approved_metered_reader_identity(scenario: Scenario) -> None:
    reader = metered_reader(scenario, budget=1000)
    reader.adapter_id = "native-baseline-admission-v1"
    baseline_contract = planning.digest(
        {
            "input_revision": "a" * 40,
            "run_id": 123,
            "attempt": 1,
            "producer_contract": reader.contract_digest,
            "operation": "read-only-reconcile",
        }
    )
    reader.contract_digest = baseline_contract
    scenario.config["reads"] = {
        "baseline-admission": {
            "adapter_id": reader.adapter_id,
            "max_wall_time_ms": 1000,
            "contract_digest": baseline_contract,
        }
    }
    authorizer = scenario.authorizer(readers={"baseline-admission": reader})
    request = {"input_revision": "a" * 40, "run_id": 123, "attempt": 1}
    result = authorizer.call_metered("baseline-admission", request, operation_id=planning.digest("baseline-read"))
    assert result["status"] == "completed" and result["receipt"]["result"]["request"] == request
    meter = raw_history(scenario)["decisions"][0]["request"]["proposal"]["effect"]["meter"]
    assert meter["purpose"] == "baseline-admission" and meter["adapter_id"] == "native-baseline-admission-v1"
    assert raw_history(scenario)["reserved"] == units(0, 1000)
    assert scenario.effect.invocations == 0  # A baseline read grants no completion or state-publication effect.


def test_baseline_admission_cannot_relabel_the_producer_observer_as_its_adapter(scenario: Scenario) -> None:
    reader = metered_reader(scenario)
    scenario.config["reads"] = {"baseline-admission": scenario.config["reads"]["producer-observe"]}
    with pytest.raises(execution_store.StoreError, match="unsupported installed read-only adapter"):
        scenario.authorizer(readers={"baseline-admission": reader})
    assert reader.calls == 0


def alternate_policy(scenario: Scenario, label: str = "B") -> Scenario:
    config = copy.deepcopy(scenario.config)
    config_path = scenario.config_path.with_name("delegation-" + label + ".json")
    config.update(id="independently-approved-policy-" + label, config_path=str(config_path))
    facts = copy.deepcopy(scenario.effect.facts)
    facts["action_id"] = planning.digest("independently approved action " + label)
    native_path = scenario.effect.native_path.with_name("native-intent-" + label + ".json")
    facts["native"] = {
        "store": {"path": str(native_path), "id": planning.digest("native journal " + label)},
        "operation_id": planning.digest("native operation " + label),
    }
    facts["authorization"]["execution_id"] = planning.digest("native execution " + label)
    config["actions"] = [facts["action_id"]]
    config["effects"]["execute"]["native_store"] = facts["native"]["store"]
    return Scenario(config, config_path, InstalledEffect(facts, native_path))


def test_different_approved_policy_action_and_native_journal_cannot_forget_an_uncertain_write(
    scenario: Scenario,
) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("response lost after native send"))
    original = scenario.execute(authorizer, "A")
    assert original["status"] == "uncertain"
    replacement = alternate_policy(scenario)
    other = replacement.authorizer()
    blocked = replacement.execute(other, "B")
    assert blocked["status"] == "paused" and "Unresolved original-objective" in blocked["reason"]
    assert planning.digest("A") in blocked["reason"] and str(scenario.effect.native_path) in blocked["reason"]
    assert replacement.effect.inspections == 0 and not replacement.effect.native_path.exists()
    assert raw_history(scenario)["reserved"] == units(1, 1000)
    original_native = json.loads(scenario.effect.native_path.read_text())
    assert original_native["status"] == "sent" and original_native["send_count"] == 1


def test_other_policy_cannot_reconcile_original_write_through_its_own_empty_native_journal(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("response lost"))
    scenario.execute(authorizer, "A")
    replacement = alternate_policy(scenario)
    other = replacement.authorizer()
    result = other.reconcile("execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("A"))
    assert result["status"] == "uncertain" and "another adapter or canonical journal" in result["reason"]
    assert replacement.effect.recoveries == 0
    assert replacement.execute(other, "B")["status"] == "paused"
    assert scenario.history()["decisions"][0]["status"] == "uncertain"


def test_exact_original_native_terminal_reconciliation_releases_only_its_objective_fence(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("response lost"))
    scenario.execute(authorizer, "A")
    replacement = alternate_policy(scenario)
    other = replacement.authorizer()
    assert replacement.execute(other, "B")["status"] == "paused"
    native = json.loads(scenario.effect.native_path.read_text())
    native.update(
        status="terminal",
        outcome={"status": "succeeded", "receipt": {"native_operation": scenario.effect.facts["native"]}},
    )
    scenario.effect._write(native)  # Controlled native source establishes its actual terminal receipt.
    recovered = authorizer.reconcile("execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("A"))
    assert recovered["status"] == "completed"
    assert replacement.execute(other, "B")["status"] == "completed"
    assert scenario.effect.invocations == replacement.effect.invocations == 1
    assert raw_history(scenario)["reserved"] == units(2, 2000)


def test_uncertain_read_slots_do_not_fence_native_writes_and_unknown_writes_do_not_fence_pure_reads(
    scenario: Scenario,
) -> None:
    reader = metered_reader(scenario, budget=8000)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    reader.after_read = lambda: (_ for _ in ()).throw(OSError("read response lost"))
    assert (
        authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("poll"))["status"] == "uncertain"
    )
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("native response lost"))
    assert scenario.execute(authorizer, "A")["status"] == "uncertain"
    reader.after_read = None
    fresh = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("fresh poll"))
    assert fresh["status"] == "completed" and reader.calls == 2
    assert raw_history(scenario)["reserved"] == units(1, 3000)
    assert scenario.effect.invocations == 1


def test_missing_or_changed_objective_lock_after_authority_is_not_silently_reinitialized(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("native response lost"))
    scenario.execute(authorizer)
    paths = list(scenario.config_path.parent.glob(".exacly-objective-*.lock"))
    assert len(paths) == 1
    path = paths[0]
    identity = json.loads(path.read_text())
    assert identity["store_id"] == scenario.config["accounting"]["store_id"]
    assert identity["objective"] == scenario.config["objective"]
    path.unlink()
    replacement = alternate_policy(scenario)
    with pytest.raises(execution_store.StoreError, match="missing after native authority"):
        replacement.authorizer()
    assert scenario.execute(authorizer)["status"] == "paused"
    assert not path.exists() and scenario.effect.invocations == 1


@pytest.mark.parametrize("damage", ["identity", "permissions", "symlink", "hardlink"])
def test_objective_mutex_identity_and_private_file_boundary_are_checked(scenario: Scenario, damage: str) -> None:
    authorizer = scenario.authorizer()
    path = next(scenario.config_path.parent.glob(".exacly-objective-*.lock"))
    if damage == "identity":
        path.write_text("{}\n")
    elif damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        original = path.with_name("original-lock")
        path.rename(original)
        path.symlink_to(original)
    else:
        os.link(path, path.with_name("hardlinked-lock"))
    report = scenario.execute(authorizer)
    assert report["status"] == "paused" and scenario.effect.invocations == 0


def test_explicit_current_cancellation_is_metered_without_revoking_the_whole_delegation(scenario: Scenario) -> None:
    scope = scenario.config["effects"]["execute"]
    scope["cancel_inflight"] = True
    capabilities = sorted(["cancel-inflight-v1", *scope["capabilities"]])
    scope["capabilities"] = scenario.effect.facts["capabilities"] = capabilities
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("native response lost"))
    scenario.execute(authorizer)
    result = authorizer.request_cancel(
        "execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect")
    )
    assert result["status"] == "cancelled" and scenario.effect.cancellations == 1
    assert authorizer.check_available()["available"]
    assert raw_history(scenario)["reserved"] == units(1, 2000)
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1


def test_cancellation_neither_invents_permission_nor_synthesizes_terminal_absence(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.before_invoke = lambda: (_ for _ in ()).throw(OSError("before native intent"))
    scenario.execute(authorizer)
    denied = authorizer.request_cancel(
        "execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect")
    )
    assert denied["status"] == "uncertain" and "outside" in denied["reason"]
    assert scenario.effect.cancellations == 0
    scope = scenario.config["effects"]["execute"]
    scope["cancel_inflight"] = True
    capabilities = sorted(["cancel-inflight-v1", *scope["capabilities"]])
    scope["capabilities"] = scenario.effect.facts["capabilities"] = capabilities
    approved = scenario.authorizer()
    absent = approved.request_cancel(
        "execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect")
    )
    assert absent["status"] == "paused" and "No native intent" in absent["reason"]
    assert scenario.effect.cancellations == 0 and scenario.history()["decisions"][0]["status"] == "uncertain"


def test_native_configuration_pin_is_required_in_the_independently_approved_delegation(scenario: Scenario) -> None:
    scenario.config.pop("native_configuration_digest")
    with pytest.raises(execution_store.StoreError, match="unknown or missing"):
        scenario.authorizer()
    assert scenario.effect.invocations == 0


def test_changed_actual_native_configuration_cannot_borrow_matching_model_and_policy_facts(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.native_configuration_digest = planning.digest("changed marker refs timers or canonical controller")
    result = scenario.execute(authorizer)
    assert result["status"] == "paused" and "native configuration" in result["reason"]
    assert scenario.effect.inspections == scenario.effect.invocations == 0
    assert raw_history(scenario)["reserved"] == units(0, 0)


def test_native_facts_cannot_self_approve_a_different_component_configuration(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.facts["preconditions"]["native_configuration_digest"] = planning.digest(
        "self-hashed changed components"
    )
    result = scenario.execute(authorizer)
    assert result["status"] == "denied" and "configuration pin" in result["reason"]
    assert scenario.effect.invocations == 0
    assert raw_history(scenario)["reserved"] == units(0, 0)


def test_actual_native_configuration_is_rechecked_immediately_before_first_invoke(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()

    def change_components_after_inspection(count: int) -> None:
        if count == 2:
            scenario.effect.native_configuration_digest = planning.digest("changed current timer and marker scope")

    scenario.effect.before_inspect = change_components_after_inspection
    result = scenario.execute(authorizer)
    assert result["status"] == "uncertain" and "native configuration" in result["reason"]
    assert scenario.effect.invocations == 0
    assert raw_history(scenario)["reserved"] == units(1, 1000)


@pytest.mark.parametrize("operation", ["recover", "cancel"])
def test_changed_actual_native_configuration_cannot_recover_or_cancel_through_a_replacement(
    scenario: Scenario, operation: str
) -> None:
    capabilities = sorted(["cancel-inflight-v1", *scenario.config["effects"]["execute"]["capabilities"]])
    scenario.config["effects"]["execute"].update(cancel_inflight=True, capabilities=capabilities)
    scenario.effect.facts["capabilities"] = capabilities
    authorizer = scenario.authorizer()
    scenario.effect.after_send = lambda: (_ for _ in ()).throw(OSError("native response lost"))
    scenario.execute(authorizer)
    recoveries = scenario.effect.recoveries
    scenario.effect.native_configuration_digest = planning.digest("changed active native components")
    if operation == "recover":
        report = authorizer.reconcile(
            "execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect")
        )
    else:
        report = authorizer.request_cancel(
            "execute", {"task": "fixed fixture inventory"}, effect_id=planning.digest("effect")
        )
    assert report["status"] == "uncertain" and "native configuration" in report["reason"]
    assert scenario.effect.recoveries == recoveries and scenario.effect.cancellations == 0
    assert json.loads(scenario.effect.native_path.read_text())["status"] == "sent"


def test_fresh_reader_must_present_the_independently_approved_native_configuration_pin(scenario: Scenario) -> None:
    reader = metered_reader(scenario)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    reader.native_configuration_digest = planning.digest("changed reader marker keys or timeouts")
    result = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("read"))
    assert result["status"] == "paused" and reader.calls == 0
    assert raw_history(scenario)["reserved"] == units(0, 0)


def test_reader_configuration_change_after_reservation_does_not_release_an_unapproved_read(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = metered_reader(scenario)
    authorizer = scenario.authorizer(readers={"producer-observe": reader})
    original = accounting.mark_uncertain

    def change_reader_after_reservation(*args: Any, **kwargs: Any) -> Doc:
        result = original(*args, **kwargs)
        reader.native_configuration_digest = planning.digest("changed checked reader components")
        return result

    monkeypatch.setattr(accounting, "mark_uncertain", change_reader_after_reservation)
    result = authorizer.call_metered("producer-observe", {}, operation_id=planning.digest("read"))
    assert result["status"] == "uncertain" and reader.calls == 0
    assert "configuration changed" in result["reason"]
    assert raw_history(scenario)["reserved"] == units(0, 1000)
