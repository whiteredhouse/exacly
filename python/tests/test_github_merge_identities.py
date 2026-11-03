"""Current GitHub commit identities remain exact before and after an atomic merge."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from automatic_merge_fixtures import BASE, BASE_REF, HEAD, MERGE, NODE_ID, PREFIX, TOKEN, MergeCase
from automatic_merge_fixtures import merge_case as merge_case
from exacly import github_merge as github

Doc = dict[str, Any]
PRIVATE_TEXT = "untrusted receipt detail: Bearer " + TOKEN


def alter_query(case: MergeCase, monkeypatch: pytest.MonkeyPatch, query: str, change: Callable[[Doc], None]) -> None:
    original = case.opener.respond

    def respond(method: str, path: str, body: Any) -> Any:
        value = original(method, path, body)
        if method == "POST" and path == "/graphql" and body["query"] == query:
            change(value)
        return value

    monkeypatch.setattr(case.opener, "respond", respond)


@pytest.mark.parametrize("legacy", ["absent", "null", "matching"])
def test_preflight_observes_exact_graphql_candidate_when_rest_no_longer_exposes_it(
    merge_case: MergeCase, legacy: str
) -> None:
    case = merge_case
    case.opener.include_legacy_merge_commit_sha = legacy != "absent"
    if legacy == "null":
        case.opener.pull_request["merge_commit_sha"] = None
    raw = case.opener.respond("GET", PREFIX + "/pulls/42", None)
    assert ("merge_commit_sha" in raw) == (legacy != "absent")
    before = copy.deepcopy(case.opener.refs)

    result = case.inspect()

    assert result["ready"] is True
    facts = result["provenance"]["facts"]
    assert facts["pull_request"]["merge_revision"] == MERGE
    assert facts["review"]["potentialMergeCommit"] == {"oid": MERGE}
    assert case.adapter.validate_result("inspect", case.request, case.payload, result) == result
    assert case.opener.refs == before and case.opener.writes == []


@pytest.mark.parametrize("legacy", ["f" * 40, "", 42, False])
def test_conflicting_or_malformed_legacy_rest_identity_is_never_hidden_by_graphql(
    merge_case: MergeCase, legacy: Any
) -> None:
    case = merge_case
    case.opener.include_legacy_merge_commit_sha = True
    case.opener.pull_request["merge_commit_sha"] = legacy

    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == [] and case.opener.refs[BASE_REF] == BASE


@pytest.mark.parametrize("candidate", [None, {}, {"oid": None}, {"oid": "f" * 40}, {"oid": 42}])
def test_missing_or_wrong_graphql_candidate_cannot_be_replaced_by_the_approved_input(
    merge_case: MergeCase, candidate: Any
) -> None:
    case = merge_case
    case.opener.graph["potentialMergeCommit"] = candidate

    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == [] and case.opener.refs[BASE_REF] == BASE


@pytest.mark.parametrize("field", ["repository", "number", "head", "base"])
def test_binding_the_graphql_candidate_does_not_replace_the_rest_pull_request_identity(
    merge_case: MergeCase, field: str
) -> None:
    case = merge_case
    if field == "repository":
        case.opener.pull_request["head"]["repo"]["id"] = 999
    elif field == "number":
        case.opener.pull_request["number"] = 43
    else:
        case.opener.pull_request[field]["sha"] = "f" * 40

    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == []


@pytest.mark.parametrize("field", ["id", "databaseId"])
def test_candidate_graphql_repository_must_match_the_selected_repository(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    case = merge_case

    def change(value: Doc) -> None:
        value["data"]["repository"][field] = "R_other" if field == "id" else 999

    alter_query(case, monkeypatch, github.READ_QUERY, change)
    assert case.inspect()["ready"] is False
    assert case.invoke()["outcome"] == "rejected"
    assert case.opener.writes == []


def test_completed_merge_uses_the_actual_graphql_commit_after_rest_removed_the_field(merge_case: MergeCase) -> None:
    case = merge_case
    assert "merge_commit_sha" not in case.opener.respond("GET", PREFIX + "/pulls/42", None)

    result = case.invoke()

    assert result["outcome"] == "applied" and result["effect"]["commit"] == MERGE
    assert case.adapter.validate_result("invoke", case.request, case.payload, result) == result
    observed = result["provenance"]["facts"]["pull_request"]
    assert observed["merged"] is True and observed["merge_revision"] == MERGE
    assert observed["base_revision"] == MERGE
    queries = [
        call for call in case.opener.calls if call["body"] and call["body"].get("query") == github.MERGE_RECEIPT_QUERY
    ]
    assert len(queries) == 1 and queries[0] not in case.opener.writes
    assert len(case.opener.writes) == 2 and len(case.opener.mutations) == 1
    transaction = next(
        index
        for index, call in enumerate(case.opener.calls)
        if call["body"] and call["body"].get("query") == github.UPDATE_QUERY
    )
    assert all(call["path"] != PREFIX + "/pulls/42" for call in case.opener.calls[transaction + 1 :])

    recovered = case.reconcile()
    assert recovered["outcome"] == "applied" and recovered["effect"]["commit"] == MERGE
    assert len(case.opener.writes) == 2


@pytest.mark.parametrize("damage", ["missing", "null", "wrong", "malformed"])
def test_test_merge_candidate_never_substitutes_for_an_actual_merged_commit(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    case = merge_case

    def change(value: Doc) -> None:
        pr = value["data"]["repository"]["pullRequest"]
        pr["potentialMergeCommit"] = {"oid": MERGE}
        if damage == "missing":
            del pr["mergeCommit"]
        elif damage == "null":
            pr["mergeCommit"] = None
        elif damage == "wrong":
            pr["mergeCommit"] = {"oid": "f" * 40}
        else:
            pr["mergeCommit"] = {"oid": 42}

    alter_query(case, monkeypatch, github.MERGE_RECEIPT_QUERY, change)
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert case.opener.refs[BASE_REF] == MERGE and len(case.opener.writes) == 2
    assert case.reconcile()["outcome"] == "uncertain"
    assert len(case.opener.writes) == 2 and len(case.opener.mutations) == 1


@pytest.mark.parametrize(
    "damage",
    ["node", "database", "number", "head_repository", "head_ref", "base_ref", "head", "state", "merged", "draft-type"],
)
def test_post_effect_receipt_requires_the_actual_scoped_merged_pull_request(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    case = merge_case

    def change(value: Doc) -> None:
        repository = value["data"]["repository"]
        pr = repository["pullRequest"]
        if damage == "node":
            repository["id"] = NODE_ID + "-other"
        elif damage == "database":
            repository["databaseId"] = 999
        elif damage == "number":
            pr["number"] = 43
        elif damage == "head_repository":
            pr["headRepository"] = {"databaseId": 999}
        elif damage == "head_ref":
            pr["headRefName"] = "other"
        elif damage == "base_ref":
            pr["baseRefName"] = "other"
        elif damage == "head":
            pr["headRefOid"] = "f" * 40
        elif damage == "state":
            pr["state"] = "OPEN"
        elif damage == "merged":
            pr["merged"] = False
        else:
            pr["isDraft"] = "false"

    alter_query(case, monkeypatch, github.MERGE_RECEIPT_QUERY, change)
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert case.opener.refs[BASE_REF] == MERGE and len(case.opener.writes) == 2
    assert case.reconcile()["outcome"] == "uncertain" and len(case.opener.writes) == 2


def test_unavailable_graphql_receipt_remains_uncertain_without_leaking_remote_errors(
    merge_case: MergeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = merge_case
    original = case.opener.respond

    def respond(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path == "/graphql" and body["query"] == github.MERGE_RECEIPT_QUERY:
            return {"errors": [{"type": "FORBIDDEN", "message": PRIVATE_TEXT}]}
        return original(method, path, body)

    monkeypatch.setattr(case.opener, "respond", respond)
    result = case.invoke()
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert TOKEN not in str(result) and PRIVATE_TEXT not in str(result)
    assert case.reconcile()["outcome"] == "uncertain" and len(case.opener.writes) == 2


def test_later_base_tip_does_not_replace_the_authenticated_merged_commit(merge_case: MergeCase) -> None:
    case = merge_case
    assert case.invoke()["outcome"] == "applied"
    case.opener.refs[BASE_REF] = "f" * 40
    result = case.reconcile()
    assert result["outcome"] == "applied" and result["effect"]["commit"] == MERGE
    assert result["provenance"]["facts"]["pull_request"]["base_revision"] == "f" * 40
    assert result["provenance"]["facts"]["pull_request"]["head_revision"] == HEAD
    assert len(case.opener.writes) == 2
