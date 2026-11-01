"""The installed HTTP adapter uses fixed queries, exact atomic refs and discover-only uncertainty."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, HEAD, HEAD_REF, MERGE, PREFIX, TOKEN, TOKEN_ENVIRONMENT, MergeCase
from automatic_merge_fixtures import merge_case as merge_case
from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import execution_store as store
from exacly import github_merge as github
from exacly.github_evidence import NoRedirect


@pytest.mark.parametrize("ref", [BASE_REF, HEAD_REF, "marker"])
def test_racing_ref_rejects_entire_three_ref_transaction_including_noop_head_lease(
    merge_case: MergeCase, ref: str
) -> None:
    case = merge_case
    selected = case.payload["marker_ref"] if ref == "marker" else ref

    def race(provider: Any, supplied: Any) -> None:
        provider.refs[selected] = "f" * 40

    case.opener.before_update = race
    outcome = case.invoke()
    assert outcome["outcome"] == "uncertain" and outcome["effect"] is None
    assert case.opener.mutations == [] and len(case.opener.writes) == 2
    assert case.opener.refs[selected] == "f" * 40
    if selected != BASE_REF:
        assert case.opener.refs[BASE_REF] == BASE
    if selected != HEAD_REF:
        assert case.opener.refs[HEAD_REF] == HEAD
    if ref != "marker":
        assert case.payload["marker_ref"] not in case.opener.refs
    writes = len(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain"
    assert len(case.opener.writes) == writes


@pytest.mark.parametrize("fact", ["review", "check", "head", "base", "contract"])
def test_changes_during_preparation_are_rechecked_before_conditional_publication(
    merge_case: MergeCase, fact: str
) -> None:
    case = merge_case

    def change_after_preparation(provider: Any, method: str, path: str, body: Any) -> None:
        if method == "POST" and path == PREFIX + "/git/tags":
            if fact == "review":
                provider.graph["reviewDecision"] = "CHANGES_REQUESTED"
            elif fact == "check":
                provider.check_runs[0]["conclusion"] = "failure"
            elif fact in ("head", "base"):
                provider.refs[HEAD_REF if fact == "head" else BASE_REF] = "f" * 40
            else:
                provider.trees[HEAD][0]["sha"] = "f" * 40

    case.opener.before_request = change_after_preparation
    outcome = case.invoke()
    assert outcome["outcome"] == "uncertain" and outcome["effect"] is None
    assert len(case.opener.writes) == 1 and case.opener.mutations == []
    assert case.payload["marker_ref"] not in case.opener.refs


def test_required_review_revoked_after_last_read_is_enforced_by_controlled_server(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.before_update = lambda provider, supplied: provider.graph.update(reviewDecision="CHANGES_REQUESTED")
    result = case.invoke()
    assert result["outcome"] == "uncertain"
    assert case.opener.refs[BASE_REF] == BASE and case.opener.mutations == []


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_server_passing_conclusion_can_change_after_stricter_success_preflight(
    merge_case: MergeCase, conclusion: str
) -> None:
    case = merge_case
    case.opener.before_update = lambda provider, supplied: provider.check_runs[0].update(conclusion=conclusion)
    result = case.invoke()
    assert result["outcome"] == "applied" and case.opener.refs[BASE_REF] == MERGE
    assert result["provenance"]["facts"]["preflight"]["check_runs"][0]["conclusion"] == "success"
    assert case.opener.check_runs[0]["conclusion"] == conclusion
    assert case.policy["server_passing_conclusions"] == ["success", "neutral", "skipped"]
    assert all(value is False for value in delivery.CLAIMS.values())
    # updateRefs leases exact refs; it does not lease a check-result or policy-version snapshot.
    case.adapter.validate_result("invoke", case.request, case.payload, result)


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_stricter_success_preflight_still_blocks_an_initial_server_passing_conclusion(
    merge_case: MergeCase, conclusion: str
) -> None:
    merge_case.opener.check_runs[0]["conclusion"] = conclusion
    result = merge_case.invoke()
    assert result["outcome"] == "rejected" and merge_case.opener.writes == []


@pytest.mark.parametrize("change", ["admin", "broad-installation"])
def test_read_only_recovery_verifies_current_credential_scope(merge_case: MergeCase, change: str) -> None:
    case = merge_case
    assert case.invoke()["outcome"] == "applied"
    writes = len(case.opener.writes)
    if change == "admin":
        case.opener.repository["permissions"]["admin"] = True
    else:
        case.opener.installation_repository_ids.append(999)
    assert case.reconcile()["outcome"] == "uncertain"
    assert len(case.opener.writes) == writes


def test_atomic_refs_do_not_claim_immediate_pull_request_metadata_acknowledgement(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.after_update = lambda provider: provider.pull_request.update(state="open", merged=False)
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert case.opener.refs[BASE_REF] == MERGE and case.payload["marker_ref"] in case.opener.refs
    writes = len(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain"
    assert len(case.opener.writes) == writes == 2
    case.opener.pull_request.update(state="closed", merged=True, merge_commit_sha=MERGE)
    recovered = case.reconcile()
    assert recovered["outcome"] == "applied" and recovered["effect"]["commit"] == MERGE
    assert len(case.opener.writes) == writes


def test_signature_protection_is_read_from_its_actual_separate_provider_endpoint(merge_case: MergeCase) -> None:
    case = merge_case
    assert "required_signatures" not in case.opener.respond("GET", PREFIX + "/branches/main/protection", None)
    assert case.inspect()["ready"] is True
    assert any(call["path"].endswith("/protection/required_signatures") for call in case.opener.calls)
    case.opener.protection["required_signatures"]["enabled"] = True
    assert case.inspect()["ready"] is False
    assert case.opener.writes == []


@pytest.mark.parametrize("damage", ["missing", "hash"])
def test_only_selected_delivery_credential_can_be_used(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    case = merge_case
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("EXACLY_REQ039_PUBLISH_TOKEN", TOKEN)
    if damage == "missing":
        monkeypatch.delenv(TOKEN_ENVIRONMENT)
    else:
        monkeypatch.setenv(TOKEN_ENVIRONMENT, "another-operator-token")
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.calls == []


def test_missing_private_delivery_key_blocks_before_any_http(merge_case: MergeCase) -> None:
    case = merge_case
    Path(case.configuration["key"]["path"]).unlink()
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.calls == []


def test_separately_selected_rotated_credential_reconciles_the_same_operation_without_reapplying(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    retained_request, retained_payload = copy.deepcopy(case.request), copy.deepcopy(case.payload)
    original_configuration = case.adapter.configuration_digest
    original_operation_profile = case.adapter.operation_profile_digest
    marker = merge.marker(case.request, case.payload, case.configuration, case.policy, lock_timeout=10)

    def lose_reply(provider: Any) -> None:
        raise URLError("controlled lost acknowledgement after the atomic effect")

    case.opener.after_update = lose_reply
    assert case.invoke()["outcome"] == "uncertain"
    assert case.opener.refs[BASE_REF] == MERGE and len(case.opener.writes) == 2
    rotated = "independently-approved-rotated-delivery-token"
    current = copy.deepcopy(case.configuration)
    current["actor"].update(
        token_digest="sha256:" + hashlib.sha256(rotated.encode()).hexdigest(),
        identity_approval_digest=delivery.digest("independent review of the replacement mint"),
    )
    monkeypatch.setenv(TOKEN_ENVIRONMENT, rotated)
    case.opener.expected_token = rotated
    adapter = github.GitHubMergeAdapter(current, case.policy, opener=case.opener)
    assert adapter.configuration_digest != original_configuration
    assert adapter.operation_profile_digest == original_operation_profile
    assert adapter.validate_payload(case.request, case.payload) == case.payload
    assert merge.marker(case.request, case.payload, current, case.policy, lock_timeout=10) == marker
    with pytest.raises(ValueError):
        adapter.invoke(case.intent, case.payload, case.authorization, deadline=store.Deadline(30))
    result = adapter.reconcile(case.intent, case.payload, deadline=store.Deadline(30))
    assert result["outcome"] == "applied" and result["effect"]["commit"] == MERGE
    assert adapter.validate_result("reconcile", case.request, case.payload, result) == result
    assert len(case.opener.writes) == 2 and len(case.opener.mutations) == 1
    assert case.request == retained_request and case.payload == retained_payload


@pytest.mark.parametrize("field", ["app", "installation", "key", "limits", "policy"])
def test_noncredential_profile_changes_are_not_read_only_credential_rotation(merge_case: MergeCase, field: str) -> None:
    case = merge_case
    configuration, policy = copy.deepcopy(case.configuration), copy.deepcopy(case.policy)
    if field in ("app", "installation"):
        configuration["actor"][field + "_id"] += 1
    elif field == "key":
        configuration["key"]["id"] = delivery.digest("different initialized key")
    elif field == "limits":
        configuration["call_limits"]["reconcile"]["read_calls"] += 1
    else:
        policy["minimum_approvals"] += 1
    adapter = github.GitHubMergeAdapter(configuration, policy, opener=case.opener)
    assert adapter.operation_profile_digest != case.payload["operation_profile_digest"]
    with pytest.raises(ValueError, match="operation profile changed"):
        adapter.reconcile(case.intent, case.payload, deadline=store.Deadline(30))
    assert case.opener.calls == []


@pytest.mark.parametrize("selector", ["GH_TOKEN", "EXACLY_DELIVERY_OTHER_TOKEN"])
def test_credential_selector_is_fixed_and_cannot_expand_redaction_or_authority_scope(
    merge_case: MergeCase, selector: str
) -> None:
    case = merge_case
    configuration = copy.deepcopy(case.configuration)
    configuration["token_environment"] = selector
    with pytest.raises(ValueError, match="fixed delivery-merge selector"):
        github.GitHubMergeAdapter(configuration, case.policy, opener=case.opener)
    assert case.opener.calls == []


def test_unconfirmed_provider_error_does_not_leak_tokens_or_remote_body(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.overrides[("GET", PREFIX)] = HTTPError(
        "https://api.github.com" + PREFIX, 500, TOKEN, Message(), io.BytesIO(TOKEN.encode())
    )
    outcome = case.invoke()
    assert outcome["outcome"] == "rejected"
    assert TOKEN not in json.dumps(outcome) and "Bearer" not in json.dumps(outcome)
    assert case.opener.writes == []


def test_expired_whole_call_deadline_cannot_start_http(merge_case: MergeCase) -> None:
    case = merge_case
    result = case.adapter.inspect(case.request, case.payload, deadline=store.Deadline(0))
    assert result["ready"] is False and case.opener.calls == []


def test_native_read_ceiling_covers_exact_protocol_and_smaller_caps_are_rejected(merge_case: MergeCase) -> None:
    case = merge_case
    case.configuration["call_limits"]["invoke"]["read_calls"] = 32
    case.rebind()
    result = case.invoke()
    assert result["outcome"] == "applied" and len(case.opener.writes) == 2
    assert len([call for call in case.opener.calls if call not in case.opener.writes]) == 32
    count = len(case.opener.calls)
    case.configuration["call_limits"]["invoke"]["read_calls"] = 31
    with pytest.raises(ValueError, match="read ceiling"):
        case.rebind()
    assert len(case.opener.calls) == count


@pytest.mark.parametrize("damage", ["missing", "wrong_object", "wrong_message", "pr_open", "wrong_commit"])
def test_reconciliation_requires_actual_merge_and_exact_marker_receipt(merge_case: MergeCase, damage: str) -> None:
    case = merge_case
    assert case.invoke()["outcome"] == "applied"
    marker_ref = case.payload["marker_ref"]
    oid = case.opener.refs[marker_ref]
    if damage == "missing":
        del case.opener.refs[marker_ref]
    elif damage == "wrong_object":
        case.opener.tags[oid]["object"] = {"sha": "f" * 40, "type": "commit"}
    elif damage == "wrong_message":
        case.opener.tags[oid]["message"] += "unapproved"
    elif damage == "pr_open":
        case.opener.pull_request.update(state="open", merged=False)
    else:
        case.opener.commit["parents"] = [{"sha": HEAD}, {"sha": BASE}]
    writes = copy.deepcopy(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain"
    assert case.opener.writes == writes


def test_lost_preparation_reply_never_rolls_forward_from_object_existence(merge_case: MergeCase) -> None:
    case = merge_case
    original = case.opener.respond

    def lost(method: str, path: str, body: Any) -> Any:
        result = original(method, path, body)
        if method == "POST" and path == PREFIX + "/git/tags":
            raise URLError("controlled preparation acknowledgement loss")
        return result

    case.opener.respond = lost  # type: ignore[method-assign]
    result = case.invoke()
    assert result["outcome"] == "uncertain" and len(case.opener.tags) == 1
    assert case.opener.refs[BASE_REF] == BASE and case.payload["marker_ref"] not in case.opener.refs
    writes = len(case.opener.writes)
    assert case.reconcile()["outcome"] == "uncertain" and len(case.opener.writes) == writes


def test_default_installed_http_transport_disallows_redirects(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    handlers: list[Any] = []

    def selected(*values: Any) -> Any:
        handlers.extend(values)
        return case.opener

    monkeypatch.setattr(github, "build_opener", selected)
    adapter = github.GitHubMergeAdapter(case.configuration, case.policy)
    assert len(handlers) == 1 and isinstance(handlers[0], NoRedirect)
    assert adapter.inspect(case.request, case.payload, deadline=store.Deadline(10))["ready"] is True
    assert all(call["path"] != "/pulls/42/merge" for call in case.opener.calls)


def test_real_urllib_roundtrip_to_controlled_http_server(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise actual HTTP parsing and response IO; this is not live GitHub protection acceptance."""
    case = merge_case
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            assert self.headers["Authorization"] == "Bearer " + TOKEN
            assert self.headers["X-GitHub-Api-Version"] == "2026-03-10"
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append((self.command, self.path))
            try:
                value = case.opener.respond(self.command, self.path, body)
                status = 200
            except HTTPError as error:
                value, status = {"message": "controlled missing object"}, error.code
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    monkeypatch.setattr(github, "API", f"http://127.0.0.1:{server.server_port}")
    worker.start()
    try:
        adapter = github.GitHubMergeAdapter(case.configuration, case.policy)
        assert adapter.inspect(case.request, case.payload, deadline=store.Deadline(10))["ready"] is True
        result = adapter.invoke(case.intent, case.payload, case.authorization, deadline=store.Deadline(10))
        assert result["outcome"] == "applied" and result["effect"]["commit"] == MERGE
        assert len(case.opener.mutations) == 1
        assert ("POST", "/graphql") in seen and ("POST", PREFIX + "/git/tags") in seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
    assert not worker.is_alive()
