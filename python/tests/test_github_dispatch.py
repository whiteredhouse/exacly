"""Controlled issue transport checks: no live tasks or credentials are used."""

from __future__ import annotations

import copy
import io
import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from exacly import github_dispatch as github
from exacly import github_evidence

TOKEN = "dispatch-test-token"
REPOSITORY = {"full_name": "example/project", "id": 17}
MARKER = "<!-- exacly-dispatch:v1 sha256:" + "a" * 64 + " -->"
EFFECT = {"title": "Verify REQ-001", "body": "Reviewed verification work.\n\n" + MARKER}


def issue(number: int = 41, **changes: Any) -> dict[str, Any]:
    return {
        "id": number + 100,
        "number": number,
        "html_url": f"https://github.com/example/project/issues/{number}",
        "created_at": "2026-10-30T09:00:00Z",
        "title": EFFECT["title"],
        "body": EFFECT["body"],
        "state": "open",
        **changes,
    }


class Response:
    def __init__(self, value: Any = None, *, raw: bytes | None = None, status: int = 200, **headers: str) -> None:
        self.body = io.BytesIO(json.dumps(value).encode() if raw is None else raw)
        self.status = status
        self.headers = headers
        self.closed = False

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def getcode(self) -> int:
        return self.status

    def read(self, size: int) -> bytes:
        return self.body.read(size)


class Opener:
    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[Request] = []
        self.timeouts: list[int] = []

    def open(self, request: Request, *, timeout: int) -> Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(
    monkeypatch: pytest.MonkeyPatch, *responses: Response | Exception
) -> tuple[github.GitHubIssueAdapter, Opener]:
    opener = Opener(list(responses))

    def build(*handlers: Any) -> Opener:
        assert len(handlers) == 1 and isinstance(handlers[0], github_evidence.NoRedirect)
        return opener

    monkeypatch.setattr(github, "build_opener", build)
    return github.GitHubIssueAdapter(TOKEN), opener


def link(page: int, relation: str = "next", **changes: str) -> str:
    values = {
        "origin": "https://api.github.com",
        "path": "/repos/example/project/issues",
        "query": f"state=all&sort=created&direction=asc&per_page=100&page={page}",
        **changes,
    }
    return f'<{values["origin"]}{values["path"]}?{values["query"]}>; rel="{relation}"'


def test_repository_identity_and_credentials_are_confined_to_the_fixed_api_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Response({**REPOSITORY, "private": True})
    client, opener = adapter(monkeypatch, response)
    assert client.repository(REPOSITORY) == REPOSITORY
    request = opener.requests[0]
    assert request.full_url == "https://api.github.com/repos/example/project"
    assert request.method == "GET"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == github_evidence.API_VERSION
    assert opener.timeouts == [30]
    assert response.closed


@pytest.mark.parametrize("token", [None, "", " ", "one two", "one\ntwo", "caf\u00e9", "one\x7ftwo", "one\0two"])
def test_invalid_tokens_do_not_create_a_transport(token: Any) -> None:
    with pytest.raises(github.GitHubDispatchError, match="token"):
        github.GitHubIssueAdapter(token)


@pytest.mark.parametrize(
    "repository",
    [
        None,
        {"full_name": "example/project"},
        {**REPOSITORY, "unexpected": True},
        {**REPOSITORY, "id": True},
        {**REPOSITORY, "id": 0},
        {**REPOSITORY, "full_name": "https://evil.example/repo"},
        {**REPOSITORY, "full_name": "example/../secret"},
        {**REPOSITORY, "full_name": "example/%2e%2e"},
        {**REPOSITORY, "full_name": "example/.."},
        {**REPOSITORY, "full_name": "example/project?token=secret"},
    ],
)
def test_invalid_repository_selections_never_make_a_request(monkeypatch: pytest.MonkeyPatch, repository: Any) -> None:
    client, opener = adapter(monkeypatch)
    with pytest.raises(github.GitHubDispatchError):
        client.repository(repository)
    assert not opener.requests


@pytest.mark.parametrize("changed", [{"id": 18}, {"id": True}, {"full_name": "example/moved"}, {"id": "17"}])
def test_live_repository_identity_must_match_reviewed_name_and_id(
    monkeypatch: pytest.MonkeyPatch, changed: dict[str, Any]
) -> None:
    client, opener = adapter(monkeypatch, Response({**REPOSITORY, **changed}))
    with pytest.raises(github.GitHubDispatchError, match="identity"):
        client.create(REPOSITORY, EFFECT)
    assert [request.method for request in opener.requests] == ["GET"]


def test_create_sends_one_reviewed_effect_after_pinning_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener = adapter(monkeypatch, Response(REPOSITORY), Response(issue(), status=201))
    receipt = client.create(REPOSITORY, EFFECT)
    assert receipt == {
        "repository": REPOSITORY,
        "id": 141,
        "number": 41,
        "url": "https://github.com/example/project/issues/41",
        "created_at": "2026-10-30T09:00:00+00:00",
        **EFFECT,
    }
    assert [request.method for request in opener.requests] == ["GET", "POST"]
    request = opener.requests[1]
    assert request.full_url == "https://api.github.com/repos/example/project/issues"
    data = request.data
    assert isinstance(data, bytes)
    assert json.loads(data) == EFFECT
    assert {key.lower(): value for key, value in request.header_items()}["content-type"] == "application/json"


@pytest.mark.parametrize(
    "effect",
    [
        {"title": "x", "body": "no dispatch marker"},
        {**EFFECT, "labels": ["unsafe-extra-effect"]},
        {**EFFECT, "title": ""},
        {**EFFECT, "title": "line\nbreak"},
        {**EFFECT, "title": "x" * 257},
        {**EFFECT, "title": "\ud800"},
        {**EFFECT, "body": "\ud800\n" + MARKER},
        {**EFFECT, "body": "x" * 65536 + MARKER},
        {**EFFECT, "body": MARKER + "\n" + MARKER},
        {**EFFECT, "body": MARKER + " trailing content"},
    ],
)
def test_creation_rejects_unreviewed_or_unbounded_effects_before_network(
    monkeypatch: pytest.MonkeyPatch, effect: Any
) -> None:
    client, opener = adapter(monkeypatch)
    with pytest.raises(github.GitHubDispatchError):
        client.create(REPOSITORY, effect)
    assert not opener.requests


@pytest.mark.parametrize("failure", [URLError("private diagnostic " + TOKEN), TimeoutError(TOKEN), OSError(TOKEN)])
def test_failed_post_is_never_retried_and_diagnostics_do_not_leak_secrets(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    client, opener = adapter(monkeypatch, Response(REPOSITORY), failure)
    with pytest.raises(github.GitHubDispatchError) as raised:
        client.create(REPOSITORY, EFFECT)
    assert TOKEN not in str(raised.value)
    assert [request.method for request in opener.requests] == ["GET", "POST"]


@pytest.mark.parametrize("status", [202, 301, 302, 307, 400, 401, 403, 429, 500, 503])
def test_unconfirmed_create_status_never_follows_redirects_or_retries(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    client, opener = adapter(
        monkeypatch, Response(REPOSITORY), Response({"message": TOKEN}, status=status, Location="https://evil.example")
    )
    with pytest.raises(github.GitHubDispatchError) as raised:
        client.create(REPOSITORY, EFFECT)
    assert TOKEN not in str(raised.value)
    assert [request.method for request in opener.requests] == ["GET", "POST"]


def test_real_urllib_redirect_error_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = Message()
    headers["Location"] = "https://evil.example/private"
    redirect = HTTPError("https://api.github.com/private", 302, TOKEN, headers, io.BytesIO())
    client, opener = adapter(monkeypatch, Response(REPOSITORY), redirect)
    with pytest.raises(github.GitHubDispatchError, match="302"):
        client.create(REPOSITORY, EFFECT)
    assert len(opener.requests) == 2


@pytest.mark.parametrize("raw", [b"[]", b"null", b"\xff", b'{"id":NaN}', b'{"id":17,"id":18}'])
def test_repository_json_must_be_an_unambiguous_object(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    client, _ = adapter(monkeypatch, Response(raw=raw))
    with pytest.raises(github.GitHubDispatchError):
        client.repository(REPOSITORY)


@pytest.mark.parametrize("declared", [True, False])
def test_response_size_is_bounded_with_or_without_content_length(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    monkeypatch.setattr(github, "JSON_LIMIT", 4)
    response = Response(raw=b"12345678")
    if declared:
        response.headers["Content-Length"] = "8"
    client, opener = adapter(monkeypatch, response)
    with pytest.raises(github.GitHubDispatchError, match="bounds"):
        client.repository(REPOSITORY)
    assert len(opener.requests) == 1


def test_discovery_scans_closed_issues_and_all_declared_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener = adapter(
        monkeypatch,
        Response(REPOSITORY),
        Response([issue(body="unrelated")], Link=link(2)),
        Response([issue(42, state="closed")]),
    )
    found = client.discover(REPOSITORY, MARKER, EFFECT)
    assert [receipt["number"] for receipt in found] == [42]
    assert [request.method for request in opener.requests] == ["GET", "GET", "GET"]
    assert opener.requests[1].full_url.endswith("?state=all&sort=created&direction=asc&per_page=100&page=1")
    assert opener.requests[2].full_url.endswith("page=2")


def test_full_page_without_link_header_requires_another_read(monkeypatch: pytest.MonkeyPatch) -> None:
    page = [issue(number, body=None) for number in range(1, 101)]
    client, opener = adapter(monkeypatch, Response(REPOSITORY), Response(page), Response([]))
    assert client.discover(REPOSITORY, MARKER, EFFECT) == []
    assert len(opener.requests) == 3


def test_discovery_retains_multiple_matching_issues_for_explicit_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = adapter(monkeypatch, Response(REPOSITORY), Response([issue(), issue(42)]))
    assert len(client.discover(REPOSITORY, MARKER, EFFECT)) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"body": EFFECT["body"] + " changed"},
        {"title": "different work"},
        {"pull_request": {}},
        {"html_url": "https://evil.example/issues/41"},
        {"created_at": "2026-10-30T09:00:00"},
        {"created_at": "not a timestamp"},
        {"number": True},
    ],
)
def test_marker_with_invalid_or_changed_effect_cannot_be_treated_as_absence(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    client, opener = adapter(monkeypatch, Response(REPOSITORY), Response([issue(**changes)]))
    with pytest.raises(github.GitHubDispatchError):
        client.discover(REPOSITORY, MARKER, EFFECT)
    assert not any(request.method == "POST" for request in opener.requests)


@pytest.mark.parametrize(
    "pagination",
    [
        "garbage",
        link(2, origin="https://evil.example"),
        link(2, origin="http://api.github.com"),
        link(2, path="/repos/example/another/issues"),
        link(2, query="state=all&sort=created&direction=asc&per_page=100&page=2&page=3"),
        link(2, query="state=open&sort=created&direction=asc&per_page=100&page=2"),
        link(3),
        link(1),
        link(2) + ", " + link(2),
        link(2, relation="unknown"),
    ],
)
def test_pagination_cannot_change_destination_skip_pages_or_hide_incomplete_discovery(
    monkeypatch: pytest.MonkeyPatch, pagination: str
) -> None:
    client, opener = adapter(monkeypatch, Response(REPOSITORY), Response([], Link=pagination))
    with pytest.raises(github.GitHubDispatchError):
        client.discover(REPOSITORY, MARKER, EFFECT)
    assert len(opener.requests) == 2


def test_pagination_limits_do_not_establish_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "MAX_PAGES", 1)
    client, opener = adapter(monkeypatch, Response(REPOSITORY), Response([], Link=link(2)))
    with pytest.raises(github.GitHubDispatchError, match="absence is not established"):
        client.discover(REPOSITORY, MARKER, EFFECT)
    assert len(opener.requests) == 2


@pytest.mark.parametrize("page", [{}, None, ["not an issue"], [{"id": True}], [issue(body={})]])
def test_malformed_discovery_never_becomes_an_empty_result(monkeypatch: pytest.MonkeyPatch, page: Any) -> None:
    client, _ = adapter(monkeypatch, Response(REPOSITORY), Response(page))
    with pytest.raises(github.GitHubDispatchError):
        client.discover(REPOSITORY, MARKER, EFFECT)


def test_repeated_page_entries_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    unrelated = issue(body="other task")
    client, _ = adapter(
        monkeypatch, Response(REPOSITORY), Response([unrelated], Link=link(2)), Response([copy.deepcopy(unrelated)])
    )
    with pytest.raises(github.GitHubDispatchError, match="ambiguous"):
        client.discover(REPOSITORY, MARKER, EFFECT)


def test_second_page_failure_does_not_establish_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener = adapter(monkeypatch, Response(REPOSITORY), Response([], Link=link(2)), URLError(TOKEN))
    with pytest.raises(github.GitHubDispatchError) as raised:
        client.discover(REPOSITORY, MARKER, EFFECT)
    assert TOKEN not in str(raised.value)
    assert len(opener.requests) == 3


def test_retained_receipt_validation_is_pure_and_preserves_the_normalized_identity() -> None:
    retained = {
        "repository": REPOSITORY,
        "id": 141,
        "number": 41,
        "url": "https://github.com/example/project/issues/41",
        "created_at": "2026-10-30T09:00:00+00:00",
        **EFFECT,
    }
    assert github.validate_receipt(retained, REPOSITORY, EFFECT) == retained
    result = github.validate_receipt(retained, REPOSITORY, EFFECT)
    result["repository"]["id"] = 99
    assert retained["repository"] == REPOSITORY


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": True},
        {"repository": {**REPOSITORY, "id": 99}},
        {"repository": {**REPOSITORY, "unexpected": True}},
        {"body": "unrelated task"},
        {"url": "https://evil.example/issue/41"},
        {"id": True},
        {"created_at": "2026-10-30"},
    ],
)
def test_retained_receipts_cannot_change_the_approved_effect_or_destination(changes: dict[str, Any]) -> None:
    retained = {
        "repository": REPOSITORY,
        "id": 141,
        "number": 41,
        "url": "https://github.com/example/project/issues/41",
        "created_at": "2026-10-30T09:00:00+00:00",
        **EFFECT,
        **changes,
    }
    with pytest.raises(github.GitHubDispatchError):
        github.validate_receipt(retained, REPOSITORY, EFFECT)
