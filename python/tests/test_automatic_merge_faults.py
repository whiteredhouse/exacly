"""Faults in concrete merge coordination retain uncertainty and never retry the provider write."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, HEAD, MERGE, PREFIX, TOKEN, MergeCase
from automatic_merge_fixtures import merge_case as merge_case
from exacly import delivery_automation as delivery


def test_lost_atomic_ack_is_discovered_without_another_send(merge_case: MergeCase) -> None:
    def lost(provider: Any) -> None:
        raise URLError("untrusted transport text " + TOKEN)

    merge_case.opener.after_update = lost
    result = merge_case.invoke()
    assert result["outcome"] == "uncertain" and merge_case.opener.refs[BASE_REF] == MERGE
    assert TOKEN not in json.dumps(result)
    writes = copy.deepcopy(merge_case.opener.writes)
    merge_case.opener.after_update = None
    recovered = merge_case.reconcile()
    assert recovered["outcome"] == "applied" and recovered["effect"]["commit"] == MERGE
    assert merge_case.opener.writes == writes
    merge_case.adapter.validate_result("reconcile", merge_case.request, merge_case.payload, recovered)


def test_lost_marker_preparation_never_rolls_forward_from_absence(merge_case: MergeCase) -> None:
    original = merge_case.opener.respond

    def lost(method: str, path: str, body: Any) -> Any:
        value = original(method, path, body)
        if method == "POST" and path == PREFIX + "/git/tags":
            raise URLError("lost preparation acknowledgement")
        return value

    merge_case.opener.respond = lost  # type: ignore[method-assign]
    result = merge_case.invoke()
    assert result["outcome"] == "uncertain" and len(merge_case.opener.tags) == 1
    assert merge_case.opener.refs[BASE_REF] == BASE
    writes = len(merge_case.opener.writes)
    assert merge_case.reconcile()["outcome"] == "uncertain"
    assert len(merge_case.opener.writes) == writes == 1


@pytest.mark.parametrize("drift", ["protected-source", "policy", "config", "key", "expiry"])
def test_drift_after_object_preparation_prevents_ref_publication(merge_case: MergeCase, drift: str) -> None:
    changed = False

    def alter(provider: Any, method: str, path: str, body: Any) -> None:
        nonlocal changed
        if changed or method != "POST" or path != PREFIX + "/git/tags":
            return
        changed = True
        if drift == "protected-source":
            provider.trees[HEAD][0]["sha"] = "f" * 40
        elif drift == "policy":
            provider.protection["required_pull_request_reviews"]["require_last_push_approval"] = False
        elif drift == "config":
            merge_case.adapter.configuration["actor"]["app_id"] += 1
        elif drift == "key":
            Path(merge_case.configuration["key"]["path"]).unlink()
        else:
            merge_case.authorization["expires_at"] = "2021-01-01T00:00:00Z"

    merge_case.opener.before_request = alter
    result = merge_case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert merge_case.opener.refs[BASE_REF] == BASE and len(merge_case.opener.writes) == 1


def test_missing_key_blocks_inspection_without_provider_reads(merge_case: MergeCase) -> None:
    Path(merge_case.configuration["key"]["path"]).unlink()
    assert merge_case.inspect()["ready"] is False
    assert merge_case.opener.calls == []


def test_raw_nested_provider_fields_cannot_enter_retained_provenance(merge_case: MergeCase) -> None:
    case = merge_case
    secret = "untrusted-provider-field-" + TOKEN
    case.opener.protection["diagnostics"] = secret
    case.opener.protection["required_pull_request_reviews"]["diagnostics"] = secret
    case.opener.protection["required_status_checks"]["checks"][0]["diagnostics"] = secret
    case.opener.graph["diagnostics"] = secret
    result = case.inspect()
    assert result["ready"] is True and secret not in json.dumps(result)
    case.adapter.validate_result("inspect", case.request, case.payload, result)


@pytest.mark.parametrize("field", ["protection", "review", "checks", "repository", "commit", "refs", "contract"])
def test_native_validator_rejects_unallowlisted_nested_proof_fields(merge_case: MergeCase, field: str) -> None:
    case = merge_case
    result = case.inspect()
    assert result["ready"] is True
    facts = result["provenance"]["facts"]
    target = {
        "protection": facts["protection"],
        "review": facts["review"],
        "checks": facts["check_runs"][0],
        "repository": facts["repository"],
        "commit": facts["merge_commit"],
        "refs": facts["refs"],
        "contract": facts["contract_files"]["criteria"][0],
    }[field]
    target["arbitrary_response_field"] = "never retain this provider body"
    with pytest.raises(ValueError):
        case.adapter.validate_result("inspect", case.request, case.payload, result)


def test_approval_from_another_policy_cannot_invoke_provider(merge_case: MergeCase) -> None:
    merge_case.authorization["policy_digest"] = delivery.digest("a different policy")
    with pytest.raises(ValueError):
        merge_case.invoke()
    assert merge_case.opener.calls == []


def test_applied_result_is_tied_to_exact_native_commit_and_provenance(merge_case: MergeCase) -> None:
    result = merge_case.invoke()
    assert result["outcome"] == "applied"
    before = copy.deepcopy(result)
    assert merge_case.adapter.validate_result("invoke", merge_case.request, merge_case.payload, result) == result
    assert result == before
    result["effect"]["commit"] = "f" * 40
    with pytest.raises(ValueError, match="actual native result"):
        merge_case.adapter.validate_result("invoke", merge_case.request, merge_case.payload, result)


def test_ready_flag_cannot_be_attached_to_blocked_provenance(merge_case: MergeCase) -> None:
    merge_case.opener.graph["reviewDecision"] = "REVIEW_REQUIRED"
    blocked = merge_case.inspect()
    assert blocked["ready"] is False
    blocked["ready"] = True
    with pytest.raises(ValueError):
        merge_case.adapter.validate_result("inspect", merge_case.request, merge_case.payload, blocked)


def test_negative_preflight_facts_remain_a_valid_rejection(merge_case: MergeCase) -> None:
    merge_case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    result = merge_case.invoke()
    assert result["outcome"] == "rejected" and not merge_case.opener.writes
    merge_case.adapter.validate_result("invoke", merge_case.request, merge_case.payload, result)


def test_replayable_receipt_does_not_depend_on_later_remote_branch_position(merge_case: MergeCase) -> None:
    result = merge_case.invoke()
    assert result["outcome"] == "applied"
    retained = copy.deepcopy(result)
    merge_case.opener.refs[BASE_REF] = "f" * 40
    assert merge_case.adapter.validate_result("invoke", merge_case.request, merge_case.payload, retained) == retained
    assert merge_case.reconcile()["outcome"] == "applied"


@pytest.mark.parametrize("field", ["marker_ref", "head_revision", "merge_revision", "tree"])
def test_corrupt_retained_receipt_cannot_establish_merged_effect(merge_case: MergeCase, field: str) -> None:
    result = merge_case.invoke()
    assert result["outcome"] == "applied"
    facts = result["provenance"]["facts"]
    if field == "marker_ref":
        facts["marker_ref"] = "f" * 40
    elif field == "tree":
        facts["merge_commit"]["tree"] = "f" * 40
    else:
        facts["pull_request"][field] = "f" * 40
    with pytest.raises(ValueError):
        merge_case.adapter.validate_result("invoke", merge_case.request, merge_case.payload, result)
