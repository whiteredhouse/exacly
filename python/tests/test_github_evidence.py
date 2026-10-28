"""Evidence transport confines credentials, validates destinations and bounds untrusted responses."""

import io
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from exacly import github_evidence as github

TOKEN = "transport-test-token"
STORAGE = "https://results.blob.core.windows.net/artifacts/test.zip?signature=private-value"


class Response:
    def __init__(self, body: bytes = b"", status: int = 200, **headers: str) -> None:
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers
        self.closed = False
        self.reads: list[int] = []

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int) -> bytes:
        self.reads.append(amount)
        return self.body.read(amount)


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


def client(monkeypatch: pytest.MonkeyPatch, *responses: Response | Exception) -> tuple[github.GitHubClient, Opener]:
    opener = Opener(list(responses))

    def build(*handlers: Any) -> Opener:
        assert len(handlers) == 1 and isinstance(handlers[0], github.NoRedirect)
        return opener

    monkeypatch.setattr(github, "build_opener", build)
    return github.GitHubClient(TOKEN), opener


def headers(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def test_json_request_uses_fixed_api_origin_headers_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response(b'{"id": 7, "nested": {"value": true}}')
    transport, opener = client(monkeypatch, response)
    assert transport.get_json("/repos/owner/.github/actions/runs/7?per_page=100&page=1") == {
        "id": 7,
        "nested": {"value": True},
    }
    assert opener.requests[0].full_url == github.API_ORIGIN + "/repos/owner/.github/actions/runs/7?per_page=100&page=1"
    assert opener.requests[0].method == "GET"
    assert headers(opener.requests[0])["authorization"] == f"Bearer {TOKEN}"
    assert headers(opener.requests[0])["accept"] == "application/vnd.github+json"
    assert headers(opener.requests[0])["x-github-api-version"] == github.API_VERSION
    assert opener.timeouts == [30]
    assert response.closed


@pytest.mark.parametrize("token", [None, "", " ", "one\ntwo", "one two", "caf\u00e9", "one\x7ftwo"])
def test_invalid_credentials_never_become_request_headers(token: Any) -> None:
    with pytest.raises(github.GitHubError, match="token"):
        github.GitHubClient(token)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/repos/o/r",
        "https://api.github.com/repos/o/r",
        "//evil.example/repos/o/r",
        "//[invalid",
        "/users/example",
        "/repos/o/r/../secret",
        "/repos/o/r/%2e%2e/secret",
        "/repos/o/r/%252e%252e/secret",
        "/repos/o/r/%2525252525252525252e%2525252525252525252e/secret",
        "/repos/o/r/%5csecret",
        "/repos/o/r/%0asecret",
        "/repos/o/r?query=one\r\ntwo",
        "/repos/o/r#fragment",
        "/repos//r",
    ],
)
def test_api_paths_cannot_change_origin_or_traverse_paths(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    transport, opener = client(monkeypatch)
    with pytest.raises(github.GitHubError):
        transport.get_json(path)
    assert opener.requests == []


@pytest.mark.parametrize(
    "body", [b"not JSON", b"[]", b"null", b"true", b'"text"', b"\xff", b'{"x":NaN}', b'{"x":1,"x":2}']
)
def test_json_must_be_an_unambiguous_object(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    transport, _ = client(monkeypatch, Response(body))
    with pytest.raises(github.GitHubError, match="JSON"):
        transport.get_json("/repos/o/r")


def test_json_api_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, opener = client(monkeypatch, Response(status=302, Location=STORAGE))
    with pytest.raises(github.GitHubError, match="redirect"):
        transport.get_json("/repos/o/r")
    assert len(opener.requests) == 1


def test_artifact_redirect_drops_api_credentials_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, opener = client(monkeypatch, Response(status=302, Location=STORAGE), Response(b"archive"))
    assert transport.download_artifact("owner/repo", 9) == b"archive"
    api, storage = opener.requests
    assert api.full_url == "https://api.github.com/repos/owner/repo/actions/artifacts/9/zip"
    assert headers(api)["authorization"] == f"Bearer {TOKEN}"
    assert storage.full_url == STORAGE
    assert "authorization" not in headers(storage)
    assert "accept" not in headers(storage)
    assert "x-github-api-version" not in headers(storage)
    assert opener.timeouts == [30, 30]


def test_artifact_handles_real_urllib_redirect_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    redirect_headers = Message()
    redirect_headers["Location"] = STORAGE
    redirect = HTTPError("https://api.github.com/private", 302, "Found", redirect_headers, io.BytesIO())
    transport, opener = client(monkeypatch, redirect, Response(b"zip"))
    assert transport.download_artifact("o/r", 1) == b"zip"
    assert "authorization" not in headers(opener.requests[1])


def test_every_storage_redirect_remains_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, opener = client(
        monkeypatch,
        Response(status=302, Location=STORAGE),
        Response(status=307, Location="https://results.actions.githubusercontent.com/archive"),
        Response(b"archive"),
    )
    assert transport.download_artifact("o/r", 1) == b"archive"
    assert all("authorization" not in headers(request) for request in opener.requests[1:])


@pytest.mark.parametrize(
    "location",
    [
        "http://results.blob.core.windows.net/archive",
        "https://localhost/archive",
        "https://127.0.0.1/archive",
        "https://results.blob.core.windows.net.evil.example/archive",
        "https://evil.example/archive",
        "https://user:password@results.blob.core.windows.net/archive",
        "https://results.blob.core.windows.net:8080/archive",
        "https://results.blob.core.windows.net/archive#fragment",
        "https://results.blob.core.windows.net/one\ntwo",
        "/relative/archive",
        "//results.blob.core.windows.net/archive",
    ],
)
def test_artifact_redirects_require_approved_https_storage(monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    transport, opener = client(monkeypatch, Response(status=302, Location=location))
    with pytest.raises(github.GitHubError):
        transport.download_artifact("o/r", 1)
    assert len(opener.requests) == 1


def test_artifact_redirect_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, opener = client(monkeypatch, *(Response(status=302, Location=STORAGE) for _ in range(4)))
    with pytest.raises(github.GitHubError, match="redirect limit"):
        transport.download_artifact("o/r", 1)
    assert len(opener.requests) == 4


def test_artifact_api_requires_its_documented_redirect_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, opener = client(monkeypatch, Response(status=301, Location=STORAGE))
    with pytest.raises(github.GitHubError, match="unsupported redirect"):
        transport.download_artifact("o/r", 1)
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "repository,artifact_id", [("o/r/extra", 1), ("o/..", 1), ("https://github.com/o/r", 1), ("o/r", 0), ("o/r", True)]
)
def test_artifact_request_parameters_are_validated(
    monkeypatch: pytest.MonkeyPatch, repository: str, artifact_id: Any
) -> None:
    transport, opener = client(monkeypatch)
    with pytest.raises(github.GitHubError):
        transport.download_artifact(repository, artifact_id)
    assert opener.requests == []


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("archive", [True, False])
def test_responses_are_bounded_with_and_without_content_length(
    monkeypatch: pytest.MonkeyPatch, declared: bool, archive: bool
) -> None:
    monkeypatch.setattr(github, "JSON_LIMIT", 8)
    monkeypatch.setattr(github, "ARCHIVE_LIMIT", 8)
    response = Response(b"123456789")
    if declared:
        response.headers["Content-Length"] = "9"
    transport, _ = client(monkeypatch, response)
    with pytest.raises(github.GitHubError, match="size"):
        if archive:
            transport.download_artifact("o/r", 1)
        else:
            transport.get_json("/repos/o/r")
    assert response.closed
    assert response.reads == ([] if declared else [9])


@pytest.mark.parametrize("length", ["not-a-number", "-1"])
def test_invalid_content_length_is_rejected(monkeypatch: pytest.MonkeyPatch, length: str) -> None:
    response = Response(b"{}")
    response.headers["Content-Length"] = length
    transport, _ = client(monkeypatch, response)
    with pytest.raises(github.GitHubError, match="content length"):
        transport.get_json("/repos/o/r")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_errors_are_safe_diagnostics(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    transport, _ = client(monkeypatch, Response(f"{TOKEN} {STORAGE}".encode(), status=status))
    with pytest.raises(github.GitHubError, match=f"HTTP {status}") as error:
        transport.get_json("/repos/o/r")
    assert TOKEN not in str(error.value)
    assert "signature" not in str(error.value)


def test_network_errors_do_not_disclose_credentials_or_storage_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _ = client(monkeypatch, Response(status=302, Location=STORAGE), URLError(f"{TOKEN} {STORAGE}"))
    with pytest.raises(github.GitHubError, match="request failed") as error:
        transport.download_artifact("o/r", 1)
    assert TOKEN not in str(error.value)
    assert "signature" not in str(error.value)
    assert error.value.__suppress_context__


def test_stream_errors_are_sanitized_and_close_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenResponse(Response):
        def read(self, amount: int) -> bytes:
            raise OSError(f"{TOKEN} {STORAGE}")

    response = BrokenResponse()
    transport, _ = client(monkeypatch, Response(status=302, Location=STORAGE), response)
    with pytest.raises(github.GitHubError, match="request failed") as error:
        transport.download_artifact("o/r", 1)
    assert TOKEN not in str(error.value) and "signature" not in str(error.value)
    assert response.closed


def test_an_exact_limit_response_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "JSON_LIMIT", 2)
    response = Response(b"{}")
    transport, _ = client(monkeypatch, response)
    assert transport.get_json("/repos/o/r") == {}
    assert response.closed
