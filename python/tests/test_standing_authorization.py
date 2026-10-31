from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any

import pytest
from exacly import execution_store, planning
from exacly import standing_authorization as authority

Doc = dict[str, Any]


def units(attempts: int = 2, wall_time_ms: int = 100, model_requests: int = 3, model_spend_microunits: int = 50) -> Doc:
    return dict(
        zip(authority.DIMENSIONS, (attempts, wall_time_ms, model_requests, model_spend_microunits), strict=True)
    )


def objective(name: str = "objective", goal: str = "original goal") -> Doc:
    return {"schema_version": "0.1.0", "id": planning.digest(name), "original_goal": planning.digest(goal)}


def proposal(name: str = "first", cost: Doc | None = None) -> Doc:
    return {
        "schema_version": "0.1.0",
        "id": planning.digest(name),
        "delegation_id": "operator-reviewed-delegation-v1",
        "policy_digest": planning.digest("approved policy"),
        "effect": {"operation": "execute", "action": planning.digest("action"), "revision": "a" * 40},
        "preconditions": {"state": planning.digest("current state"), "binding": planning.digest("active binding")},
        "authorization": {"effect": "run-local-attempt", "invocation": "exact invocation"},
        "cost": cost if cost is not None else units(1, 50, 1, 25),
    }


@pytest.fixture
def store(tmp_path: Path) -> tuple[Path, str]:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    path = private / "authority.sqlite3"
    result = authority.initialize(path)
    return path, result["store_id"]


def enroll(store: tuple[Path, str], limits: Doc | None = None) -> Doc:
    path, store_id = store
    return authority.enroll(path, store_id=store_id, objective=objective(), limits=limits or units())


def reserve(store: tuple[Path, str], value: Doc | None = None, *, approved: bool = True) -> Doc:
    path, store_id = store
    return authority.reserve(
        path,
        store_id=store_id,
        objective_id=objective()["id"],
        proposal=value or proposal(),
        approved=approved,
        reason="trusted authorizer checked exact current scope",
    )


def inspect(store: tuple[Path, str]) -> Doc:
    path, store_id = store
    return authority.inspect(path, store_id=store_id, objective_id=objective()["id"])


def test_initialization_is_explicit_private_and_pins_unique_store_identity(tmp_path: Path) -> None:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    path = private / "authority.sqlite3"
    with pytest.raises(execution_store.StoreError, match="cannot open"):
        authority.inspect(path, store_id=uuid.uuid4().hex, objective_id=objective()["id"])
    assert list(private.iterdir()) == []
    first = authority.initialize(path)
    assert uuid.UUID(hex=first["store_id"]).version == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    with pytest.raises(execution_store.StoreError, match="new destination"):
        authority.initialize(path)
    assert path.read_bytes() == before
    second = authority.initialize(private / "another.sqlite3")
    assert first["store_id"] != second["store_id"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (authority.APPLICATION_ID,)
        assert connection.execute("PRAGMA user_version").fetchone() == (authority.USER_VERSION,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_objective_enrollment_is_immutable_and_an_alias_cannot_reset_limits(store: tuple[Path, str]) -> None:
    initial = enroll(store)
    path, store_id = store
    assert authority.enroll(path, store_id=store_id, objective=objective(), limits=units()) == initial
    for changed, limits in (
        (objective("alias"), units()),
        (objective(), units(20)),
        (objective(goal="changed"), units()),
    ):
        with pytest.raises(execution_store.StoreError, match=r"immutable|already enrolled"):
            authority.enroll(path, store_id=store_id, objective=changed, limits=limits)
    assert inspect(store)["reserved"] == units(0, 0, 0, 0)


def test_reservation_is_exact_durable_and_replay_does_not_consume_twice(store: tuple[Path, str]) -> None:
    enroll(store)
    proposed = proposal()
    original = copy.deepcopy(proposed)
    decision = reserve(store, proposed)
    assert decision["approved"] and decision["status"] == "reserved"
    assert decision["request"]["proposal"] == original
    before = store[0].read_bytes()
    assert reserve(store, proposed) == decision
    assert store[0].read_bytes() == before
    assert inspect(store)["reserved"] == original["cost"]
    proposed["authorization"]["invocation"] = "mutated caller content"
    decision["request"]["proposal"]["authorization"]["invocation"] = "mutated returned content"
    assert inspect(store)["decisions"][0]["request"]["proposal"] == original
    with pytest.raises(execution_store.StoreError, match="conflicts"):
        reserve(store, proposed)


@pytest.mark.parametrize(
    "field", ["policy_digest", "delegation_id", "effect", "preconditions", "authorization", "cost"]
)
def test_proposal_identity_cannot_be_reused_for_changed_authority_or_preconditions(
    store: tuple[Path, str], field: str
) -> None:
    enroll(store)
    reserve(store)
    changed = proposal()
    if field == "policy_digest":
        changed[field] = planning.digest("new policy")
    elif field == "delegation_id":
        changed[field] = "new independently reviewed delegation"
    elif field == "cost":
        changed[field] = units(0, 0, 0, 0)
    else:
        changed[field]["changed"] = True
    before = store[0].read_bytes()
    with pytest.raises(execution_store.StoreError, match="conflicts"):
        reserve(store, changed)
    assert store[0].read_bytes() == before


@pytest.mark.parametrize("dimension", authority.DIMENSIONS)
def test_each_budget_dimension_is_atomic_and_exact_exhaustion_denies_new_effects(
    store: tuple[Path, str], dimension: str
) -> None:
    limits = units()
    limits[dimension] = 0
    enroll(store, limits)
    decision = reserve(store)
    assert not decision["approved"] and decision["status"] == "denied"
    assert dimension in decision["reason"]
    assert inspect(store)["reserved"] == units(0, 0, 0, 0)
    assert reserve(store) == decision


def test_different_actions_revisions_policies_and_delegations_share_consumed_capacity(store: tuple[Path, str]) -> None:
    enroll(store)
    reserve(store)
    second = proposal("second")
    second["delegation_id"] = "reviewed-policy-v2"
    second["policy_digest"] = planning.digest("policy-v2")
    second["effect"] = {"operation": "handoff", "revision": "b" * 40, "action": planning.digest("replanned action")}
    assert reserve(store, second)["approved"]
    assert inspect(store)["reserved"] == units(2, 100, 2, 50)
    assert not reserve(store, proposal("third"))["approved"]


def test_explicit_denial_is_journaled_without_reserving_or_becoming_reusable_approval(store: tuple[Path, str]) -> None:
    enroll(store)
    denied = reserve(store, approved=False)
    assert denied["status"] == "denied" and not denied["approved"]
    assert inspect(store)["reserved"] == units(0, 0, 0, 0)
    assert reserve(store, approved=False) == denied
    with pytest.raises(execution_store.StoreError, match="conflicts"):
        reserve(store, approved=True)
    with pytest.raises(execution_store.StoreError, match="approved retained"):
        authority.settle(
            store[0],
            store_id=store[1],
            objective_id=objective()["id"],
            proposal_id=proposal()["id"],
            outcome={"status": "succeeded", "receipt": {"native": "receipt"}},
        )


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_uncertainty_and_native_settlement_preserve_reservations_and_history(
    store: tuple[Path, str], status: str
) -> None:
    enroll(store)
    reserve(store)
    path, store_id = store
    arguments = {"store_id": store_id, "objective_id": objective()["id"], "proposal_id": proposal()["id"]}
    uncertain = authority.mark_uncertain(path, **arguments, reason="response lost")
    assert uncertain["status"] == "uncertain"
    assert authority.mark_uncertain(path, **arguments, reason="response lost") == uncertain
    assert reserve(store) == uncertain
    outcome = {"status": status, "receipt": {"component": "executor", "receipt_id": planning.digest("receipt")}}
    settled = authority.settle(path, **arguments, outcome=outcome)
    assert settled["status"] == "settled" and settled["uncertainty"] == {"reason": "response lost"}
    assert authority.settle(path, **arguments, outcome=outcome) == settled
    assert reserve(store) == settled
    assert inspect(store)["reserved"] == proposal()["cost"]
    with pytest.raises(execution_store.StoreError, match="conflicts"):
        authority.settle(path, **arguments, outcome={"status": "failed", "receipt": {"different": True}})
    with pytest.raises(execution_store.StoreError, match="conflicts"):
        authority.mark_uncertain(path, **arguments, reason="new ambiguity")


def test_settled_decision_cannot_become_uncertain(store: tuple[Path, str]) -> None:
    enroll(store)
    reserve(store)
    path, store_id = store
    arguments = {"store_id": store_id, "objective_id": objective()["id"], "proposal_id": proposal()["id"]}
    authority.settle(path, **arguments, outcome={"status": "cancelled", "receipt": {"cancelled": True}})
    with pytest.raises(execution_store.StoreError, match="settled"):
        authority.mark_uncertain(path, **arguments, reason="new ambiguity")


@pytest.mark.parametrize("invalid", [True, -1, 0.5, float("inf"), float("nan"), 2**63, "1"])
def test_budget_units_reject_booleans_nonintegers_and_nonfinite_or_unbounded_values(
    store: tuple[Path, str], invalid: Any
) -> None:
    limits = units()
    limits["attempts"] = invalid
    with pytest.raises(execution_store.StoreError):
        enroll(store, limits)
    with pytest.raises(execution_store.StoreError, match="unknown accounting objective"):
        inspect(store)


@pytest.mark.parametrize("change", ["extra", "version", "missing", "invalid_authorization", "unknown_decision"])
def test_closed_proposal_and_explicit_decision_validation(store: tuple[Path, str], change: str) -> None:
    enroll(store)
    value = proposal()
    decision: Any = True
    if change == "extra":
        value["allow_anything"] = True
    elif change == "version":
        value["schema_version"] = "999"
    elif change == "missing":
        value.pop("preconditions")
    elif change == "invalid_authorization":
        value["authorization"] = []
    else:
        decision = "allow"
    with pytest.raises(execution_store.StoreError):
        authority.reserve(
            store[0],
            store_id=store[1],
            objective_id=objective()["id"],
            proposal=value,
            approved=decision,
            reason="reviewed",
        )
    assert inspect(store)["decisions"] == []


@pytest.mark.parametrize("status", ["unknown", "success", None, True])
def test_unknown_native_outcomes_cannot_establish_settlement(store: tuple[Path, str], status: Any) -> None:
    enroll(store)
    reserve(store)
    with pytest.raises(execution_store.StoreError, match="unknown native outcome"):
        authority.settle(
            store[0],
            store_id=store[1],
            objective_id=objective()["id"],
            proposal_id=proposal()["id"],
            outcome={"status": status, "receipt": {"native": "receipt"}},
        )
    assert inspect(store)["decisions"][0]["status"] == "reserved"


def test_wrong_or_replaced_store_identity_cannot_replenish_budget(store: tuple[Path, str]) -> None:
    enroll(store)
    reserve(store)
    path, store_id = store
    replacement = path.with_name("replacement.sqlite3")
    new_id = authority.initialize(replacement)["store_id"]
    with pytest.raises(execution_store.StoreError, match="trusted pinned"):
        authority.enroll(replacement, store_id=store_id, objective=objective(), limits=units())
    os.replace(replacement, path)
    with pytest.raises(execution_store.StoreError, match="trusted pinned"):
        reserve(store)
    assert new_id != store_id


@pytest.mark.parametrize("damage", ["schema", "version", "application", "objective", "decision", "outcome", "identity"])
def test_unrecognized_schema_or_changed_history_fails_closed(store: tuple[Path, str], damage: str) -> None:
    enroll(store)
    reserve(store)
    authority.settle(
        store[0],
        store_id=store[1],
        objective_id=objective()["id"],
        proposal_id=proposal()["id"],
        outcome={"status": "succeeded", "receipt": {"native": "receipt"}},
    )
    with sqlite3.connect(store[0]) as connection:
        if damage == "schema":
            connection.execute("CREATE TABLE extra (value TEXT)")
        elif damage in ("version", "application"):
            pragma = "user_version" if damage == "version" else "application_id"
            connection.execute(f"PRAGMA {pragma}=999")
        elif damage == "identity":
            connection.execute("UPDATE authority_store SET value='not-a-uuid'")
        elif damage == "objective":
            connection.execute("UPDATE objectives SET digest=?", (planning.digest("changed"),))
        elif damage == "decision":
            connection.execute("UPDATE decisions SET document_json=?", (json.dumps({"changed": True}),))
        else:
            connection.execute("UPDATE outcomes SET digest=?", (planning.digest("changed"),))
    before = store[0].read_bytes()
    with pytest.raises(execution_store.StoreError):
        inspect(store)
    with pytest.raises(execution_store.StoreError):
        reserve(store, proposal("new"))
    assert store[0].read_bytes() == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_sidecars_never_trigger_implicit_recovery(store: tuple[Path, str], suffix: str) -> None:
    enroll(store)
    sidecar = Path(str(store[0]) + suffix)
    sidecar.write_bytes(b"unresolved transaction")
    sidecar.chmod(0o600)
    before = {item.name: item.read_bytes() for item in store[0].parent.iterdir()}
    with pytest.raises(execution_store.StoreError, match="sidecars"):
        reserve(store)
    assert {item.name: item.read_bytes() for item in store[0].parent.iterdir()} == before


def test_symlink_and_unsafe_permissions_are_rejected(store: tuple[Path, str]) -> None:
    path, store_id = store
    link = path.with_name("link.sqlite3")
    link.symlink_to(path)
    with pytest.raises(execution_store.StoreError):
        authority.enroll(link, store_id=store_id, objective=objective(), limits=units())
    path.chmod(0o644)
    with pytest.raises(execution_store.StoreError, match="0600"):
        enroll(store)
    path.chmod(0o600)
    path.parent.chmod(0o755)
    with pytest.raises(execution_store.StoreError, match="0700"):
        enroll(store)
