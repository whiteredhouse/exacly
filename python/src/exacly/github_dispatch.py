"""Bounded GitHub issue discovery and one-shot creation for an authorized dispatch.

The coordinator records its durable create intent before calling this adapter. This transport never
retries a POST: a missing response cannot establish that GitHub did not create the issue.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, build_opener

from exacly.github_evidence import (
    API_ORIGIN,
    API_VERSION,
    SLUG,
    GitHubError,
    NoRedirect,
    json_object,
    no_constant,
    read_bounded,
)

Doc = dict[str, Any]
TIMEOUT = 30
JSON_LIMIT = 2 * 1024 * 1024
MAX_PAGES = 100
PAGE_SIZE = 100
MAX_BODY_BYTES = 64 * 1024
MAX_TITLE_BYTES = 256
_MARKER = re.compile(r"<!-- exacly-dispatch:v1 sha256:[0-9a-f]{64} -->")
_LINK = re.compile(r'<([^<>]+)>;\s*rel="([a-z]+)"')


class GitHubDispatchError(GitHubError):
    """A safe diagnostic without credentials, response bodies or private API details."""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise GitHubDispatchError(message)


def _repository(value: Any) -> Doc:
    _require(isinstance(value, dict) and set(value) == {"full_name", "id"}, "invalid dispatch repository")
    name, identity = value["full_name"], value["id"]
    _require(isinstance(name, str), "invalid dispatch repository name")
    pieces = name.split("/")
    _require(
        len(pieces) == 2 and all(SLUG.fullmatch(piece) and piece not in {".", ".."} for piece in pieces),
        "invalid dispatch repository name",
    )
    _require(type(identity) is int and identity > 0, "dispatch repository ID must be positive")
    return {"full_name": name, "id": identity}


def _effect(value: Any) -> Doc:
    _require(isinstance(value, dict) and set(value) == {"title", "body"}, "invalid GitHub issue effect")
    title, body = value["title"], value["body"]
    _require(
        isinstance(title, str)
        and bool(title.strip())
        and _utf8_length(title) <= MAX_TITLE_BYTES
        and not any(ord(char) < 32 or ord(char) == 127 for char in title),
        "invalid GitHub issue title",
    )
    _require(
        isinstance(body, str) and "\0" not in body and _utf8_length(body) <= MAX_BODY_BYTES,
        "invalid GitHub issue body",
    )
    markers = list(_MARKER.finditer(body))
    _require(len(markers) == 1 and markers[0].end() == len(body), "issue effect needs one final dispatch marker")
    return {"title": title, "body": body}


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise GitHubDispatchError("GitHub issue text must be valid UTF-8") from None


def validate_effect(value: Any) -> Doc:
    """Check the exact issue effect without credentials, transport or side effects."""
    return _effect(value)


def _receipt(value: Any, repository: Doc, effect: Doc) -> Doc:
    _require(isinstance(value, dict) and "pull_request" not in value, "dispatch marker identifies an invalid issue")
    identity, number = value.get("id"), value.get("number")
    _require(
        type(identity) is int and identity > 0 and type(number) is int and number > 0, "invalid GitHub issue identity"
    )
    _require(
        value.get("title") == effect["title"] and value.get("body") == effect["body"],
        "dispatch marker conflicts with the reviewed issue effect",
    )
    url = f"https://github.com/{repository['full_name']}/issues/{number}"
    _require(value.get("html_url") == url, "GitHub issue URL differs from the reviewed repository")
    created = value.get("created_at")
    _require(isinstance(created, str) and len(created) <= 64, "invalid GitHub issue creation timestamp")
    try:
        stamp = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        _require(stamp.utcoffset() is not None, "GitHub issue creation timestamp needs a timezone")
        normalized = stamp.astimezone(dt.UTC).isoformat()
    except (ValueError, OverflowError):
        raise GitHubDispatchError("invalid GitHub issue creation timestamp") from None
    return {
        "repository": dict(repository),
        "id": identity,
        "number": number,
        "url": url,
        "created_at": normalized,
        "title": effect["title"],
        "body": effect["body"],
    }


def validate_receipt(value: Any, repository: Doc, effect: Doc) -> Doc:
    """Recheck a normalized retained or adapter-provided receipt without remote access."""
    reviewed, desired = _repository(repository), _effect(effect)
    _require(
        isinstance(value, dict) and set(value) == {"repository", "id", "number", "url", "created_at", "title", "body"},
        "invalid retained GitHub issue receipt",
    )
    _require(
        _repository(value["repository"]) == reviewed, "retained issue receipt differs from the reviewed repository"
    )
    return _receipt(
        {
            **{key: value[key] for key in ("id", "number", "created_at", "title", "body")},
            "html_url": value["url"],
        },
        reviewed,
        desired,
    )


def _next_page(link: str | None, repository: Doc, page: int) -> bool:
    """Validate pagination hints without ever requesting a server-supplied URL."""
    if link is None:
        return False
    _require(isinstance(link, str) and len(link) <= 8192, "invalid GitHub issue pagination")
    entries = link.split(",")
    seen: set[str] = set()
    following = False
    for entry in entries:
        match = _LINK.fullmatch(entry.strip())
        _require(match is not None, "invalid GitHub issue pagination")
        assert match is not None
        destination, relation = match.groups()
        _require(
            relation in {"first", "last", "prev", "next"} and relation not in seen, "invalid GitHub issue pagination"
        )
        seen.add(relation)
        try:
            parsed = urlsplit(destination)
        except ValueError:
            raise GitHubDispatchError("invalid GitHub issue pagination destination") from None
        query = parse_qsl(parsed.query, keep_blank_values=True)
        expected = {"state": "all", "sort": "created", "direction": "asc", "per_page": str(PAGE_SIZE)}
        _require(
            parsed.scheme == "https"
            and parsed.netloc == "api.github.com"
            and parsed.path == f"/repos/{repository['full_name']}/issues"
            and not parsed.fragment
            and len(query) == len(expected) + 1
            and len(dict(query)) == len(query)
            and all(dict(query).get(key) == value for key, value in expected.items()),
            "GitHub issue pagination changed the reviewed destination",
        )
        number = dict(query).get("page", "")
        _require(number.isascii() and number.isdecimal() and int(number) > 0, "invalid GitHub issue pagination page")
        if relation == "next":
            _require(int(number) == page + 1, "GitHub issue pagination skipped or repeated a page")
            following = True
    return following


class GitHubIssueAdapter:
    """Use only the fixed API origin, with bounded reads and a single creation request."""

    def __init__(self, token: str) -> None:
        _require(
            isinstance(token, str)
            and bool(token)
            and token.isascii()
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token),
            "a nonblank GitHub token without whitespace is required",
        )
        self._token = token
        self._opener = build_opener(NoRedirect())

    def _request(self, path: str, *, effect: Doc | None = None) -> tuple[Any, str | None]:
        headers = {
            "User-Agent": "exacly-dispatch",
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        data = None
        if effect is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(effect, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        try:
            request = Request(
                API_ORIGIN + path, headers=headers, data=data, method="POST" if effect is not None else "GET"
            )
            try:
                response = self._opener.open(request, timeout=TIMEOUT)
            except HTTPError as error:
                response = error
            with response:
                status = response.getcode()
                _require(type(status) is int, "GitHub returned an invalid HTTP status")
                _require(
                    status == (201 if effect is not None else 200), f"GitHub dispatch request returned HTTP {status}"
                )
                body = read_bounded(response, JSON_LIMIT)
                link = response.headers.get("Link")
            value = json.loads(body.decode("utf-8"), object_pairs_hook=json_object, parse_constant=no_constant)
            return value, link
        except GitHubDispatchError:
            raise
        except GitHubError:
            raise GitHubDispatchError("GitHub dispatch response exceeded its permitted bounds") from None
        except (UnicodeError, ValueError, RecursionError):
            raise GitHubDispatchError("GitHub dispatch returned invalid JSON or response data") from None
        except (URLError, OSError, HTTPException):
            raise GitHubDispatchError("GitHub dispatch request failed; its remote outcome may be unknown") from None

    def repository(self, repository: Doc) -> Doc:
        reviewed = _repository(repository)
        value, _ = self._request(f"/repos/{reviewed['full_name']}")
        _require(
            isinstance(value, dict)
            and type(value.get("id")) is int
            and value["id"] == reviewed["id"]
            and value.get("full_name") == reviewed["full_name"],
            "GitHub repository identity differs from the reviewed destination",
        )
        return reviewed

    def discover(self, repository: Doc, marker: str, effect: Doc) -> list[Doc]:
        reviewed, desired = _repository(repository), _effect(effect)
        _require(isinstance(marker, str) and _MARKER.fullmatch(marker), "invalid dispatch marker")
        _require(desired["body"].endswith(marker), "dispatch marker differs from the reviewed issue effect")
        self.repository(reviewed)
        found: list[Doc] = []
        seen: set[int] = set()
        for page in range(1, MAX_PAGES + 1):
            value, link = self._request(
                f"/repos/{reviewed['full_name']}/issues?state=all&sort=created&direction=asc&per_page={PAGE_SIZE}&page={page}"
            )
            _require(isinstance(value, list) and len(value) <= PAGE_SIZE, "invalid GitHub issue discovery page")
            following = _next_page(link, reviewed, page)
            for issue in value:
                _require(isinstance(issue, dict), "invalid GitHub issue discovery entry")
                identity = issue.get("id")
                _require(
                    type(identity) is int and identity > 0 and identity not in seen, "ambiguous GitHub issue pagination"
                )
                seen.add(identity)
                body = issue.get("body")
                _require(body is None or isinstance(body, str), "invalid GitHub issue discovery body")
                if isinstance(body, str) and marker in body:
                    found.append(_receipt(issue, reviewed, desired))
            # A full page warrants one more read even if a pagination hint was omitted. A short
            # page with a declared next page still requires that page before absence is known.
            if len(value) < PAGE_SIZE and not following:
                return found
        raise GitHubDispatchError("GitHub issue discovery exceeded its page limit; absence is not established")

    def create(self, repository: Doc, effect: Doc) -> Doc:
        reviewed, desired = _repository(repository), _effect(effect)
        self.repository(reviewed)
        value, _ = self._request(f"/repos/{reviewed['full_name']}/issues", effect=desired)
        return _receipt(value, reviewed, desired)
