"""Bounded GitHub reads for evidence admission, with credentials confined to the API origin."""

from __future__ import annotations

import json
import re
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ORIGIN = "https://api.github.com"
API_VERSION = "2026-03-10"
JSON_LIMIT = 8 * 1024 * 1024
ARCHIVE_LIMIT = 16 * 1024 * 1024
TIMEOUT = 30
MAX_REDIRECTS = 3
SLUG = re.compile(r"[A-Za-z0-9_.-]+")


class GitHubError(Exception):
    """A safe diagnostic that contains neither credentials nor temporary artifact download URLs."""


class NoRedirect(HTTPRedirectHandler):
    """The artifact path handles redirects explicitly after dropping API credentials."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def api_path(path: str) -> str:
    if not isinstance(path, str) or any(ord(char) < 32 or ord(char) == 127 for char in path) or "\\" in path:
        raise GitHubError("invalid GitHub API path")
    try:
        parsed = urlsplit(path)
    except ValueError:
        raise GitHubError("invalid GitHub API path") from None
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/repos/"):
        raise GitHubError("GitHub API paths must be relative repository paths")
    decoded = parsed.path
    for _ in range(8):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        if unquote(decoded) != decoded:
            raise GitHubError("GitHub API path encoding is too deep")
    pieces = decoded.split("/")
    if (
        len(pieces) < 4
        or not SLUG.fullmatch(pieces[2])
        or not SLUG.fullmatch(pieces[3])
        or any(piece in {".", ".."} for piece in pieces)
        or "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
    ):
        raise GitHubError("invalid GitHub repository path")
    return path


def storage_url(location: str | None) -> str:
    if not isinstance(location, str) or any(ord(char) < 32 or ord(char) == 127 for char in location):
        raise GitHubError("GitHub artifact download has an invalid redirect")
    try:
        parsed = urlsplit(location)
        host = parsed.hostname or ""
        allowed = host == "githubusercontent.com" or host.endswith((".githubusercontent.com", ".blob.core.windows.net"))
        if (
            parsed.scheme != "https"
            or not allowed
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or "\\" in location
        ):
            raise GitHubError("GitHub artifact download requires an approved HTTPS storage host")
    except ValueError:
        raise GitHubError("GitHub artifact download has an invalid redirect") from None
    return location


def read_bounded(response: Any, limit: int) -> bytes:
    size = response.headers.get("Content-Length")
    if size is not None:
        try:
            declared = int(size)
        except (TypeError, ValueError):
            raise GitHubError("GitHub response has an invalid content length") from None
        if declared < 0:
            raise GitHubError("GitHub response has an invalid content length")
        if declared > limit:
            raise GitHubError("GitHub response exceeds the permitted size")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not isinstance(chunk, bytes):
            raise GitHubError("GitHub returned an invalid response body")
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise GitHubError("GitHub response exceeds the permitted size")
        chunks.append(chunk)


def json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def no_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


class GitHubClient:
    """Read public GitHub evidence using a token and the inherited proxy and TLS configuration."""

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token or not token.isascii() or any(char.isspace() for char in token):
            raise GitHubError("a nonblank GitHub token without whitespace is required")
        if any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise GitHubError("a nonblank GitHub token without whitespace is required")
        self._token = token
        self._opener = build_opener(NoRedirect())

    def _request(self, url: str, *, authenticated: bool, limit: int) -> tuple[int, str | None, bytes]:
        headers = {"User-Agent": "exacly-evidence"}
        if authenticated:
            if not url.startswith(API_ORIGIN + "/"):
                raise GitHubError("GitHub credentials are restricted to the API origin")
            headers.update(
                {
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                }
            )
        try:
            request = Request(url, headers=headers, method="GET")
            try:
                response = self._opener.open(request, timeout=TIMEOUT)
            except HTTPError as error:
                response = error
            with response:
                status = response.getcode()
                if not isinstance(status, int) or not 100 <= status <= 599:
                    raise GitHubError("GitHub returned an invalid HTTP status")
                if 200 <= status < 300:
                    return status, None, read_bounded(response, limit)
                if status in (301, 302, 303, 307, 308):
                    return status, response.headers.get("Location"), b""
                raise GitHubError(f"GitHub evidence request returned HTTP {status}")
        except GitHubError:
            raise
        except (URLError, OSError, ValueError, HTTPException):
            raise GitHubError("GitHub evidence request failed") from None

    def get_json(self, path: str) -> dict[str, Any]:
        status, _, body = self._request(API_ORIGIN + api_path(path), authenticated=True, limit=JSON_LIMIT)
        if not 200 <= status < 300:
            raise GitHubError("GitHub API redirects are not supported")
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=json_object, parse_constant=no_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise GitHubError("GitHub API returned invalid JSON") from None
        if not isinstance(value, dict):
            raise GitHubError("GitHub API response must be a JSON object")
        return value

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        if not isinstance(repository, str):
            raise GitHubError("invalid GitHub repository")
        parts = repository.split("/")
        if len(parts) != 2 or not all(SLUG.fullmatch(part) and part not in {".", ".."} for part in parts):
            raise GitHubError("invalid GitHub repository")
        if type(artifact_id) is not int or artifact_id < 1:
            raise GitHubError("artifact ID must be a positive integer")
        url = API_ORIGIN + f"/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        authenticated = True
        for count in range(MAX_REDIRECTS + 1):
            status, location, body = self._request(url, authenticated=authenticated, limit=ARCHIVE_LIMIT)
            if 200 <= status < 300:
                return body
            if authenticated and status != 302:
                raise GitHubError("GitHub artifact endpoint returned an unsupported redirect")
            if count == MAX_REDIRECTS:
                raise GitHubError("GitHub artifact download exceeded the redirect limit")
            url = storage_url(location)
            authenticated = False
        raise GitHubError("GitHub artifact download did not complete")
