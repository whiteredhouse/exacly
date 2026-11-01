"""Actual private journal and bounded native calls; the provider is deliberately controlled."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest
from delivery_engine_fixtures import CAPS, Case, make_case, read
from exacly import delivery_automation as contracts
from exacly import delivery_store
from exacly.delivery_engine import DeliveryEngine
from test_delivery_contracts import pin


def recovery_approval(case: Case) -> dict[str, Any]:
    saved = delivery_store.show(case.journal, case.journal_id, case.request["operation_key"])["operation"]
    intent = saved["intent"]
    return {
        "schema_version": "0.1.0",
        "profile": contracts.PROFILE,
        "kind": "delivery-read-recovery",
        "issuer": "separate operator review",
        "approval_id": "read-discovery-only-2",
        "objective": copy.deepcopy(case.request["objective"]),
        "journal": copy.deepcopy(intent["journal"]),
        "operation_key": case.request["operation_key"],
        "intent_id": intent["id"],
        "request_digest": intent["request_digest"],
        "native_payload_digest": case.request["native_payload_digest"],
        "old_configuration_digest": intent["adapter"]["configuration_digest"],
        "new_configuration_digest": case.adapter.configuration_digest,
        "operation_profile_digest": case.adapter.operation_profile_digest,
        "policy_digest": case.approval["policy_digest"],
        "contract": copy.deepcopy(case.approval["contract"]),
        "limits": copy.deepcopy(case.approval["limits"]),
        "read_limits": case.adapter.call_limits["reconcile"],
        "not_before": case.approval["not_before"],
        "expires_at": case.approval["expires_at"],
    }


def test_exact_apply_retains_actual_effect_and_offline_replay_precedes_factory(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    result = case.apply()
    assert result["status"] == "applied", result
    assert result["historical"] is False and result["live_checked"] is True
    assert read(case.remote)["writes"] == 1
    before = read(case.remote)

    def forbidden() -> Any:
        pytest.fail("offline historical replay loaded policy, credentials or adapter")

    replay = DeliveryEngine(case.journal, journal_id=case.journal_id, adapter_factory=forbidden)
    actual = replay.apply(
        case.request, case.payload, approval={}, pin=contracts.ApprovalPin("missing", "missing", "missing")
    )
    assert actual["status"] == "applied" and actual["historical"] is True
    assert actual["receipt"] == result["receipt"]
    assert not any(actual["claims"].values())
    assert read(case.remote) == before


def test_preview_is_local_and_needs_separate_current_authority(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    before = case.journal.read_bytes()
    result = case.engine.preview(case.request, case.payload)
    assert result["status"] == "approval_required" and result["live_checked"] is False
    assert result["effect_authorized"] is False
    assert case.journal.read_bytes() == before
    assert read(case.remote)["reads"] == read(case.remote)["writes"] == 0
    approved = case.engine.preview(case.request, case.payload, approval=case.approval, pin=pin(case.approval))
    assert approved["status"] == "preview" and approved["packet"]["intent"]
    assert case.journal.read_bytes() == before


@pytest.mark.parametrize("mode", ["before", "after"])
def test_ambiguous_effect_never_resends_on_old_or_absent_remote_state(tmp_path: Path, mode: str) -> None:
    case = make_case(tmp_path, mode=mode)
    result = case.apply()
    assert result["status"] == "uncertain", result
    original_writes = read(case.remote)["writes"]
    for _ in range(2):
        assert case.apply()["status"] == "uncertain"
    observed = case.discover()
    assert observed["status"] == ("applied" if mode == "after" else "uncertain"), observed
    assert read(case.remote)["writes"] == original_writes
    if mode == "before":
        assert case.discover()["status"] == "uncertain"
        assert read(case.remote)["writes"] == 0
    assert "private token" not in json.dumps(delivery_store.show(case.journal, case.journal_id))
    assert "secret transport" not in json.dumps(delivery_store.show(case.journal, case.journal_id))


def test_ready_claim_cannot_substitute_a_different_merge_commit(tmp_path: Path) -> None:
    case = make_case(tmp_path, mode="wrong_commit")
    result = case.apply()
    assert result["status"] == "uncertain", result
    assert result["receipt"]["effect"]["commit"] == "f" * 40
    assert read(case.remote)["writes"] == 1
    assert case.apply()["status"] == "uncertain"
    assert read(case.remote)["writes"] == 1


def test_config_or_policy_mismatch_blocks_before_provider_access(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    case.approval["policy_digest"] = contracts.digest("different policy")
    with pytest.raises(ValueError):
        case.apply()
    assert read(case.remote)["reads"] == read(case.remote)["writes"] == 0
    assert not delivery_store.show(case.journal, case.journal_id)["operations"]


def test_ready_claim_must_follow_actual_allowlisted_native_proof(tmp_path: Path) -> None:
    case = make_case(tmp_path, mode="ready_lie")
    result = case.apply()
    assert result["status"] == "prepared"
    assert read(case.remote)["writes"] == 0
    saved = delivery_store.show(case.journal, case.journal_id, case.request["operation_key"])["operation"]
    assert [call["method"] for call in saved["calls"]] == ["inspect"]


@pytest.mark.parametrize("mode", ["effect_lie", "secret_proof", "wrong_subject"])
def test_corrupt_post_effect_proof_stays_uncertain_and_cannot_leak_raw_native_data(tmp_path: Path, mode: str) -> None:
    case = make_case(tmp_path, mode=mode)
    result = case.apply()
    assert result["status"] == "uncertain"
    assert read(case.remote)["writes"] == 1
    assert case.apply()["status"] == "uncertain"
    history = delivery_store.show(case.journal, case.journal_id)
    assert "do-not-retain-private-provider-credential" not in json.dumps(history)
    assert "unexpected_authorization" not in json.dumps(history)
    assert history["operations"][0]["calls"][-1]["result"]["provenance"]["kind"] == "delivery-engine-boundary-v1"


def test_real_watchdog_stops_blocked_native_work_and_preserves_full_reservation(tmp_path: Path) -> None:
    caps = copy.deepcopy(CAPS)
    caps["invoke"]["wall_time_ms"] = 1000
    case = make_case(tmp_path, mode="hang", caps=caps)
    started = time.monotonic()
    result = case.apply()
    assert time.monotonic() - started < 5
    assert result["status"] == "uncertain", result
    assert read(case.remote).get("started") is True and read(case.remote)["writes"] == 0
    assert case.apply()["status"] == "uncertain"
    history = delivery_store.show(case.journal, case.journal_id)
    assert history["reserved"]["operations"] == 1
    assert history["reserved"]["wall_time_ms"] == 4000
    time.sleep(0.1)
    assert read(case.remote)["writes"] == 0


def test_rotated_credential_requires_a_distinct_read_only_grant_and_keeps_original_receipt_lineage(
    tmp_path: Path,
) -> None:
    case = make_case(tmp_path, mode="after")
    uncertain = case.apply()
    assert uncertain["status"] == "uncertain"
    old = uncertain["intent"]
    case.adapter.credential_descriptor = "independently reviewed renewed installation token"
    with pytest.raises(ValueError):
        case.discover()
    approved = recovery_approval(case)
    with pytest.raises(ValueError, match="read-only recovery"):
        case.engine.apply(case.request, case.payload, approval=approved, pin=pin(approved))
    result = case.engine.reconcile(case.request["operation_key"], approval=approved, pin=pin(approved))
    assert result["status"] == "applied", result
    assert result["receipt"]["authority_digest"] == old["authority_digest"]
    assert result["receipt"]["adapter"] == old["adapter"]
    assert read(case.remote)["writes"] == 1
    saved = delivery_store.show(case.journal, case.journal_id, case.request["operation_key"])["operation"]
    assert saved["calls"][-1]["approval"]["kind"] == "delivery-read-recovery"
    assert saved["calls"][-1]["bounds"]["write_calls"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "intent_id",
        "request_digest",
        "old_configuration_digest",
        "new_configuration_digest",
        "policy_digest",
        "operation_profile_digest",
    ],
)
def test_recovery_grant_cannot_change_pinned_identity(tmp_path: Path, field: str) -> None:
    case = make_case(tmp_path, mode="after")
    assert case.apply()["status"] == "uncertain"
    case.adapter.credential_descriptor = "renewed token"
    approved = recovery_approval(case)
    approved[field] = contracts.digest("wrong identity")
    before = read(case.remote)
    with pytest.raises(ValueError):
        case.engine.reconcile(case.request["operation_key"], approval=approved, pin=pin(approved))
    assert read(case.remote) == before


@pytest.mark.parametrize("change", ["code", "key", "caps", "adapter"])
def test_recovery_cannot_migrate_runtime_key_or_profile_limits(tmp_path: Path, change: str) -> None:
    case = make_case(tmp_path, mode="after")
    assert case.apply()["status"] == "uncertain"
    case.adapter.credential_descriptor = "renewed token"
    if change == "code":
        case.adapter.config = "a different installed runtime or policy"
    elif change == "key":
        case.adapter.key_id = "a different signing key"
    elif change == "caps":
        case.adapter._caps["reconcile"]["read_calls"] += 1
    else:
        case.adapter.adapter_id = "another-installed-stage-adapter-v1"
    approved = recovery_approval(case)
    before = read(case.remote)
    with pytest.raises(ValueError):
        case.engine.reconcile(case.request["operation_key"], approval=approved, pin=pin(approved))
    assert read(case.remote) == before
