"""Controlled App-grant authority, live credential leases and historical receipt compatibility."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, MERGE, PREFIX, TOKEN, TOKEN_ENVIRONMENT, MergeCase, make_case
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
PERMISSIONS = {
    "contents": "write",
    "administration": "read",
    "checks": "read",
    "metadata": "read",
    "pull_requests": "read",
}


def timestamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def grant(case: MergeCase) -> Doc:
    return {
        "schema_version": "0.1.0",
        "repository_ids": [case.configuration["repository"]["id"]],
        "permissions": copy.deepcopy(PERMISSIONS),
        "not_before": timestamp(NOW - dt.timedelta(minutes=1)),
        "not_after": timestamp(NOW + dt.timedelta(minutes=9)),
        "mint_provenance_digest": delivery.digest("independently reviewed pinned mint-run evidence"),
    }


@pytest.fixture
def app_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MergeCase:
    case = make_case(tmp_path, monkeypatch)
    case.configuration["actor"].update(
        credential_kind="github-app-installation-grant-v1",
        grant=grant(case),
    )
    case.opener.repository["permissions"] = {"admin": False, "maintain": False, "push": False}
    monkeypatch.setattr(github, "_credential_now", lambda: NOW)
    case.rebind()
    return case


def test_authentic_app_role_flags_use_bound_grants_without_rewriting_provider_facts(app_case: MergeCase) -> None:
    case = app_case
    ready = case.inspect()
    assert ready["ready"] is True
    assert case.opener.writes == []
    assert {key: ready["provenance"]["facts"]["repository"][key] for key in ("admin", "maintain", "push")} == {
        "admin": False,
        "maintain": False,
        "push": False,
    }
    applied = case.invoke()
    assert applied["outcome"] == "applied" and applied["effect"]["commit"] == MERGE
    assert case.opener.refs[BASE_REF] == MERGE and len(case.opener.mutations) == 1
    assert len(case.opener.writes) == 2
    assert applied["provenance"]["facts"]["actor"]["repository"]["push"] is False
    assert case.adapter.validate_result("invoke", case.request, case.payload, applied) == applied


@pytest.mark.parametrize("permissions", [None, {}, {"admin": False, "push": False}])
def test_missing_optional_app_role_flags_remain_unknown_not_synthesized(
    app_case: MergeCase, permissions: Doc | None
) -> None:
    case = app_case
    if permissions is None:
        del case.opener.repository["permissions"]
    else:
        case.opener.repository["permissions"] = permissions
    result = case.inspect()
    assert result["ready"] is True
    facts = result["provenance"]["facts"]["repository"]
    for key in ("admin", "maintain", "push"):
        assert facts[key] is (permissions or {}).get(key)
    assert case.opener.writes == []


@pytest.mark.parametrize("permissions", [{"admin": True}, {"maintain": True}, "untrusted", {"push": "false"}])
def test_app_grant_does_not_accept_broad_or_malformed_observed_roles(app_case: MergeCase, permissions: Any) -> None:
    app_case.opener.repository["permissions"] = permissions
    assert app_case.inspect()["ready"] is False
    assert app_case.invoke()["outcome"] == "rejected"
    assert app_case.opener.writes == []


@pytest.mark.parametrize(
    "damage",
    [
        "missing-kind",
        "missing-grant",
        "unknown-kind",
        "unknown-grant-schema",
        "wrong-repository",
        "extra-repository",
        "duplicate-repository",
        "missing-permission",
        "contents-read",
        "administration-write",
        "checks-write",
        "pull-requests-write",
        "extra-permission",
        "extra-actor-field",
        "extra-grant-field",
        "missing-provenance",
        "bad-provenance-digest",
        "reversed-window",
        "empty-window",
        "overlong-window",
        "naive-time",
        "non-utc-time",
    ],
)
def test_app_grant_is_closed_exact_and_rejects_unapproved_metadata_before_http(
    app_case: MergeCase, damage: str
) -> None:
    configuration = copy.deepcopy(app_case.configuration)
    actor = configuration["actor"]
    proof = actor["grant"]
    if damage == "missing-kind":
        del actor["credential_kind"]
    elif damage == "missing-grant":
        del actor["grant"]
    elif damage == "unknown-kind":
        actor["credential_kind"] = "caller-selected-authority"
    elif damage == "unknown-grant-schema":
        proof["schema_version"] = "999.0.0"
    elif damage in ("wrong-repository", "extra-repository", "duplicate-repository"):
        proof["repository_ids"] = {
            "wrong-repository": [999],
            "extra-repository": [123, 999],
            "duplicate-repository": [123, 123],
        }[damage]
    elif damage == "missing-permission":
        del proof["permissions"]["contents"]
    elif damage in ("contents-read", "administration-write", "checks-write", "pull-requests-write"):
        name, value = {
            "contents-read": ("contents", "read"),
            "administration-write": ("administration", "write"),
            "checks-write": ("checks", "write"),
            "pull-requests-write": ("pull_requests", "write"),
        }[damage]
        proof["permissions"][name] = value
    elif damage == "extra-permission":
        proof["permissions"]["issues"] = "write"
    elif damage == "extra-actor-field":
        actor["token"] = TOKEN
    elif damage == "extra-grant-field":
        proof["self_approved"] = True
    elif damage == "missing-provenance":
        del proof["mint_provenance_digest"]
    elif damage == "bad-provenance-digest":
        proof["mint_provenance_digest"] = "unbound issuer claim"
    elif damage in ("reversed-window", "empty-window", "overlong-window"):
        proof["not_after"] = timestamp(
            {
                "reversed-window": NOW - dt.timedelta(minutes=2),
                "empty-window": NOW - dt.timedelta(minutes=1),
                "overlong-window": NOW + dt.timedelta(hours=1),
            }[damage]
        )
    elif damage == "naive-time":
        proof["not_before"] = "2026-11-02T11:59:00"
    else:
        proof["not_before"] = "2026-11-02T12:59:00+01:00"
    with pytest.raises(ValueError):
        github.GitHubMergeAdapter(configuration, app_case.policy, opener=app_case.opener)
    assert app_case.opener.calls == []


@pytest.mark.parametrize("phase", ["expired", "not-yet-valid", "exact-expiry"])
def test_local_grant_lease_blocks_all_fresh_io_including_recovery(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    case = app_case
    proof = case.configuration["actor"]["grant"]
    now = {
        "expired": NOW + dt.timedelta(days=1),
        "not-yet-valid": NOW - dt.timedelta(days=1),
        "exact-expiry": delivery._timestamp(proof["not_after"]),
    }[phase]
    monkeypatch.setattr(github, "_credential_now", lambda: now)
    # Structural validation is historical and deliberately independent of the live clock.
    assert merge.validate_configuration(case.configuration) == case.configuration
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.reconcile()["outcome"] == "uncertain"
    assert case.opener.calls == []


def test_expiry_between_reads_blocks_the_next_http_request(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = app_case
    clock = [NOW]
    monkeypatch.setattr(github, "_credential_now", lambda: clock[0])

    def expire(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "GET" and path == PREFIX:
            clock[0] = NOW + dt.timedelta(days=1)

    case.opener.before_request = expire
    assert case.inspect()["ready"] is False
    assert [call["path"] for call in case.opener.calls] == [PREFIX]
    assert case.opener.writes == []


def test_expiry_after_marker_preparation_cannot_publish_or_retry_refs(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = app_case
    clock = [NOW]
    monkeypatch.setattr(github, "_credential_now", lambda: clock[0])

    def expire(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "POST" and path == PREFIX + "/git/tags":
            clock[0] = NOW + dt.timedelta(days=1)

    case.opener.before_request = expire
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert case.opener.refs[BASE_REF] == BASE and case.payload["marker_ref"] not in case.opener.refs
    assert len(case.opener.writes) == 1 and case.opener.mutations == []
    writes = copy.deepcopy(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain"
    assert case.opener.writes == writes


@pytest.mark.parametrize("after_preparation", [False, True])
def test_lease_is_checked_immediately_before_each_provider_write(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch, after_preparation: bool
) -> None:
    case = app_case
    clock = [NOW]
    monkeypatch.setattr(github, "_credential_now", lambda: clock[0])
    marker_path = PREFIX + "/git/ref/" + case.payload["marker_ref"].removeprefix("refs/")

    def expire_at_final_preflight_read(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "GET" and path == marker_path and len(provider.writes) == int(after_preparation):
            clock[0] = NOW + dt.timedelta(days=1)

    case.opener.before_request = expire_at_final_preflight_read
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert case.opener.refs[BASE_REF] == BASE and case.opener.mutations == []
    assert len(case.opener.writes) == int(after_preparation)
    assert case.opener.calls[-1]["path"] == marker_path
    assert case.payload["marker_ref"] not in case.opener.refs


@pytest.mark.parametrize("damage", ["scope", "review", "checks", "protection", "rules", "contract"])
def test_valid_app_grant_never_substitutes_for_existing_merge_preconditions(app_case: MergeCase, damage: str) -> None:
    case = app_case
    if damage == "scope":
        case.opener.installation_repository_ids.append(999)
    elif damage == "review":
        case.opener.graph["reviewDecision"] = "REVIEW_REQUIRED"
    elif damage == "checks":
        case.opener.check_runs[0]["conclusion"] = "failure"
    elif damage == "protection":
        case.opener.protection["enforce_admins"]["enabled"] = False
    elif damage == "rules":
        case.opener.rules = [{"type": "required_merge_queue"}]
    else:
        case.opener.trees[case.request["subject"]["head_revision"]][0]["sha"] = "f" * 40
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == []


@pytest.mark.parametrize("changed", ["token", "app", "installation", "mint-provenance", "approval"])
def test_copied_grant_metadata_cannot_reuse_another_approved_configuration(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    case = app_case
    configuration = copy.deepcopy(case.configuration)
    actor = configuration["actor"]
    if changed == "token":
        other = "different-unapproved-installation-token"
        actor["token_digest"] = "sha256:" + hashlib.sha256(other.encode()).hexdigest()
        monkeypatch.setenv(TOKEN_ENVIRONMENT, other)
        case.opener.expected_token = other
    elif changed in ("app", "installation"):
        actor[changed + "_id"] += 1
    elif changed == "mint-provenance":
        actor["grant"]["mint_provenance_digest"] = delivery.digest("copied proof from a different mint")
    else:
        actor["identity_approval_digest"] = delivery.digest("not the independently selected approval")
    adapter = github.GitHubMergeAdapter(configuration, case.policy, opener=case.opener)
    assert adapter.configuration_digest != case.adapter.configuration_digest
    with pytest.raises(ValueError):
        adapter.invoke(case.intent, case.payload, case.authorization, deadline=Deadline(30))
    assert case.opener.calls == []


@pytest.mark.parametrize("missing", [False, True])
def test_app_grant_for_another_actual_token_cannot_use_an_approved_configuration(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    case = app_case
    other = "different-actual-token-without-the-approved-mint-binding"
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    if missing:
        monkeypatch.delenv(TOKEN_ENVIRONMENT)
    else:
        monkeypatch.setenv(TOKEN_ENVIRONMENT, other)
        case.opener.expected_token = other
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.reconcile()["outcome"] == "uncertain"
    assert case.opener.calls == []


def candidate_and_facts(case: MergeCase) -> tuple[Doc, Doc]:
    candidate = copy.deepcopy(case.configuration["actor"])
    del candidate["identity_approval_digest"]
    return candidate, {
        "repository": {
            **copy.deepcopy(case.configuration["repository"]),
            "admin": False,
            "maintain": False,
            "push": False,
        },
        "installation_repository_ids": [case.configuration["repository"]["id"]],
    }


def test_advisory_candidate_validation_cannot_manufacture_installation_approval(app_case: MergeCase) -> None:
    case = app_case
    candidate, facts = candidate_and_facts(case)
    before = copy.deepcopy(candidate)
    checked = merge.validate_installation_grant_candidate(case.configuration["repository"], candidate, facts)
    assert checked == before and candidate == before
    assert "identity_approval_digest" not in checked
    configuration = copy.deepcopy(case.configuration)
    configuration["actor"] = checked
    with pytest.raises(ValueError):
        github.GitHubMergeAdapter(configuration, case.policy, opener=case.opener)
    # Candidate use has no HTTP, local key access or effect-authority side effect.
    assert case.opener.calls == []


@pytest.mark.parametrize("damage", ["approval", "extra", "repository", "scope", "administrator", "malformed-role"])
def test_candidate_rejects_approval_smuggling_or_mismatched_live_identity(app_case: MergeCase, damage: str) -> None:
    case = app_case
    candidate, facts = candidate_and_facts(case)
    if damage == "approval":
        candidate["identity_approval_digest"] = delivery.digest("candidate cannot approve itself")
    elif damage == "extra":
        candidate["ready"] = True
    elif damage == "repository":
        facts["repository"]["id"] = 999
    elif damage == "scope":
        facts["installation_repository_ids"].append(999)
    elif damage == "administrator":
        facts["repository"]["admin"] = True
    else:
        facts["repository"]["maintain"] = "false"
    with pytest.raises(ValueError):
        merge.validate_installation_grant_candidate(case.configuration["repository"], candidate, facts)
    assert case.opener.calls == []


def test_candidate_static_metadata_and_fresh_time_validation_have_separate_roles(app_case: MergeCase) -> None:
    case = app_case
    candidate, facts = candidate_and_facts(case)
    assert merge.validate_installation_grant_candidate(case.configuration["repository"], candidate, facts) == candidate
    merge.validate_installation_grant_time(candidate, now=NOW)
    with pytest.raises(ValueError):
        merge.validate_installation_grant_time(candidate, now=NOW + dt.timedelta(days=1))
    assert merge.validate_installation_grant_candidate(case.configuration["repository"], candidate, facts) == candidate
    assert case.opener.calls == []


def test_same_authority_rotation_preserves_marker_and_only_permits_read_recovery(
    app_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = app_case
    marker = merge.marker(case.request, case.payload, case.configuration, case.policy, lock_timeout=10)

    def lost_acknowledgement(provider: Any) -> None:
        raise URLError("controlled lost acknowledgement")

    case.opener.after_update = lost_acknowledgement
    assert case.invoke()["outcome"] == "uncertain"
    assert case.opener.refs[BASE_REF] == MERGE and len(case.opener.writes) == 2
    rotated = "separately-approved-app-installation-token"
    configuration = copy.deepcopy(case.configuration)
    configuration["actor"].update(
        token_digest="sha256:" + hashlib.sha256(rotated.encode()).hexdigest(),
        identity_approval_digest=delivery.digest("independent approval of replacement App mint"),
    )
    configuration["actor"]["grant"].update(
        not_before=timestamp(NOW),
        not_after=timestamp(NOW + dt.timedelta(minutes=20)),
        mint_provenance_digest=delivery.digest("replacement mint-run evidence"),
    )
    monkeypatch.setenv(TOKEN_ENVIRONMENT, rotated)
    case.opener.expected_token = rotated
    adapter = github.GitHubMergeAdapter(configuration, case.policy, opener=case.opener)
    assert adapter.configuration_digest != case.adapter.configuration_digest
    assert adapter.operation_profile_digest == case.adapter.operation_profile_digest
    assert merge.marker(case.request, case.payload, configuration, case.policy, lock_timeout=10) == marker
    with pytest.raises(ValueError):
        adapter.invoke(case.intent, case.payload, case.authorization, deadline=Deadline(30))
    result = adapter.reconcile(case.intent, case.payload, deadline=Deadline(30))
    assert result["outcome"] == "applied" and result["effect"]["commit"] == MERGE
    assert result["provenance"]["facts"]["actor"]["repository"]["push"] is False
    assert adapter.validate_result("reconcile", case.request, case.payload, result) == result
    assert len(case.opener.writes) == 2 and len(case.opener.mutations) == 1


def test_legacy_actor_keeps_its_original_strict_role_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_case(tmp_path, monkeypatch)
    case.opener.repository["permissions"]["push"] = False
    assert case.inspect()["ready"] is False and case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == []
    original_payload = copy.deepcopy(case.payload)
    case.configuration["actor"].update(credential_kind="github-app-installation-grant-v1", grant=grant(case))
    adapter = github.GitHubMergeAdapter(case.configuration, case.policy, opener=case.opener)
    assert adapter.operation_profile_digest != original_payload["operation_profile_digest"]
    calls = copy.deepcopy(case.opener.calls)
    with pytest.raises(ValueError, match="operation profile changed"):
        adapter.reconcile(case.intent, original_payload, deadline=Deadline(30))
    assert case.opener.calls == calls


def test_expired_app_grant_does_not_expire_historical_native_proof_or_offline_receipt(
    app_case: MergeCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = app_case
    private = tmp_path / "retained-journal"
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
        **{name: copy.deepcopy(result[name]) for name in ("outcome", "effect", "reason")},
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
    calls, before = copy.deepcopy(case.opener.calls), path.read_bytes()
    monkeypatch.setattr(github, "_credential_now", lambda: NOW + dt.timedelta(days=1))
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    Path(case.configuration["key"]["path"]).unlink()
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result

    def forbidden() -> Any:
        pytest.fail("offline replay loaded an expired credential, current adapter or provider")

    replay = DeliveryEngine(path, journal_id=metadata["id"], adapter_factory=forbidden)
    historical = replay.apply(
        case.request, case.payload, approval={}, pin=delivery.ApprovalPin("missing", "missing", "missing")
    )
    assert historical["status"] == "applied" and historical["historical"] is True
    assert historical["receipt"] == receipt
    assert path.read_bytes() == before and case.opener.calls == calls
