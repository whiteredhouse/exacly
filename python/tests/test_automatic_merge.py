"""The exact merge profile retains delivery boundaries and never treats missing facts as approval."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from automatic_merge_fixtures import BASE, HEAD, MERGE, MergeCase
from automatic_merge_fixtures import merge_case as merge_case
from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import execution_store as store
from test_delivery_contracts import identify, receipt


def test_exact_head_and_base_need_separate_merge_authority(merge_case: MergeCase) -> None:
    case = merge_case
    assert delivery.validate_intent(case.intent, approval=case.authorization) == case.intent
    for key in ("head_revision", "base_revision"):
        changed = copy.deepcopy(case.request)
        changed["subject"][key] = "f" * 40
        assert case.adapter.inspect(changed, case.payload, deadline=store.Deadline(10))["ready"] is False
    wrong = copy.deepcopy(case.authorization)
    wrong["stage"] = "plan"
    with pytest.raises(ValueError):
        case.adapter.invoke(case.intent, case.payload, wrong, deadline=store.Deadline(10))
    assert case.opener.writes == []


def test_required_checks_reviews_and_protection_cannot_be_bypassed(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    assert case.inspect()["ready"] is False
    case.opener.graph["reviewDecision"] = "APPROVED"
    case.opener.check_runs[0]["conclusion"] = "failure"
    assert case.inspect()["ready"] is False
    case.opener.check_runs[0]["conclusion"] = "success"
    case.opener.protection["enforce_admins"]["enabled"] = False
    assert case.inspect()["ready"] is False
    assert case.opener.writes == []


def test_uncertain_merge_is_reconciled_before_retry(merge_case: MergeCase) -> None:
    case = merge_case

    def lost_acknowledgement(provider: Any) -> None:
        raise URLError("controlled lost acknowledgement")

    case.opener.after_update = lost_acknowledgement
    outcome = case.invoke()
    assert outcome["outcome"] == "uncertain" and outcome["effect"] is None
    assert case.opener.refs[case.request["subject"]["base_ref"]] == MERGE
    assert len(case.opener.writes) == 2
    recovered = case.reconcile()
    assert recovered["outcome"] == "applied" and recovered["effect"]["commit"] == MERGE
    assert len(case.opener.writes) == 2
    assert case.reconcile() == recovered


def test_merged_commit_requires_fresh_observations(merge_case: MergeCase) -> None:
    case = merge_case
    outcome = case.invoke()
    assert outcome["outcome"] == "applied" and outcome["effect"]["commit"] == MERGE
    retained = receipt(case.intent)
    retained.update(effect=outcome["effect"], provenance_digest=delivery.digest(outcome["provenance"]))
    identify(retained)
    assert delivery.validate_receipt(retained, intent=case.intent) == retained
    assert retained["claims"] == {
        "implementation_verified": False,
        "operational_health_established": False,
        "later_stage_authorized": False,
        "fresh_observation": False,
        "self_authenticating": False,
    }


def test_inspection_is_read_only_and_exact_success_records_three_conditional_refs(merge_case: MergeCase) -> None:
    case = merge_case
    before_request, before_payload = copy.deepcopy(case.request), copy.deepcopy(case.payload)
    before_config, before_policy = copy.deepcopy(case.configuration), copy.deepcopy(case.policy)
    ready = case.inspect()
    assert ready["ready"] is True and case.opener.writes == []
    assert case.adapter.validate_provenance("inspect", case.request, case.payload, ready["provenance"])
    outcome = case.invoke()
    assert outcome["outcome"] == "applied"
    assert len(case.opener.mutations) == 1 and len(case.opener.writes) == 2
    changes = case.opener.mutations[0]["refUpdates"]
    assert changes[:2] == [
        {"name": case.request["subject"]["base_ref"], "beforeOid": BASE, "afterOid": MERGE, "force": False},
        {"name": case.payload["head_ref"], "beforeOid": HEAD, "afterOid": HEAD, "force": False},
    ]
    assert changes[2]["beforeOid"] == merge.ZERO and changes[2]["name"] == case.payload["marker_ref"]
    assert all(change["force"] is False for change in changes)
    assert case.request == before_request and case.payload == before_payload
    assert case.configuration == before_config and case.policy == before_policy


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_unsupported_merge_method_is_not_replaced_with_rest_fallback(merge_case: MergeCase, method: str) -> None:
    case = merge_case
    case.request["subject"]["method"] = method
    with pytest.raises(ValueError, match="squash and rebase"):
        case.adapter.validate_payload(case.request, case.payload)
    assert case.opener.calls == []


def test_fork_repository_has_no_same_repository_atomic_profile(merge_case: MergeCase) -> None:
    case = merge_case
    case.request["subject"]["head_repository"] = {"id": 456, "full_name": "other/product"}
    with pytest.raises(ValueError, match="fork"):
        case.adapter.validate_payload(case.request, case.payload)
    assert case.opener.calls == []


@pytest.mark.parametrize(
    "damage",
    ["base_ref", "head_ref", "same_ref", "same_merge", "marker", "inventory", "path", "digest", "extra"],
)
def test_native_payload_scope_changes_are_rejected(merge_case: MergeCase, damage: str) -> None:
    case = merge_case
    if damage == "base_ref":
        case.request["subject"]["base_ref"] = "refs/heads/other"
    elif damage == "head_ref":
        case.payload["head_ref"] = "refs/heads/unsafe..ref"
    elif damage == "same_ref":
        case.payload["head_ref"] = case.request["subject"]["base_ref"]
    elif damage == "same_merge":
        case.payload["merge_revision"] = HEAD
    elif damage == "marker":
        case.payload["marker_ref"] = "refs/tags/release-v1"
    elif damage in ("inventory", "path"):
        entry = case.payload["contract_files"]["criteria"][0]
        entry["after" if damage == "inventory" else "path"] = "f" * 40 if damage == "inventory" else "../criteria.yaml"
    elif damage == "digest":
        case.request["native_payload_digest"] = delivery.digest("another payload")
    else:
        case.payload["candidate_adapter"] = "unsafe executable"
    with pytest.raises(ValueError):
        case.adapter.validate_payload(case.request, case.payload)
    assert case.opener.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("isMergeQueueEnabled", True), ("isInMergeQueue", True), ("isCrossRepository", True), ("mergeable", "UNKNOWN")],
)
def test_unsupported_provider_path_or_unknown_mergeability_blocks(
    merge_case: MergeCase, field: str, value: Any
) -> None:
    case = merge_case
    case.opener.graph[field] = value
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == []


@pytest.mark.parametrize("field", ["admin", "maintain", "push"])
def test_broad_or_unknown_actor_permissions_do_not_release_merge(merge_case: MergeCase, field: str) -> None:
    case = merge_case
    case.opener.repository["permissions"][field] = field != "push"
    assert case.inspect()["ready"] is False and case.opener.writes == []


def test_rulesets_or_unapproved_contract_changes_are_not_silently_accepted(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.rules = [{"type": "required_merge_queue"}]
    assert case.inspect()["ready"] is False
    case.opener.rules = []
    case.opener.trees[HEAD][0]["sha"] = "f" * 40
    assert case.inspect()["ready"] is False
    assert case.opener.writes == []


def test_policy_configuration_is_recomputed_and_cannot_echo_old_approval(merge_case: MergeCase) -> None:
    case = merge_case
    approved = case.adapter.configuration_digest
    case.adapter.configuration["actor"]["identity_approval_digest"] = delivery.digest("changed operator actor")
    assert case.adapter.configuration_digest != approved
    with pytest.raises(ValueError):
        case.adapter.invoke(case.intent, case.payload, case.authorization, deadline=store.Deadline(10))
    assert case.opener.writes == []


def test_marker_is_signed_for_the_exact_delivery_request_and_key_purpose(merge_case: MergeCase) -> None:
    case = merge_case
    before = merge.marker(case.request, case.payload, case.configuration, case.policy, lock_timeout=10)
    assert before["ref"] == case.payload["marker_ref"]
    assert before["scope"]["stage"] == "merge" and before["scope"]["key_id"] == case.configuration["key"]["id"]
    case.request["operation_key"] = delivery.digest("another exact operation")
    case.payload["marker_ref"] = "refs/tags/exacly-delivery-operations/" + case.request["operation_key"].split(":")[1]
    case.rebind()
    after = merge.marker(case.request, case.payload, case.configuration, case.policy, lock_timeout=10)
    assert before["object"] != after["object"] and before["scope"]["proof"] != after["scope"]["proof"]


def test_native_result_validator_rejects_a_forged_ready_flag_over_blocked_facts(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    result = case.inspect()
    assert case.adapter.validate_result("inspect", case.request, case.payload, result) == result
    result["ready"] = True
    with pytest.raises(ValueError):
        case.adapter.validate_result("inspect", case.request, case.payload, result)


@pytest.mark.parametrize("damage", ["effect_commit", "marker_flag", "commit_parent", "policy_subject"])
def test_actual_effect_must_match_retained_native_proof(merge_case: MergeCase, damage: str) -> None:
    case = merge_case
    result = case.invoke()
    assert result["outcome"] == "applied"
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result
    if damage == "effect_commit":
        result["effect"]["commit"] = "f" * 40
    elif damage == "marker_flag":
        result["provenance"]["facts"]["marker_verified"] = False
    elif damage == "commit_parent":
        result["provenance"]["facts"]["merge_commit"]["parents"].reverse()
    else:
        result["provenance"]["request_digest"] = delivery.digest("another approved request")
    with pytest.raises(ValueError):
        case.adapter.validate_result("invoke", case.request, case.payload, result)


def test_historical_native_result_validation_needs_no_new_key_or_provider_read(merge_case: MergeCase) -> None:
    case = merge_case
    result = case.invoke()
    assert result["outcome"] == "applied"
    calls = copy.deepcopy(case.opener.calls)
    Path(case.configuration["key"]["path"]).unlink()
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result
    assert case.opener.calls == calls
