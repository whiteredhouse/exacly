"""Explicit required-policy evidence replaces only GitHub's coarse UNSTABLE verdict."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, HEAD, MERGE, PREFIX, TOKEN, TOKEN_ENVIRONMENT, MergeCase
from automatic_merge_fixtures import merge_case as merge_case
from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import delivery_store, executor_runtime
from exacly import github_merge as github
from exacly.delivery_engine import DeliveryEngine
from exacly.execution_store import Deadline
from test_delivery_contracts import identify
from test_delivery_contracts import intent as common_intent

Doc = dict[str, Any]
NOW = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
STATUS_PATHS = tuple(PREFIX + "/commits/" + revision + "/status?per_page=100" for revision in (HEAD, MERGE))
COMPARE_PATH = PREFIX + "/compare/" + BASE + "..." + HEAD + "?per_page=100"


def approve_grant(case: MergeCase, monkeypatch: pytest.MonkeyPatch, *, version: str = "0.2.0") -> None:
    permissions = dict(merge.INSTALLATION_PERMISSIONS)
    if version == "0.2.0":
        permissions["statuses"] = "read"
    case.configuration["actor"].update(
        credential_kind=merge.INSTALLATION_GRANT_KIND,
        grant={
            "schema_version": version,
            "repository_ids": [123],
            "permissions": permissions,
            "not_before": (NOW - dt.timedelta(minutes=1)).isoformat(),
            "not_after": (NOW + dt.timedelta(minutes=9)).isoformat(),
            "mint_provenance_digest": delivery.digest("separately reviewed status-read mint"),
        },
    )
    case.opener.repository["permissions"]["push"] = False
    monkeypatch.setattr(github, "_credential_now", lambda: NOW)
    case.rebind()


@pytest.fixture
def unstable_case(merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch) -> MergeCase:
    approve_grant(merge_case, monkeypatch)
    merge_case.opener.graph["mergeStateStatus"] = "UNSTABLE"
    return merge_case


def add_server_requirement(case: MergeCase) -> Doc:
    case.opener.protection["required_status_checks"]["checks"].append(
        {"context": "additional-server-check", "app_id": 987}
    )
    case.policy["protection_digest"] = delivery.digest(merge.protection_snapshot(case.opener.protection))
    case.rebind()
    result = {
        "name": "additional-server-check",
        "app": {"id": 987},
        "head_sha": MERGE,
        "status": "completed",
        "conclusion": "success",
    }
    case.opener.check_runs.append(result)
    return result


def corrupt_conditions(case: MergeCase, change: Callable[[Doc], None]) -> None:
    original = case.opener.respond

    def respond(method: str, path: str, body: Any) -> Any:
        result = original(method, path, body)
        if method == "POST" and path == "/graphql" and body["query"] == github.UNSTABLE_QUERY:
            change(result)
        return result

    case.opener.respond = respond  # type: ignore[method-assign]


def assert_no_effect(case: MergeCase) -> None:
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == [] and case.opener.mutations == []
    assert case.opener.refs[BASE_REF] == BASE


def test_unstable_uses_full_current_evidence_and_real_required_policy(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.opener.check_runs.append(
        {
            "name": "optional-quality",
            "app": {"id": 999},
            "head_sha": MERGE,
            "status": "completed",
            "conclusion": "failure",
        }
    )
    ready = case.inspect()
    assert ready["ready"] is True and case.opener.writes == []
    facts = ready["provenance"]["facts"]
    assert facts["repository"]["push"] is False
    assert facts["review"]["mergeStateStatus"] == "UNSTABLE"
    assert facts["legacy_statuses"] == {
        "head": {"sha": HEAD, "total_count": 0, "statuses": []},
        "merge": {"sha": MERGE, "total_count": 0, "statuses": []},
    }
    assert facts["required_conditions"]["ancestry"]["merge_base_revision"] == BASE
    assert len(case.opener.calls) == 18
    assert case.adapter.validate_result("inspect", case.request, case.payload, ready) == ready
    case.opener.calls.clear()
    result = case.invoke()
    assert result["outcome"] == "applied" and result["effect"]["commit"] == MERGE
    assert len(case.opener.writes) == 2 and len(case.opener.mutations) == 1
    assert len(case.opener.calls) - len(case.opener.writes) == 40
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result


def test_unstable_checks_server_requirements_beyond_the_approved_subset(unstable_case: MergeCase) -> None:
    case = unstable_case
    add_server_requirement(case)
    assert len(case.policy["required_checks"]) == 1
    assert len(case.opener.protection["required_status_checks"]["checks"]) == 2
    assert case.inspect()["ready"] is True
    assert case.invoke()["outcome"] == "applied"


@pytest.mark.parametrize("damage", ["missing", "wrong-app", "wrong-sha", "duplicate", "pending", "neutral", "skipped"])
def test_unstable_cannot_hide_an_unselected_server_requirement(unstable_case: MergeCase, damage: str) -> None:
    case = unstable_case
    required = add_server_requirement(case)
    if damage == "missing":
        case.opener.check_runs.remove(required)
    elif damage == "wrong-app":
        required["app"]["id"] = 988
    elif damage == "wrong-sha":
        required["head_sha"] = HEAD
    elif damage == "duplicate":
        case.opener.check_runs.append(copy.deepcopy(required))
    elif damage == "pending":
        required.update(status="in_progress", conclusion=None)
    else:
        required["conclusion"] = damage
    assert_no_effect(case)


@pytest.mark.parametrize("state", ["BLOCKED", "BEHIND", "DIRTY", "UNKNOWN", "HAS_HOOKS", None])
def test_other_aggregate_states_still_block_with_successful_checks(unstable_case: MergeCase, state: Any) -> None:
    unstable_case.opener.graph["mergeStateStatus"] = state
    assert_no_effect(unstable_case)


@pytest.mark.parametrize("revision", [HEAD, MERGE])
@pytest.mark.parametrize("state", ["success", "pending", "failure"])
def test_same_name_legacy_status_is_not_satisfied_by_a_check_run(
    unstable_case: MergeCase, revision: str, state: str
) -> None:
    unstable_case.opener.legacy_statuses[revision].update(
        total_count=1, statuses=[{"context": "verify-exact-merge", "state": state}]
    )
    assert_no_effect(unstable_case)


def test_legacy_collision_covers_actual_server_policy_not_only_selected_subset(unstable_case: MergeCase) -> None:
    case = unstable_case
    add_server_requirement(case)
    case.opener.legacy_statuses[HEAD].update(
        total_count=1, statuses=[{"context": "additional-server-check", "state": "success"}]
    )
    assert_no_effect(case)


def test_required_legacy_name_collision_is_case_insensitive(unstable_case: MergeCase) -> None:
    unstable_case.opener.legacy_statuses[HEAD].update(
        total_count=1, statuses=[{"context": "VERIFY-EXACT-MERGE", "state": "success"}]
    )
    assert_no_effect(unstable_case)


def test_legacy_case_variants_are_not_two_independent_contexts(unstable_case: MergeCase) -> None:
    unstable_case.opener.legacy_statuses[HEAD].update(
        total_count=2,
        statuses=[{"context": name, "state": "success"} for name in ("Optional-Status", "optional-status")],
    )
    assert_no_effect(unstable_case)


def test_server_required_case_variants_are_ambiguous_even_with_distinct_apps(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.opener.protection["required_status_checks"]["checks"].append({"context": "VERIFY-EXACT-MERGE", "app_id": 987})
    case.opener.check_runs.append(
        {
            "name": "VERIFY-EXACT-MERGE",
            "app": {"id": 987},
            "head_sha": MERGE,
            "status": "completed",
            "conclusion": "success",
        }
    )
    case.policy["protection_digest"] = delivery.digest(merge.protection_snapshot(case.opener.protection))
    case.rebind()
    assert_no_effect(case)


def test_nonrequired_legacy_failure_does_not_become_a_required_check(unstable_case: MergeCase) -> None:
    unstable_case.opener.legacy_statuses[HEAD].update(
        total_count=1, statuses=[{"context": "optional-status", "state": "failure"}]
    )
    assert unstable_case.inspect()["ready"] is True
    assert unstable_case.invoke()["outcome"] == "applied"


@pytest.mark.parametrize("damage", ["sha", "count", "bool-count", "duplicate", "blank", "state", "missing", "ceiling"])
def test_legacy_inventory_must_be_complete_typed_and_bound(unstable_case: MergeCase, damage: str) -> None:
    inventory = unstable_case.opener.legacy_statuses[HEAD]
    if damage == "sha":
        inventory["sha"] = MERGE
    elif damage == "count":
        inventory["total_count"] = 1
    elif damage == "bool-count":
        inventory["total_count"] = False
    elif damage == "duplicate":
        inventory.update(total_count=2, statuses=[{"context": "optional", "state": "success"}] * 2)
    elif damage in ("blank", "state"):
        inventory.update(
            total_count=1,
            statuses=[
                {
                    "context": " " if damage == "blank" else "optional",
                    "state": "unknown" if damage == "state" else "success",
                }
            ],
        )
    elif damage == "missing":
        del inventory["sha"]
    else:
        inventory.update(
            total_count=100, statuses=[{"context": f"optional-{i}", "state": "success"} for i in range(100)]
        )
    assert_no_effect(unstable_case)


@pytest.mark.parametrize("damage", ["unresolved", "truncated", "count", "duplicate", "untyped", "ceiling"])
def test_required_conversations_need_complete_resolved_thread_evidence(unstable_case: MergeCase, damage: str) -> None:
    threads = unstable_case.opener.review_threads
    if damage == "unresolved":
        threads.update(totalCount=1, nodes=[{"id": "PRRT_one", "isResolved": False}])
    elif damage == "truncated":
        threads["pageInfo"]["hasNextPage"] = True
    elif damage == "count":
        threads["totalCount"] = 1
    elif damage == "duplicate":
        threads.update(totalCount=2, nodes=[{"id": "PRRT_same", "isResolved": True}] * 2)
    elif damage == "untyped":
        threads.update(totalCount=1, nodes=[{"id": "PRRT_one", "isResolved": "true"}])
    else:
        threads.update(totalCount=100, nodes=[{"id": f"PRRT_{i}", "isResolved": True} for i in range(100)])
    assert_no_effect(unstable_case)


def test_optional_unresolved_conversation_obeys_actual_pinned_policy(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.opener.protection["required_conversation_resolution"]["enabled"] = False
    case.opener.branch_policy["requiresConversationResolution"] = False
    case.opener.review_threads.update(totalCount=1, nodes=[{"id": "PRRT_one", "isResolved": False}])
    case.policy["protection_digest"] = delivery.digest(merge.protection_snapshot(case.opener.protection))
    case.rebind()
    assert case.inspect()["ready"] is True and case.invoke()["outcome"] == "applied"


@pytest.mark.parametrize("damage", ["deployment", "environment", "unknown", "restricted-push", "conversation-mismatch"])
def test_additional_policy_cannot_hide_unsupported_enforcement(unstable_case: MergeCase, damage: str) -> None:
    policy = unstable_case.opener.branch_policy
    if damage == "deployment":
        policy["requiresDeployments"] = True
    elif damage == "environment":
        policy["requiredDeploymentEnvironments"] = ["production"]
    elif damage == "unknown":
        policy["requiresDeployments"] = None
    elif damage == "restricted-push":
        policy["restrictsPushes"] = True
    else:
        policy["requiresConversationResolution"] = False
    assert_no_effect(unstable_case)


@pytest.mark.parametrize("damage", ["repo", "base-ref", "base-oid", "head", "merge", "missing-rule", "errors"])
def test_additional_query_facts_are_bound_to_the_actual_request(unstable_case: MergeCase, damage: str) -> None:
    def alter(value: Doc) -> None:
        repo = value["data"]["repository"]
        if damage == "repo":
            repo["databaseId"] += 1
        elif damage == "base-ref":
            repo["ref"]["name"] = "other"
        elif damage == "base-oid":
            repo["ref"]["target"]["oid"] = "f" * 40
        elif damage == "head":
            repo["pullRequest"]["headRefOid"] = "f" * 40
        elif damage == "merge":
            repo["pullRequest"]["potentialMergeCommit"]["oid"] = "f" * 40
        elif damage == "missing-rule":
            repo["ref"]["branchProtectionRule"] = None
        else:
            value["errors"] = [{"type": "FORBIDDEN", "message": "untrusted provider error " + TOKEN}]

    corrupt_conditions(unstable_case, alter)
    assert_no_effect(unstable_case)


@pytest.mark.parametrize("damage", ["behind", "base", "head", "count", "duplicate", "ceiling"])
def test_complete_compare_summary_proves_the_head_is_up_to_date(unstable_case: MergeCase, damage: str) -> None:
    comparison = unstable_case.opener.comparison
    if damage == "behind":
        comparison.update(status="diverged", behind_by=1)
        comparison["merge_base_commit"]["sha"] = "f" * 40
    elif damage == "base":
        comparison["base_commit"]["sha"] = "f" * 40
    elif damage == "head":
        comparison["commits"][-1]["sha"] = "f" * 40
    elif damage == "count":
        comparison["total_commits"] = 2
    elif damage == "duplicate":
        comparison.update(total_commits=2, ahead_by=2, commits=[{"sha": HEAD}] * 2)
    else:
        comparison.update(total_commits=100, ahead_by=100, commits=[{"sha": f"{i:040x}"} for i in range(100)])
    assert "head_commit" not in comparison  # The real GitHub response has no such field.
    assert_no_effect(unstable_case)


@pytest.mark.parametrize("read_limit", [32, 36, 39])
def test_insufficient_unstable_invoke_capacity_blocks_before_marker_preparation(
    unstable_case: MergeCase, read_limit: int
) -> None:
    case = unstable_case
    case.configuration["call_limits"]["invoke"]["read_calls"] = read_limit
    case.rebind()
    assert case.invoke()["outcome"] == "rejected"
    assert case.configuration["call_limits"]["invoke"]["read_calls"] == read_limit
    assert case.opener.writes == [] and case.opener.tags == {}


def test_exact_unstable_capacity_runs_the_whole_protocol(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.configuration["call_limits"]["inspect"]["read_calls"] = 18
    case.configuration["call_limits"]["invoke"]["read_calls"] = 40
    case.rebind()
    assert case.inspect()["ready"] is True and len(case.opener.calls) == 18
    case.opener.calls.clear()
    assert case.invoke()["outcome"] == "applied"
    assert len(case.opener.calls) - len(case.opener.writes) == 40


def test_small_inspection_cap_stops_before_an_extra_http_request(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.configuration["call_limits"]["inspect"]["read_calls"] = 17
    case.rebind()
    assert case.inspect()["ready"] is False
    assert len(case.opener.calls) == 17 and case.opener.writes == []


@pytest.mark.parametrize("kind", ["legacy", "0.1.0", "0.2.0"])
def test_clean_preserves_old_protocol_and_retained_fact_shape(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    case = merge_case
    if kind != "legacy":
        approve_grant(case, monkeypatch, version=kind)
    ready = case.inspect()
    assert ready["ready"] is True and len(case.opener.calls) == 14
    assert "legacy_statuses" not in ready["provenance"]["facts"]
    assert "required_conditions" not in ready["provenance"]["facts"]
    assert not any(
        call["path"] in STATUS_PATHS
        or call["path"] == COMPARE_PATH
        or (call["body"] and call["body"].get("query") == github.UNSTABLE_QUERY)
        for call in case.opener.calls
    )
    case.opener.calls.clear()
    case.configuration["call_limits"]["invoke"]["read_calls"] = 32
    case.rebind()
    assert case.invoke()["outcome"] == "applied"
    assert len(case.opener.calls) - len(case.opener.writes) == 32


@pytest.mark.parametrize("kind", ["legacy", "0.1.0"])
def test_old_credentials_do_not_authorize_new_status_endpoints(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    case = merge_case
    if kind != "legacy":
        approve_grant(case, monkeypatch, version=kind)
    case.opener.graph["mergeStateStatus"] = "UNSTABLE"
    assert_no_effect(case)
    assert not any(
        call["path"] in STATUS_PATHS
        or call["path"] == COMPARE_PATH
        or (call["body"] and call["body"].get("query") == github.UNSTABLE_QUERY)
        for call in case.opener.calls
    )


@pytest.mark.parametrize("kind", ["legacy", "0.1.0"])
def test_pure_unstable_validation_also_requires_independently_approved_status_capability(
    unstable_case: MergeCase, kind: str
) -> None:
    case = unstable_case
    ready = case.inspect()
    assert ready["ready"] is True
    configuration, facts = copy.deepcopy(case.configuration), copy.deepcopy(ready["provenance"]["facts"])
    if kind == "legacy":
        del configuration["actor"]["credential_kind"]
        del configuration["actor"]["grant"]
        facts["repository"]["push"] = True
    else:
        configuration["actor"]["grant"]["schema_version"] = "0.1.0"
        del configuration["actor"]["grant"]["permissions"]["statuses"]
    with pytest.raises(ValueError):
        merge.validate_ready_facts(case.request, case.payload, configuration, case.policy, facts)


def test_status_read_candidate_is_advisory_and_cannot_install_its_own_approval(unstable_case: MergeCase) -> None:
    case = unstable_case
    candidate = copy.deepcopy(case.configuration["actor"])
    del candidate["identity_approval_digest"]
    facts = {
        "repository": {**case.configuration["repository"], "admin": False, "maintain": False, "push": False},
        "installation_repository_ids": [123],
    }
    assert merge.validate_installation_grant_candidate(case.configuration["repository"], candidate, facts) == candidate
    configuration = copy.deepcopy(case.configuration)
    configuration["actor"] = candidate
    with pytest.raises(ValueError):
        github.GitHubMergeAdapter(configuration, case.policy, opener=case.opener)
    assert case.opener.calls == []


@pytest.mark.parametrize(
    "damage", ["missing-status-read", "old-schema-six", "status-write", "extra-grant", "unknown-schema"]
)
def test_expanded_capability_is_a_closed_new_grant(unstable_case: MergeCase, damage: str) -> None:
    configuration = copy.deepcopy(unstable_case.configuration)
    grant = configuration["actor"]["grant"]
    if damage == "missing-status-read":
        del grant["permissions"]["statuses"]
    elif damage == "old-schema-six":
        grant["schema_version"] = "0.1.0"
    elif damage == "status-write":
        grant["permissions"]["statuses"] = "write"
    elif damage == "extra-grant":
        grant["permissions"]["actions"] = "read"
    else:
        grant["schema_version"] = "0.3.0"
    with pytest.raises(ValueError):
        github.GitHubMergeAdapter(configuration, unstable_case.policy, opener=unstable_case.opener)
    assert unstable_case.opener.calls == []


def test_five_to_six_permissions_is_not_read_only_credential_rotation(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    approve_grant(case, monkeypatch, version="0.1.0")
    old_intent, old_payload = copy.deepcopy(case.intent), copy.deepcopy(case.payload)
    old_profile = case.adapter.operation_profile_digest
    approve_grant(case, monkeypatch)
    assert case.adapter.operation_profile_digest != old_profile
    with pytest.raises(ValueError, match="operation profile changed"):
        case.adapter.reconcile(old_intent, old_payload, deadline=Deadline(30))
    assert case.opener.calls == []


def test_required_legacy_status_change_after_preparation_stops_atomic_publication(unstable_case: MergeCase) -> None:
    case = unstable_case

    def change(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "POST" and path == PREFIX + "/git/tags":
            provider.legacy_statuses[MERGE].update(
                total_count=1, statuses=[{"context": "verify-exact-merge", "state": "failure"}]
            )

    case.opener.before_request = change
    result = case.invoke()
    assert result["outcome"] == "uncertain" and len(case.opener.writes) == 1
    assert case.opener.refs[BASE_REF] == BASE and case.opener.mutations == []
    writes = copy.deepcopy(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain" and case.opener.writes == writes


def test_clean_to_unstable_cannot_release_refs_under_the_old_read_allowance(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.opener.graph["mergeStateStatus"] = "CLEAN"
    case.configuration["call_limits"]["invoke"]["read_calls"] = 32
    case.rebind()

    def change(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "POST" and path == PREFIX + "/git/tags":
            provider.graph["mergeStateStatus"] = "UNSTABLE"

    case.opener.before_request = change
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert len(case.opener.writes) == 1 and case.opener.mutations == []
    assert len(case.opener.calls) - len(case.opener.writes) == 32
    assert case.opener.refs[BASE_REF] == BASE and case.payload["marker_ref"] not in case.opener.refs
    assert case.configuration["call_limits"]["invoke"]["read_calls"] == 32
    writes = copy.deepcopy(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain" and case.opener.writes == writes


def test_server_still_enforces_a_required_conversation_revoked_after_last_read(unstable_case: MergeCase) -> None:
    case = unstable_case
    case.opener.before_update = lambda provider, supplied: provider.review_threads.update(
        totalCount=1, nodes=[{"id": "PRRT_raced", "isResolved": False}]
    )
    result = case.invoke()
    assert result["outcome"] == "uncertain" and len(case.opener.writes) == 2
    assert case.opener.refs[BASE_REF] == BASE and case.opener.mutations == []


def test_expiry_before_new_policy_reads_prevents_further_http(
    unstable_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = unstable_case
    clock = [NOW]
    monkeypatch.setattr(github, "_credential_now", lambda: clock[0])

    def expire(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "GET" and path in STATUS_PATHS:
            clock[0] = NOW + dt.timedelta(days=1)

    case.opener.before_request = expire
    assert case.inspect()["ready"] is False and case.opener.writes == []
    assert sum(call["path"] in STATUS_PATHS for call in case.opener.calls) == 1
    assert case.opener.calls[-1]["path"] in STATUS_PATHS


def test_new_retained_facts_are_closed_and_cannot_omit_status_evidence(unstable_case: MergeCase) -> None:
    case = unstable_case
    result = case.inspect()
    assert result["ready"] is True
    for field in ("legacy_statuses", "required_conditions"):
        changed = copy.deepcopy(result)
        del changed["provenance"]["facts"][field]
        with pytest.raises(ValueError):
            case.adapter.validate_result("inspect", case.request, case.payload, changed)
    changed = copy.deepcopy(result)
    changed["provenance"]["facts"]["required_conditions"]["branch_policy"]["unreviewed"] = True
    with pytest.raises(ValueError):
        case.adapter.validate_result("inspect", case.request, case.payload, changed)


def test_only_selected_provider_fields_enter_unstable_provenance(unstable_case: MergeCase) -> None:
    case = unstable_case
    secret = "untrusted-provider-debug-" + TOKEN
    case.opener.branch_policy["debug"] = secret
    case.opener.comparison["files"] = [{"patch": secret}]
    case.opener.legacy_statuses[HEAD]["debug"] = secret
    result = case.inspect()
    assert result["ready"] is True and secret not in json.dumps(result)
    case.adapter.validate_result("inspect", case.request, case.payload, result)


def test_lost_unstable_response_reconciles_without_another_effect_and_replays_without_key(
    unstable_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = unstable_case

    def lose_reply(provider: Any) -> None:
        raise URLError("untrusted reply " + TOKEN)

    case.opener.after_update = lose_reply
    uncertain = case.invoke()
    assert uncertain["outcome"] == "uncertain" and case.opener.refs[BASE_REF] == MERGE
    writes = copy.deepcopy(case.opener.writes)
    case.opener.after_update = None
    recovered = case.reconcile()
    assert recovered["outcome"] == "applied" and case.opener.writes == writes
    # Discovery is historical proof after its actual key/token lease disappears.
    case.opener.after_update = None
    case.opener.graph["mergeStateStatus"] = "UNSTABLE"
    calls = copy.deepcopy(case.opener.calls)
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    monkeypatch.setattr(github, "_credential_now", lambda: NOW + dt.timedelta(days=1))
    Path(case.configuration["key"]["path"]).unlink()
    assert case.adapter.validate_result("reconcile", case.request, case.payload, recovered) == recovered
    assert case.opener.calls == calls and TOKEN not in json.dumps(uncertain)


def test_unstable_terminal_receipt_replays_offline_without_reloading_current_authority(
    unstable_case: MergeCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = unstable_case
    private = tmp_path / "retained-unstable-journal"
    private.mkdir(mode=0o700)
    path = private / "delivery.sqlite3"
    metadata = delivery_store.initialize(
        path,
        objective=case.request["objective"],
        limits=case.authorization["limits"],
        native_limits=case.adapter.call_limits["invoke"],
    )
    case.authorization["journal"] = {"path": str(path), "id": metadata["id"]}
    case.pin = delivery.ApprovalPin(
        case.authorization["issuer"], case.authorization["approval_id"], delivery.digest(case.authorization)
    )
    case.intent = common_intent(case.request, case.authorization)
    case.intent["cost"].update(wall_time_ms=120000, reads=64)
    identify(case.intent)
    result = case.invoke()
    assert result["outcome"] == "applied"
    assert result["provenance"]["facts"]["preflight"]["review"]["mergeStateStatus"] == "UNSTABLE"
    receipt = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-receipt",
        "intent_id": case.intent["id"],
        "operation_key": case.request["operation_key"],
        "request_digest": delivery.digest(case.request),
        "authority_digest": delivery.digest(case.authorization),
        "stage": "merge",
        "adapter": copy.deepcopy(case.authorization["adapter"]),
        **{key: copy.deepcopy(result[key]) for key in ("outcome", "effect", "reason")},
        "provenance_digest": delivery.digest(result["provenance"]),
        "claims": copy.deepcopy(delivery.CLAIMS),
    }
    identify(receipt)
    with delivery_store.opened(path, metadata["id"], write=True) as retained:
        retained.prepare(case.intent, case.payload, case.authorization, case.pin, NOW)
        call = retained.reserve_call(
            case.request["operation_key"],
            "invoke",
            case.adapter.call_limits["invoke"],
            case.authorization,
            case.pin,
            NOW,
        )
        retained.update_call(
            case.request["operation_key"],
            call["id"],
            lambda value: value.update(
                owner={"process": executor_runtime.identity(os.getpid()), "deadline_monotonic": time.monotonic() + 120},
                cleanup=True,
            ),
        )
        retained.finish_call(case.request["operation_key"], call["id"], result, receipt)
    before, calls = path.read_bytes(), copy.deepcopy(case.opener.calls)
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    monkeypatch.setattr(github, "_credential_now", lambda: NOW + dt.timedelta(days=1))
    Path(case.configuration["key"]["path"]).unlink()
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result

    def forbidden() -> Any:
        pytest.fail("offline terminal replay loaded current configuration, credential or provider")

    replay = DeliveryEngine(path, journal_id=metadata["id"], adapter_factory=forbidden)
    historical = replay.apply(
        case.request, case.payload, approval={}, pin=delivery.ApprovalPin("missing", "missing", "missing")
    )
    assert historical["historical"] is True and historical["receipt"] == receipt
    assert path.read_bytes() == before and case.opener.calls == calls
