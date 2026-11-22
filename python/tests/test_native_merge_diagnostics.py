"""Native diagnostics retain bounded observations without changing merge authority or sends."""

from __future__ import annotations

import copy
import io
import json
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, MERGE, PREFIX, TOKEN, TOKEN_ENVIRONMENT, MergeCase, Response
from automatic_merge_fixtures import merge_case as merge_case
from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import github_merge as github
from test_delivery_contracts import identify, receipt

Doc = dict[str, Any]
PREDICATES = {
    "pull_request_identity_types",
    "marker_reference",
    "marker_object_type",
    "marker_object_identity",
    "marker_tag",
    "marker_message",
    "marker_target_type",
    "marker_target",
    "pull_request_merged",
    "merge_revision",
    "pull_request_number",
    "repository",
    "head_repository",
    "head_revision",
    "head_ref",
    "base_ref",
    "merge_commit",
}
PR_PREDICATES = {
    "pull_request_identity_types",
    "pull_request_merged",
    "merge_revision",
    "pull_request_number",
    "repository",
    "head_repository",
    "head_revision",
    "head_ref",
    "base_ref",
}
TAG_PREDICATES = {
    "marker_object_type",
    "marker_object_identity",
    "marker_tag",
    "marker_message",
    "marker_target_type",
    "marker_target",
}


def _diagnostic(result: Doc) -> Doc:
    value: Doc = result["provenance"]["diagnostic"]
    assert merge.validate_diagnostic(value) == value
    assert set(value["applied_predicates"]) == PREDICATES
    return value


def _project(case: MergeCase, method: str, result: Doc) -> Doc:
    assert case.adapter.validate_result(method, case.request, case.payload, result) == result
    value: Doc = merge.project_result_diagnostics(method, result)
    assert set(value) == {
        "schema_version",
        "profile",
        "method",
        "provenance_digest",
        "diagnostic_available",
        "write_started",
        "marker_object",
        "preparation_object",
        "diagnostic",
    }
    assert value["schema_version"] == merge.VERSION
    assert value["profile"] == "github-atomic-merge-diagnostics-v1"
    assert value["method"] == method
    assert value["provenance_digest"] == delivery.digest(result["provenance"])
    return value


def _http_failure(status: int = 503) -> HTTPError:
    headers = Message()
    headers["X-Controlled-Secret"] = "private-provider-header"
    error = HTTPError(
        "https://api.github.com/private-provider-url",
        status,
        "private-provider-reason",
        headers,
        io.BytesIO(b"private-provider-body"),
    )
    error.__cause__ = ValueError("private-provider-cause")
    return error


@pytest.mark.parametrize("point", ["preflight", "tag_preparation", "atomic_update"])
def test_provider_http_failure_retains_only_owned_stage_and_numeric_status(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    case = merge_case
    original = case.opener.respond

    def fail(method: str, path: str, body: Any) -> Any:
        selected = (
            (point == "preflight" and method == "GET" and path == PREFIX)
            or (point == "tag_preparation" and method == "POST" and path == PREFIX + "/git/tags")
            or (point == "atomic_update" and method == "POST" and body and body.get("query") == github.UPDATE_QUERY)
        )
        if selected:
            raise _http_failure()
        return original(method, path, body)

    monkeypatch.setattr(case.opener, "respond", fail)
    result = case.invoke()
    value = _diagnostic(result)
    assert value["stage"] == point and value["code"] == "http_error" and value["http_status"] == 503
    assert result["outcome"] == ("rejected" if point == "preflight" else "uncertain")
    assert result["effect"] is None
    assert value["write_may_have_started"] is (point != "preflight")
    assert result["provenance"]["write_started"] is (point != "preflight")
    assert all(item is None for item in value["applied_predicates"].values())
    assert len(case.opener.writes) == {"preflight": 0, "tag_preparation": 1, "atomic_update": 2}[point]
    assert case.opener.mutations == [] and case.opener.refs[BASE_REF] == BASE
    projection = _project(case, "invoke", result)
    serialized = json.dumps({"result": result, "projection": projection}, sort_keys=True)
    for secret in (TOKEN, "Bearer", "private-provider-url", "private-provider-reason", "private-provider-header"):
        assert secret not in serialized
    assert "private-provider-body" not in serialized and "private-provider-cause" not in serialized


def test_transport_diagnostic_never_formats_exception_arguments_or_causes(merge_case: MergeCase) -> None:
    class UnprintableTransportError(URLError):
        def __str__(self) -> str:
            raise AssertionError("provider exception must not be formatted")

    error = UnprintableTransportError("private-transport-args")
    error.__cause__ = ValueError("private-transport-cause")
    merge_case.opener.overrides[("POST", PREFIX + "/git/tags")] = error
    result = merge_case.invoke()
    value = _diagnostic(result)
    assert value["stage"] == "tag_preparation" and value["code"] == "transport_error"
    assert value["http_status"] is None and value["graphql_errors"] is None
    assert result["outcome"] == "uncertain" and value["write_may_have_started"] is True
    assert value["preparation_response_observed"] is None and value["preparation_confirmed"] is None
    assert value["preparation_object"] is None and value["atomic_update_acknowledged"] is None
    assert "private-transport" not in json.dumps(_project(merge_case, "invoke", result))


def test_invalid_preparation_reply_keeps_object_existence_separate_from_confirmation(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    original = case.opener.open

    def malformed(request: Any, *, timeout: float) -> Response:
        response = original(request, timeout=timeout)
        if request.get_method() == "POST" and request.full_url.endswith("/git/tags"):
            return Response(None, raw=b"private-malformed-provider-response")
        return response

    monkeypatch.setattr(case.opener, "open", malformed)
    result = case.invoke()
    value = _diagnostic(result)
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert value["stage"] == "tag_preparation" and value["code"] == "invalid_response"
    assert value["write_may_have_started"] is True
    assert value["preparation_response_observed"] is None and value["preparation_confirmed"] is None
    assert value["preparation_object"] is None and value["atomic_update_acknowledged"] is None
    assert len(case.opener.tags) == 1 and len(case.opener.writes) == 1
    assert case.opener.refs[BASE_REF] == BASE and case.payload["marker_ref"] not in case.opener.refs
    assert "private-malformed" not in json.dumps(_project(case, "invoke", result))


@pytest.mark.parametrize("unknown", ["PRIVATE_GRAPHQL_TYPE", "forbidden", 123, None, {"secret": "private-type"}])
def test_graphql_categories_are_closed_and_unknown_types_never_echo_remote_content(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, unknown: Any
) -> None:
    case = merge_case
    original = case.opener.respond

    def reject(method: str, path: str, body: Any) -> Any:
        if method == "POST" and body and body.get("query") == github.UPDATE_QUERY:
            return {
                "errors": [
                    {"type": "FORBIDDEN", "message": "private-message", "path": ["private-path"]},
                    {"type": unknown, "extensions": {"code": "private-extension"}},
                    {"type": "FORBIDDEN", "message": TOKEN},
                ]
            }
        return original(method, path, body)

    monkeypatch.setattr(case.opener, "respond", reject)
    result = case.invoke()
    value = _diagnostic(result)
    assert result["outcome"] == "uncertain" and result["effect"] is None, value
    assert value["stage"] == "atomic_update" and value["code"] == "graphql_errors"
    assert value["graphql_errors"] == ["forbidden", "unknown"]
    assert value["http_status"] is None and value["atomic_update_acknowledged"] is False
    assert value["preparation_response_observed"] is True and value["preparation_confirmed"] is True
    assert value["preparation_object"] == result["provenance"]["marker_object"]
    assert len(case.opener.writes) == 2 and case.opener.mutations == []
    text = json.dumps(_project(case, "invoke", result))
    for secret in (TOKEN, "private-", "PRIVATE_GRAPHQL_TYPE", '"extensions"', '"message"', '"path"'):
        assert secret not in text


@pytest.mark.parametrize("response", [{}, {"sha": "f" * 40}, {"sha": "private-invalid-sha"}])
def test_preparation_response_and_confirmation_are_separate_observations(merge_case: MergeCase, response: Doc) -> None:
    case = merge_case
    case.opener.overrides[("POST", PREFIX + "/git/tags")] = response
    result = case.invoke()
    value = _diagnostic(result)
    assert result["outcome"] == "uncertain" and value["write_may_have_started"] is True
    assert value["preparation_response_observed"] is True and value["preparation_confirmed"] is False
    expected = response.get("sha") if response.get("sha") == "f" * 40 else None
    assert value["preparation_object"] == result["provenance"]["facts"]["preparation_object"] == expected
    assert value["atomic_update_acknowledged"] is None
    assert len(case.opener.writes) == 1 and case.opener.mutations == []
    retained = copy.deepcopy(result)
    projection = _project(case, "invoke", result)
    assert projection["preparation_object"] is None and projection["diagnostic"]["preparation_object"] is None
    assert result == retained
    assert "private-invalid-sha" not in json.dumps(projection) and "f" * 40 not in json.dumps(projection)


@pytest.mark.parametrize(
    ("damage", "false_predicates"),
    [
        ("marker_reference", {"marker_reference"}),
        ("marker_object_type", TAG_PREDICATES),
        ("marker_object_missing", TAG_PREDICATES),
        ("marker_object_identity", {"marker_object_identity"}),
        ("marker_tag", {"marker_tag"}),
        ("marker_message", {"marker_message"}),
        ("marker_target_type", {"marker_target_type", "marker_target"}),
        ("marker_target", {"marker_target"}),
        ("pull_request_merged", {"pull_request_merged", "merge_revision"}),
        ("merge_revision", {"merge_revision"}),
        ("pull_request_identity_types", {"pull_request_identity_types"}),
        ("head_repository", {"head_repository"}),
        ("head_revision", {"head_revision"}),
        ("head_ref", {"head_ref"}),
        ("base_ref", {"base_ref"}),
        ("merge_commit", {"merge_commit"}),
    ],
)
def test_completed_discovery_retains_independent_comparison_failures_without_extra_reads(
    merge_case: MergeCase, damage: str, false_predicates: set[str]
) -> None:
    case = merge_case
    assert case.invoke()["outcome"] == "applied"
    oid = case.opener.refs[case.payload["marker_ref"]]
    tag = case.opener.tags[oid]
    if damage == "marker_reference":
        del case.opener.refs[case.payload["marker_ref"]]
    elif damage == "marker_object_type":
        case.opener.overrides[("GET", PREFIX + "/git/tags/" + oid)] = []
    elif damage == "marker_object_missing":
        case.opener.overrides[("GET", PREFIX + "/git/tags/" + oid)] = _http_failure(404)
    elif damage == "marker_object_identity":
        tag["sha"] = "f" * 40
    elif damage in {"marker_tag", "marker_message"}:
        tag["tag" if damage == "marker_tag" else "message"] = "private-unmatched-provider-content"
    elif damage == "marker_target_type":
        tag["object"] = []
    elif damage == "marker_target":
        tag["object"]["sha"] = "f" * 40
    elif damage == "pull_request_merged":
        case.opener.pull_request.update(merged=False, state="open")
    elif damage == "merge_revision":
        case.opener.pull_request["merge_commit_sha"] = "f" * 40
    elif damage in {"pull_request_identity_types", "head_repository"}:
        case.opener.pull_request["head"]["repo"]["id"] = 123.0 if damage == "pull_request_identity_types" else 999
    elif damage == "head_revision":
        case.opener.pull_request["head"]["sha"] = "f" * 40
    elif damage in {"head_ref", "base_ref"}:
        case.opener.pull_request["head" if damage == "head_ref" else "base"]["ref"] = "another-branch"
    else:
        case.opener.commit["parents"].reverse()
    calls, writes = len(case.opener.calls), copy.deepcopy(case.opener.writes)
    result = case.reconcile()
    value = _diagnostic(result)
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert result["reason"] == "Exact GitHub merge and authenticated marker are not jointly established; do not resend."
    assert value["stage"] == "applied_predicates" and value["code"] == "applied_unconfirmed"
    assert {key for key, item in value["applied_predicates"].items() if item is False} == false_predicates
    assert {key for key, item in value["applied_predicates"].items() if item is True} == PREDICATES - false_predicates
    assert len(case.opener.calls) - calls == 6 and case.opener.writes == writes
    assert value["write_may_have_started"] is False and result["provenance"]["write_started"] is False
    assert value["preparation_response_observed"] is None and value["atomic_update_acknowledged"] is None
    assert "private-unmatched-provider-content" not in json.dumps(_project(case, "reconcile", result))


@pytest.mark.parametrize(
    ("point", "reached"),
    [
        ("observe_pull_request", set()),
        ("observe_marker_reference", PR_PREDICATES),
        ("observe_marker_object", PR_PREDICATES | {"marker_reference"}),
        ("observe_merge_commit", PR_PREDICATES | {"marker_reference"} | TAG_PREDICATES),
    ],
)
def test_provider_failure_preserves_reached_comparisons_and_leaves_unreached_comparisons_null(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, point: str, reached: set[str]
) -> None:
    case = merge_case
    assert case.invoke()["outcome"] == "applied"
    marker = case.payload["marker_ref"].removeprefix("refs/")
    oid = case.opener.refs[case.payload["marker_ref"]]
    original = case.opener.respond

    def fail(method: str, path: str, body: Any) -> Any:
        selected = (
            (point == "observe_pull_request" and method == "POST" and body.get("query") == github.MERGE_RECEIPT_QUERY)
            or (point == "observe_marker_reference" and path == PREFIX + "/git/ref/" + marker)
            or (point == "observe_marker_object" and path == PREFIX + "/git/tags/" + oid)
            or (point == "observe_merge_commit" and path == PREFIX + "/git/commits/" + MERGE)
        )
        if selected:
            raise _http_failure(502)
        return original(method, path, body)

    monkeypatch.setattr(case.opener, "respond", fail)
    writes = copy.deepcopy(case.opener.writes)
    result = case.reconcile()
    value = _diagnostic(result)
    assert result["outcome"] == "uncertain" and value["stage"] == point
    assert value["code"] == "http_error" and value["http_status"] == 502
    assert {key for key, item in value["applied_predicates"].items() if item is True} == reached
    assert {key for key, item in value["applied_predicates"].items() if item is None} == PREDICATES - reached
    assert case.opener.writes == writes
    _project(case, "reconcile", result)


def test_invoke_and_read_only_reconciliation_keep_independent_diagnostic_observations(merge_case: MergeCase) -> None:
    case = merge_case

    def lost_acknowledgement(provider: Any) -> None:
        raise URLError("private-lost-atomic-acknowledgement")

    case.opener.after_update = lost_acknowledgement
    invoked = case.invoke()
    original = copy.deepcopy(invoked)
    first = _project(case, "invoke", invoked)
    assert invoked["outcome"] == "uncertain" and first["diagnostic"]["stage"] == "atomic_update"
    assert first["diagnostic"]["atomic_update_acknowledged"] is None
    writes = copy.deepcopy(case.opener.writes)
    reconciled = case.reconcile()
    second = _project(case, "reconcile", reconciled)
    assert reconciled["outcome"] == "applied" and second["diagnostic"]["stage"] == "applied_predicates"
    assert all(item is True for item in second["diagnostic"]["applied_predicates"].values())
    assert second["diagnostic"]["atomic_update_acknowledged"] is None
    assert first["provenance_digest"] != second["provenance_digest"]
    assert invoked == original and first["write_started"] is True and second["write_started"] is False
    assert case.opener.writes == writes and len(writes) == 2 and len(case.opener.mutations) == 1


def test_successful_diagnostics_do_not_add_provider_calls_or_change_effect_evidence(merge_case: MergeCase) -> None:
    case = merge_case
    result = case.invoke()
    value = _diagnostic(result)
    assert result["outcome"] == "applied"
    assert value["stage"] == "applied_predicates" and value["code"] == "none"
    assert value["preparation_response_observed"] is True and value["preparation_confirmed"] is True
    assert value["atomic_update_acknowledged"] is True and all(value["applied_predicates"].values())
    assert len(case.opener.calls) == 34 and len(case.opener.writes) == 2 and len(case.opener.mutations) == 1
    assert result["effect"]["remote_receipt_digest"] == delivery.digest(result["provenance"]["facts"])
    forged = copy.deepcopy(result)
    forged["provenance"]["facts"]["marker_verified"] = False
    with pytest.raises(ValueError):
        case.adapter.validate_result("invoke", case.request, case.payload, forged)


@pytest.mark.parametrize("ready", [True, False])
def test_read_only_inspection_has_diagnostics_and_legacy_absence_is_preserved(
    merge_case: MergeCase, ready: bool
) -> None:
    case = merge_case
    if not ready:
        case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    result = case.inspect()
    value = _diagnostic(result)
    assert result["ready"] is ready and value["stage"] == "preflight"
    assert value["code"] == ("none" if ready else "readiness_failed")
    assert value["readiness_check"] == (None if ready else "review_decision")
    assert value["write_may_have_started"] is False
    assert all(item is None for item in value["applied_predicates"].values())
    assert value["preparation_response_observed"] is None and value["atomic_update_acknowledged"] is None
    _project(case, "inspect", result)
    del result["provenance"]["diagnostic"]
    retained = copy.deepcopy(result)
    projection = _project(case, "inspect", result)
    assert projection["diagnostic_available"] is False and projection["diagnostic"] is None
    assert result == retained and case.opener.writes == []


@pytest.mark.parametrize("outcome", ["applied", "rejected", "uncertain"])
def test_legacy_provenance_validation_and_projection_preserve_bytes_digest_and_receipt_identity(
    merge_case: MergeCase, outcome: str
) -> None:
    case = merge_case
    if outcome == "rejected":
        case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    elif outcome == "uncertain":
        case.opener.after_update = lambda provider: provider.pull_request.update(state="open", merged=False)
    result = case.invoke()
    assert result["outcome"] == outcome
    del result["provenance"]["diagnostic"]
    retained = copy.deepcopy(result)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    proof_digest = delivery.digest(result["provenance"])
    native_receipt = receipt(case.intent)
    native_receipt.update(outcome=outcome, effect=result["effect"], provenance_digest=proof_digest)
    identify(native_receipt)
    retained_receipt, receipt_digest = copy.deepcopy(native_receipt), delivery.digest(native_receipt)
    calls = copy.deepcopy(case.opener.calls)
    projection = _project(case, "invoke", result)
    assert projection["diagnostic_available"] is False and projection["diagnostic"] is None
    assert projection["provenance_digest"] == proof_digest
    assert result == retained and "diagnostic" not in result["provenance"]
    assert json.dumps(result, sort_keys=True, separators=(",", ":")).encode() == encoded
    assert delivery.validate_receipt(native_receipt, intent=case.intent) == retained_receipt
    assert delivery.digest(native_receipt) == receipt_digest
    assert case.opener.calls == calls


def test_projection_is_pure_after_credential_and_private_key_are_unavailable(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    result = case.invoke()
    expected = _project(case, "invoke", result)
    retained = copy.deepcopy(result)
    calls = copy.deepcopy(case.opener.calls)
    Path(case.configuration["key"]["path"]).unlink()
    monkeypatch.delenv(TOKEN_ENVIRONMENT)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("projection must not request a credential, key, or provider observation")

    monkeypatch.setattr(case.opener, "open", forbidden)
    monkeypatch.setattr(github, "_credential_now", forbidden)
    assert merge.project_result_diagnostics("invoke", result) == expected
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result
    assert result == retained and case.opener.calls == calls
    expected["diagnostic"]["applied_predicates"]["marker_message"] = False
    assert result == retained
    assert len(json.dumps(merge.project_result_diagnostics("invoke", result))) < 4096


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stage", "private-stage"),
        ("code", "private-code"),
        ("readiness_check", "private-check"),
        ("http_status", True),
        ("http_status", 99),
        ("http_status", 600),
        ("graphql_errors", ["private-provider-category"]),
        ("graphql_errors", ["unknown", "unknown"]),
        ("graphql_errors", ["unknown", "forbidden"]),
        ("write_may_have_started", 1),
        ("preparation_response_observed", 1),
        ("preparation_confirmed", "true"),
        ("preparation_object", "private-path-or-object"),
        ("atomic_update_acknowledged", 0),
        ("applied_predicates", {"marker_message": True}),
    ],
)
def test_closed_diagnostic_fields_reject_unbounded_or_untyped_values(
    merge_case: MergeCase, field: str, value: Any
) -> None:
    case = merge_case
    result = case.invoke()
    result["provenance"]["diagnostic"][field] = value
    with pytest.raises(ValueError):
        merge.validate_diagnostic(result["provenance"]["diagnostic"])
    with pytest.raises(ValueError):
        case.adapter.validate_result("invoke", case.request, case.payload, result)
    with pytest.raises(ValueError):
        merge.project_result_diagnostics("invoke", result)


@pytest.mark.parametrize("damage", ["extra", "missing", "predicate_type", "predicate_extra", "method"])
def test_projection_rejects_nonclosed_diagnostics_and_method_substitution(merge_case: MergeCase, damage: str) -> None:
    result = merge_case.invoke()
    value = result["provenance"]["diagnostic"]
    if damage == "extra":
        value["provider_body"] = "private-body"
    elif damage == "missing":
        del value["http_status"]
    elif damage == "predicate_type":
        value["applied_predicates"]["marker_message"] = 1
    elif damage == "predicate_extra":
        value["applied_predicates"]["private-predicate"] = False
    with pytest.raises(ValueError):
        merge.project_result_diagnostics("reconcile" if damage == "method" else "invoke", result)


@pytest.mark.parametrize("damage", ["prepared_object", "applied_conjunction", "unconfirmed_conjunction", "unreached"])
def test_diagnostics_cannot_contradict_retained_native_observations(merge_case: MergeCase, damage: str) -> None:
    case = merge_case
    if damage == "prepared_object":
        case.opener.overrides[("POST", PREFIX + "/git/tags")] = {"sha": "f" * 40}
    elif damage == "unconfirmed_conjunction":
        case.opener.after_update = lambda provider: provider.pull_request.update(state="open", merged=False)
    result = case.invoke()
    value = _diagnostic(result)
    if damage == "prepared_object":
        value["preparation_object"] = "e" * 40
    elif damage == "applied_conjunction":
        value["applied_predicates"]["marker_message"] = False
    elif damage == "unconfirmed_conjunction":
        value["applied_predicates"] = dict.fromkeys(PREDICATES, True)
    else:
        value["applied_predicates"]["marker_message"] = None
    with pytest.raises(ValueError):
        case.adapter.validate_result("invoke", case.request, case.payload, result)
