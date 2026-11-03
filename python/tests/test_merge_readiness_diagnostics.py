"""Readiness failures retain safe local diagnostics and never release a native merge."""

from __future__ import annotations

import copy
import io
import json
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, HEAD, MERGE, PREFIX, TOKEN, TOKEN_ENVIRONMENT, MergeCase, Response
from automatic_merge_fixtures import merge_case as merge_case
from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import delivery_store
from exacly import execution_store as store
from exacly import github_merge as github
from exacly.delivery_engine import DeliveryEngine
from test_merge_required_check_readiness import approve_grant

Doc = dict[str, Any]
PRIVATE_TEXT = "untrusted-provider-message\nAuthorization: Bearer " + TOKEN
PROTECTION = PREFIX + "/branches/main/protection"


def blocked(case: MergeCase, result: Doc, stage: str, code: str, status: int | None = None) -> None:
    suffix = "" if status is None else "; http_status=" + str(status)
    assert result["ready"] is False
    assert result["reason"] == f"GitHub merge readiness blocked: stage={stage}; code={code}{suffix}."
    assert result["provenance"]["facts"] == {"blocked": True}
    assert case.adapter.validate_result("inspect", case.request, case.payload, result) == result
    assert case.opener.writes == [] and case.opener.mutations == []
    assert case.opener.refs[BASE_REF] == BASE
    encoded = json.dumps(result)
    assert TOKEN not in encoded and PRIVATE_TEXT not in encoded and "Bearer" not in encoded


@pytest.mark.parametrize("status", [401, 403, 429, 500])
@pytest.mark.parametrize(
    ("path", "stage"),
    [
        (PREFIX, "repository"),
        ("/installation/repositories?per_page=100", "installation_scope"),
        (PROTECTION, "protection"),
    ],
)
def test_http_failure_identifies_only_fixed_stage_and_numeric_status(
    merge_case: MergeCase, path: str, stage: str, status: int
) -> None:
    headers = Message()
    headers["Authorization"] = PRIVATE_TEXT.replace("\n", " ")
    merge_case.opener.overrides[("GET", path)] = HTTPError(
        github.API + path, status, PRIVATE_TEXT, headers, io.BytesIO(PRIVATE_TEXT.encode())
    )
    blocked(merge_case, merge_case.inspect(), stage, "http_error", status)


@pytest.mark.parametrize("status", [True, "403", 99, 600])
def test_untrusted_http_status_does_not_expand_the_diagnostic(merge_case: MergeCase, status: Any) -> None:
    merge_case.opener.overrides[("GET", PROTECTION)] = HTTPError(
        github.API + PROTECTION, status, PRIVATE_TEXT, Message(), io.BytesIO(PRIVATE_TEXT.encode())
    )
    blocked(merge_case, merge_case.inspect(), "protection", "http_error")


def test_transport_failure_does_not_echo_its_private_exception_text(merge_case: MergeCase) -> None:
    merge_case.opener.overrides[("GET", PROTECTION)] = URLError(PRIVATE_TEXT)
    blocked(merge_case, merge_case.inspect(), "protection", "transport_error")


@pytest.mark.parametrize("raw", [b'{"private": "not retained"', b'{"id":1,"id":2}', b'{"id":NaN}', b"\xff"])
def test_malformed_provider_response_is_distinct_from_a_policy_rejection(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    original = merge_case.opener.open

    def opened(request: Request, *, timeout: float) -> Response:
        response = original(request, timeout=timeout)
        if request.full_url == github.API + PROTECTION:
            response.close()
            return Response(None, raw=raw)
        return response

    monkeypatch.setattr(merge_case.opener, "open", opened)
    blocked(merge_case, merge_case.inspect(), "protection", "invalid_response")


@pytest.mark.parametrize("damage", ["missing-key", "missing-token", "wrong-token"])
def test_local_identity_failures_stop_before_provider_access(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    if damage == "missing-key":
        Path(merge_case.configuration["key"]["path"]).unlink()
        stage, code = "key", "local_io_error"
    else:
        if damage == "missing-token":
            monkeypatch.delenv(TOKEN_ENVIRONMENT)
        else:
            monkeypatch.setenv(TOKEN_ENVIRONMENT, PRIVATE_TEXT)
        stage, code = "credential", "validation_failed"
    blocked(merge_case, merge_case.inspect(), stage, code)
    assert merge_case.opener.calls == []


def test_expired_call_deadline_is_diagnosable_without_starting_http(merge_case: MergeCase) -> None:
    result = merge_case.adapter.inspect(merge_case.request, merge_case.payload, deadline=store.Deadline(0))
    blocked(merge_case, result, "key", "deadline_or_storage")
    assert merge_case.opener.calls == []


@pytest.mark.parametrize(("query_name", "stage"), [("READ_QUERY", "review"), ("UNSTABLE_QUERY", "branch_conditions")])
def test_graphql_failures_do_not_retain_remote_errors_or_release_a_merge(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, query_name: str, stage: str
) -> None:
    case = merge_case
    approve_grant(case, monkeypatch)
    case.opener.graph["mergeStateStatus"] = "UNSTABLE"
    original = case.opener.respond

    def respond(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path == "/graphql" and body["query"] == getattr(github, query_name):
            return {"errors": [{"type": "FORBIDDEN", "message": PRIVATE_TEXT}]}
        return original(method, path, body)

    monkeypatch.setattr(case.opener, "respond", respond)
    blocked(case, case.inspect(), stage, "validation_failed")


@pytest.mark.parametrize(("revision", "stage"), [(HEAD, "legacy_head_status"), (MERGE, "legacy_merge_status")])
def test_unstable_status_permission_failure_identifies_the_selected_read(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, revision: str, stage: str
) -> None:
    case = merge_case
    approve_grant(case, monkeypatch)
    case.opener.graph["mergeStateStatus"] = "UNSTABLE"
    path = PREFIX + "/commits/" + revision + "/status?per_page=100"
    case.opener.overrides[("GET", path)] = HTTPError(
        github.API + path, 403, PRIVATE_TEXT, Message(), io.BytesIO(PRIVATE_TEXT.encode())
    )
    blocked(case, case.inspect(), stage, "http_error", 403)


@pytest.mark.parametrize(
    ("damage", "stage", "code"),
    [
        ("review", "ready_facts", "review_decision"),
        ("check", "ready_facts", "required_check_conclusion"),
        ("protection", "protection", "validation_failed"),
        ("contract", "ready_facts", "contract_inventory"),
        ("base-ref", "ready_facts", "base_reference"),
    ],
)
def test_fixed_diagnostics_preserve_every_current_readiness_guard(
    merge_case: MergeCase, damage: str, stage: str, code: str
) -> None:
    case = merge_case
    if damage == "review":
        case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    elif damage == "check":
        case.opener.check_runs[0]["conclusion"] = "failure"
    elif damage == "protection":
        case.opener.protection["enforce_admins"]["enabled"] = False
    elif damage == "contract":
        case.opener.trees[HEAD][0]["sha"] = "f" * 40
    else:
        case.opener.refs[BASE_REF] = "f" * 40
    refs = copy.deepcopy(case.opener.refs)
    result = case.inspect()
    # This case intentionally starts with a drifted base; inspection must leave it alone.
    assert result["ready"] is False
    assert result["reason"] == f"GitHub merge readiness blocked: stage={stage}; code={code}."
    assert case.adapter.validate_result("inspect", case.request, case.payload, result) == result
    assert case.opener.refs == refs and case.opener.writes == []
    assert case.invoke()["outcome"] == "rejected" and case.opener.writes == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("reviewDecision", "REVIEW_REQUIRED", "review_decision"),
        ("mergeStateStatus", "UNKNOWN", "merge_state"),
        ("mergeable", "UNKNOWN", "mergeability"),
        ("isMergeQueueEnabled", True, "merge_queue_setting"),
        ("isMergeQueueEnabled", 0, "merge_queue_setting"),
        ("isInMergeQueue", True, "queue_membership"),
        ("isInMergeQueue", 0, "queue_membership"),
        ("isCrossRepository", True, "repository_relationship"),
        ("isCrossRepository", 0, "repository_relationship"),
        ("headRefOid", "f" * 40, "review_head"),
        ("baseRefOid", "f" * 40, "review_base"),
    ],
)
def test_each_rejected_review_condition_has_its_own_fixed_identifier(
    merge_case: MergeCase, field: str, value: Any, code: str
) -> None:
    merge_case.opener.graph[field] = value
    blocked(merge_case, merge_case.inspect(), "ready_facts", code)
    assert merge_case.invoke()["outcome"] == "rejected" and merge_case.opener.writes == []


@pytest.mark.parametrize(("field", "code"), [("draft", "pr_draft"), ("merged", "pr_merged")])
def test_numeric_false_cannot_satisfy_a_required_boolean_pr_fact(merge_case: MergeCase, field: str, code: str) -> None:
    merge_case.opener.pull_request[field] = 0
    blocked(merge_case, merge_case.inspect(), "ready_facts", code)


def test_fixed_identifiers_preserve_first_failure_order_and_do_not_grant_an_invoke(merge_case: MergeCase) -> None:
    case = merge_case
    case.opener.pull_request["number"] = 43
    case.opener.graph.update(reviewDecision="REVIEW_REQUIRED", mergeable="UNKNOWN")
    blocked(case, case.inspect(), "ready_facts", "pr_number")
    case.opener.pull_request["number"] = 42
    blocked(case, case.inspect(), "ready_facts", "review_decision")
    case.opener.graph["reviewDecision"] = "APPROVED"
    blocked(case, case.inspect(), "ready_facts", "mergeability")
    case.opener.graph["mergeable"] = "MERGEABLE"
    assert case.inspect()["ready"] is True
    assert case.opener.writes == [] and case.opener.refs[BASE_REF] == BASE


def test_typed_rule_failure_keeps_the_existing_value_error_message_offline(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    facts = copy.deepcopy(case.inspect()["provenance"]["facts"])
    facts["review"]["reviewDecision"] = "REVIEW_REQUIRED"
    before = copy.deepcopy(case.opener.calls)
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    Path(case.configuration["key"]["path"]).unlink()
    with pytest.raises(
        ValueError, match="current independent reviews, checks or supported merge path are missing"
    ) as error:
        merge.validate_ready_facts(case.request, case.payload, case.configuration, case.policy, facts)
    assert type(error.value) is merge.ReadinessFailure
    assert error.value.check is merge.ReadinessCheck.REVIEW_DECISION
    assert error.value.args == ("current independent reviews, checks or supported merge path are missing",)
    assert case.opener.calls == before and case.opener.writes == []


@pytest.mark.parametrize("kind", ["foreign", "exact", "subclass", "corrupted", "missing-tag"])
def test_only_the_exact_installed_failure_and_enum_can_select_a_rule_code(kind: str) -> None:
    class ForeignFailure(ValueError):
        def __str__(self) -> str:
            raise AssertionError("diagnostic formatter inspected exception text")

    class SubclassedFailure(merge.ReadinessFailure):
        def __str__(self) -> str:
            raise AssertionError("diagnostic formatter inspected subclass exception text")

    selected = merge.ReadinessCheck.REVIEW_DECISION
    error: ValueError
    if kind == "foreign":
        error = ForeignFailure(PRIVATE_TEXT)
    elif kind == "subclass":
        error = SubclassedFailure(PRIVATE_TEXT, check=selected)
    elif kind == "missing-tag":
        error = merge.ReadinessFailure(PRIVATE_TEXT)
    else:
        error = merge.ReadinessFailure(PRIVATE_TEXT, check=selected)
        if kind == "corrupted":
            object.__setattr__(error, "check", PRIVATE_TEXT)
    expected = selected.value if kind == "exact" else "validation_failed"
    progress = github._InspectionProgress(stage=github._InspectionStage.READY_FACTS)
    reason = progress.blocked_reason(error)
    assert reason == f"GitHub merge readiness blocked: stage=ready_facts; code={expected}."
    assert TOKEN not in reason and PRIVATE_TEXT not in reason and "Bearer" not in reason


def test_transport_exceptions_cannot_masquerade_as_trusted_rule_failures(merge_case: MergeCase) -> None:
    merge_case.opener.overrides[("GET", PREFIX)] = merge.ReadinessFailure(
        PRIVATE_TEXT, check=merge.ReadinessCheck.REVIEW_DECISION
    )
    blocked(merge_case, merge_case.inspect(), "repository", "invalid_response")


def test_expected_absent_operation_marker_is_not_a_readiness_failure(merge_case: MergeCase) -> None:
    before = copy.deepcopy(merge_case.opener.refs)
    result = merge_case.inspect()
    assert result["ready"] is True
    assert result["reason"] == "Exact protected GitHub merge is currently ready; reads are not atomic."
    assert merge_case.payload["marker_ref"] not in merge_case.opener.refs
    assert merge_case.opener.refs == before and merge_case.opener.writes == []


def test_each_inspection_has_its_own_stage_and_http_status(merge_case: MergeCase) -> None:
    case = merge_case
    checks = PREFIX + "/commits/" + MERGE + "/check-runs?filter=latest&per_page=100"
    for path, stage in ((PROTECTION, "protection"), (checks, "checks")):
        case.opener.overrides[("GET", path)] = HTTPError(
            github.API + path, 403, PRIVATE_TEXT, Message(), io.BytesIO(PRIVATE_TEXT.encode())
        )
        blocked(case, case.inspect(), stage, "http_error", 403)
        del case.opener.overrides[("GET", path)]
    result = case.inspect()
    assert result["ready"] is True
    assert result["reason"] == "Exact protected GitHub merge is currently ready; reads are not atomic."
    assert case.payload["marker_ref"] not in case.opener.refs and case.opener.writes == []


def test_old_generic_blocked_proof_validates_offline_without_new_credentials(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    case.opener.graph["reviewDecision"] = "CHANGES_REQUESTED"
    historical = case.inspect()
    historical["reason"] = "Delivery credentials, key or current exact protected merge conditions block."
    calls = copy.deepcopy(case.opener.calls)
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    Path(case.configuration["key"]["path"]).unlink()
    assert case.adapter.validate_result("inspect", case.request, case.payload, historical) == historical
    assert historical["ready"] is False and historical["provenance"]["facts"] == {"blocked": True}
    assert case.opener.calls == calls and case.opener.writes == []


@pytest.mark.parametrize("failure", ["http", "rule"])
def test_actual_engine_retains_the_safe_failure_and_never_reserves_an_invoke(
    merge_case: MergeCase, tmp_path: Path, failure: str
) -> None:
    case = merge_case
    private = tmp_path / "retained-blocked-inspection"
    private.mkdir(mode=0o700)
    path = private / "delivery.sqlite3"
    case.authorization["limits"] = {"operations": 1, "wall_time_ms": 360000, "reads": 192, "rollbacks": 0}
    metadata = delivery_store.initialize(
        path,
        objective=case.request["objective"],
        limits=case.authorization["limits"],
        native_limits=case.adapter.call_limits["invoke"],
    )
    case.authorization["journal"] = {"path": str(path), "id": metadata["id"]}
    pin = delivery.ApprovalPin(
        case.authorization["issuer"], case.authorization["approval_id"], delivery.digest(case.authorization)
    )
    if failure == "http":
        case.opener.overrides[("GET", PROTECTION)] = HTTPError(
            github.API + PROTECTION, 403, PRIVATE_TEXT, Message(), io.BytesIO(PRIVATE_TEXT.encode())
        )
        expected_reason = "GitHub merge readiness blocked: stage=protection; code=http_error; http_status=403."
    else:
        case.opener.graph["reviewDecision"] = "REVIEW_REQUIRED"
        expected_reason = "GitHub merge readiness blocked: stage=ready_facts; code=review_decision."
    engine = DeliveryEngine(path, journal_id=metadata["id"], adapter_factory=lambda: case.adapter)
    report = engine.apply(case.request, case.payload, approval=case.authorization, pin=pin)
    assert report["status"] == "prepared" and report["live_checked"] is True
    assert report["reason"] == expected_reason
    assert report["receipt"] is None
    saved = delivery_store.show(path, metadata["id"], case.request["operation_key"])["operation"]
    assert saved["phase"] == "prepared" and saved["receipt"] is None
    assert [call["method"] for call in saved["calls"]] == ["inspect"]
    assert saved["calls"][0]["bounds"]["write_calls"] == 0
    assert saved["calls"][0]["result"]["provenance"]["write_started"] is False
    assert TOKEN not in json.dumps(saved) and PRIVATE_TEXT not in json.dumps(report)
    assert case.opener.refs[BASE_REF] == BASE and case.opener.writes == []
