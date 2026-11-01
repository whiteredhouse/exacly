"""Exact GitHub publication using an explicit atomic receive-pack transaction and an independently read operation tag.

The trusted operator selects the literal GitHub repository and protected token variable. Native
authorization must grant both destination and marker; the adapter never imports missing base history.
Git handles the server-side compare-and-swap. GitHub reads authenticate repository and object identity.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import zlib
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as HTTPRequest
from urllib.request import build_opener

from exacly import admission, handoff_git, publication_key, remote_publication
from exacly import execution_store as store
from exacly.github_evidence import GitHubError, NoRedirect, read_bounded

Doc = dict[str, Any]
ADAPTER = "github-git-atomic-v1"
_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"


def _body(response: Any, deadline: store.Deadline) -> bytes:
    class BoundedResponse:
        headers = response.headers

        def read(self, size: int) -> bytes:
            deadline.check()
            block: bytes = response.read(size)
            deadline.check()
            return block

    return read_bounded(BoundedResponse(), 1024 * 1024)


def _get(source: admission.Source, path: str, *, deadline: store.Deadline, optional: bool = False) -> Doc | None:
    deadline.check()
    try:
        result = source.get_json(path)
        deadline.check()
        return result
    except GitHubError as error:
        if optional and str(error) == "GitHub evidence request returned HTTP 404":
            deadline.check()
            return None
        raise


def _packet(value: bytes) -> bytes:
    store.require(len(value) <= 65516, "Git packet exceeds its protocol limit")
    return f"{len(value) + 4:04x}".encode() + value


def _packets(content: bytes) -> list[bytes]:
    store.require(isinstance(content, bytes) and len(content) <= 1024 * 1024, "invalid bounded Git response")
    result: list[bytes] = []
    cursor = 0
    while cursor < len(content):
        header = content[cursor : cursor + 4]
        store.require(len(header) == 4 and re.fullmatch(b"[0-9a-fA-F]{4}", header), "invalid Git packet framing")
        size = int(header, 16)
        cursor += 4
        if size == 0:
            result.append(b"")
            continue
        store.require(5 <= size <= 65520 and cursor + size - 4 <= len(content), "truncated or empty Git data packet")
        result.append(content[cursor : cursor + size - 4])
        cursor += size - 4
    return result


def _advertisement(content: bytes, operation: Doc) -> None:
    packets = _packets(content)
    store.require(
        len(packets) >= 4 and packets[:2] == [b"# service=git-receive-pack\n", b""],
        "unexpected Git receive-pack service",
    )
    refs: dict[str, str] = {}
    capabilities: set[str] = set()
    for entry in packets[2:]:
        if not entry:
            continue
        if b"\0" in entry:
            entry, declared = entry.split(b"\0", 1)
            store.require(not capabilities, "duplicate Git capability advertisement")
            capabilities = set(declared.decode("ascii").strip().split())
        oid, ref = entry.decode("ascii").strip().split(" ", 1)
        store.require(re.fullmatch(r"[0-9a-f]{40}", oid) and ref not in refs, "invalid Git advertised reference")
        refs[ref] = oid
    request, marker = operation["request"], operation["authorization"]["operation_marker"]
    store.require(
        {"atomic", "report-status"} <= capabilities
        and not any(item.startswith("object-format=") and item != "object-format=sha1" for item in capabilities),
        "remote Git server does not support the required atomic SHA1 transaction",
    )
    store.require(
        refs.get(request["ref"]) == request["expected_revision"] and marker["ref"] not in refs,
        "advertised remote destination or marker conflicts with exact authority",
    )


def _commands(operation: Doc) -> bytes:
    request, authorization = operation["request"], operation["authorization"]
    marker = authorization["operation_marker"]
    # Always send the exact expected-old command, including when a racing actor has already moved
    # the branch to our desired commit. Porcelain push skips that no-op and can create a false marker.
    branch = f"{request['expected_revision']} {authorization['commit']} {request['ref']}".encode()
    tag = f"{'0' * 40} {marker['object']} {marker['ref']}\n".encode()
    return _packet(branch + b"\0report-status atomic\n") + _packet(tag) + b"0000"


def _accepted(response: bytes, operation: Doc) -> None:
    packets = _packets(response)
    wanted = [
        b"unpack ok\n",
        f"ok {operation['request']['ref']}\n".encode(),
        f"ok {operation['authorization']['operation_marker']['ref']}\n".encode(),
        b"",
    ]
    if packets != wanted:
        refs = [operation["request"]["ref"], operation["authorization"]["operation_marker"]["ref"]]
        rejected = (
            len(packets) == 4
            and packets[-1] == b""
            and packets[0].startswith(b"unpack ")
            and all(
                packet.startswith(f"ng {reference} ".encode())
                for packet, reference in zip(packets[1:3], refs, strict=True)
            )
        )
        if rejected:
            raise remote_publication.PublicationRejected("remote Git explicitly rejected the exact atomic transaction")
        raise store.StoreError("remote Git did not confirm the complete exact transaction")


def _install_marker(repository: Path, marker: Doc) -> None:
    raw = marker["content"].encode()
    encoded = b"tag " + str(len(raw)).encode() + b"\0" + raw
    identity = hashlib.sha1(encoded).hexdigest()
    store.require(identity == marker["object"], "operation marker object differs from exact authorization")
    directory = repository / "objects" / identity[:2]
    if not directory.exists():
        directory.mkdir(mode=0o700)
        handoff_git._sync(directory.parent)
    path = directory / identity[2:]
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        store.require(
            zlib.decompress(handoff_git._read_file(path, 64 * 1024)) == encoded, "local operation marker changed"
        )
    else:
        with os.fdopen(descriptor, "wb") as output:
            output.write(zlib.compress(encoded))
            output.flush()
            os.fsync(output.fileno())
    handoff_git._sync(directory)


class GitHubGitTransport:
    """Trusted selected GitHub adapter; no ambient URL, credential helper or candidate provider."""

    adapter = ADAPTER

    def __init__(
        self,
        repository_url: str,
        repository_id: str,
        source: admission.Source,
        *,
        token_environment: str = "EXACLY_REQ039_PUBLISH_TOKEN",
    ) -> None:
        matched = re.fullmatch(r"https://github\.com/(" + _SLUG + r")\.git", repository_url)
        store.require(matched is not None, "Git publication requires a literal approved GitHub HTTPS clone URL")
        store.require(
            isinstance(repository_id, str) and re.fullmatch(r"[1-9][0-9]{0,19}", repository_id),
            "Git publication requires an immutable repository identity",
        )
        store.require(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", token_environment)
            and token_environment not in {"HOME", "PATH", "HTTP_PROXY", "HTTPS_PROXY"},
            "invalid protected publisher token selector",
        )
        assert matched is not None
        self.repository_url, self.repository_id, self.full_name = repository_url, repository_id, matched.group(1)
        self.source, self.token_environment = source, token_environment

    def _operation(self, operation: Doc, deadline: store.Deadline) -> Doc:
        checked = remote_publication.validate_operation(operation, lock_timeout=deadline.remaining())
        store.require(
            checked["request"]["adapter"] == ADAPTER
            and checked["request"]["repository_id"] == self.repository_id
            and "operation_marker" in checked["authorization"],
            "Git publication needs its exact selected repository, adapter and marker grant",
        )
        store.require(
            checked["request"]["expected_revision"] == checked["authorization"]["base_revision"],
            "Git publication only updates an existing exact approved base; branch setup is separate",
        )
        remote = _get(self.source, f"/repos/{self.full_name}", deadline=deadline)
        store.require(
            remote is not None
            and type(remote.get("id")) is int
            and str(remote["id"]) == self.repository_id
            and remote.get("full_name") == self.full_name
            and remote.get("clone_url") == self.repository_url,
            "remote repository identity changed",
        )
        return checked

    def _ref(self, reference: str, deadline: store.Deadline) -> Doc | None:
        found = _get(
            self.source,
            f"/repos/{self.full_name}/git/ref/{quote(reference.removeprefix('refs/'), safe='/')}",
            optional=True,
            deadline=deadline,
        )
        if found is not None:
            store.require(
                found.get("ref") == reference and isinstance(found.get("object"), dict),
                "remote reference identity changed",
            )
        return found

    def inspect(self, operation: Doc, *, timeout: float) -> Doc:
        deadline = store.Deadline(timeout)
        checked = self._operation(operation, deadline)
        request, authorization = checked["request"], checked["authorization"]
        base = _get(
            self.source, f"/repos/{self.full_name}/git/commits/{authorization['base_revision']}", deadline=deadline
        )
        store.require(
            base is not None and base.get("sha") == authorization["base_revision"],
            "approved remote base is unavailable; base import needs separate authority",
        )
        destination = self._ref(request["ref"], deadline)
        revision = None if destination is None else destination["object"].get("sha")
        store.require(
            destination is None
            or (
                destination["object"].get("type") == "commit"
                and isinstance(revision, str)
                and re.fullmatch(r"[0-9a-f]{40}", revision)
            ),
            "invalid remote branch",
        )
        commit = _get(
            self.source,
            f"/repos/{self.full_name}/git/commits/{authorization['commit']}",
            optional=True,
            deadline=deadline,
        )
        present = commit is not None
        if present:
            assert commit is not None
            store.require(
                commit.get("sha") == authorization["commit"]
                and commit.get("tree", {}).get("sha") == authorization["tree"]
                and [parent.get("sha") for parent in commit.get("parents", [])] == [authorization["base_revision"]],
                "remote commit differs from the exact handoff",
            )
        marker, operation_id = authorization["operation_marker"], None
        reference = self._ref(marker["ref"], deadline)
        if (
            reference is not None
            and reference["object"].get("type") == "tag"
            and reference["object"].get("sha") == marker["object"]
        ):
            tag = _get(self.source, f"/repos/{self.full_name}/git/tags/{marker['object']}", deadline=deadline)
            store.require(
                tag is not None
                and tag.get("sha") == marker["object"]
                and tag.get("tag") == marker["ref"].removeprefix("refs/tags/")
                and tag.get("object", {}).get("sha") == authorization["commit"]
                and tag.get("object", {}).get("type") == "commit"
                and tag.get("message") == marker["content"].split("\n\n", 1)[1],
                "remote operation tag differs from the independently approved object",
            )
            assert tag is not None
            tagger = tag.get("tagger", {})
            store.require(
                tagger.get("name") == "Exacly publication"
                and tagger.get("email") == "publication@exacly.invalid"
                and dt.datetime.fromisoformat(tagger.get("date", "").replace("Z", "+00:00")).timestamp() == 0,
                "remote operation tag identity changed",
            )
            # The correlation is read from the actual immutable remote tag, never echoed from input.
            payload = json.loads(tag["message"], object_pairs_hook=store.unique_object)
            proof = payload.pop("proof", None)
            store.require(
                publication_key.verify(
                    payload,
                    proof,
                    Path(request["operation_key"]),
                    request["operation_key_id"],
                    lock_timeout=deadline.remaining(),
                ),
                "remote operation marker lacks its authenticated operator proof",
            )
            operation_id = payload["operation_id"]
        deadline.check()
        return {
            "repository_id": self.repository_id,
            "ref": request["ref"],
            "revision": revision,
            "commit_present": present,
            "operation_id": operation_id,
        }

    def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
        deadline = store.Deadline(timeout)
        checked = self._operation(operation, deadline)
        authorization, request = checked["authorization"], checked["request"]
        store.require(
            handoff_git.inspect_repository(repository, timeout=deadline.remaining()) == authorization["repository"],
            "sealed handoff repository changed",
        )
        store.require(
            request["expected_revision"] == authorization["base_revision"],
            "Git publication only fast-forwards its exact approved base",
        )
        current = self.inspect(checked, timeout=deadline.remaining())
        store.require(
            current["revision"] == request["expected_revision"]
            and self._ref(authorization["operation_marker"]["ref"], deadline) is None,
            "remote destination or operation marker conflicts with exact authority",
        )
        token = os.environ.get(self.token_environment)
        store.require(
            isinstance(token, str)
            and token.isascii()
            and bool(token)
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token),
            "protected publisher credential is unavailable",
        )
        assert token is not None
        self._publisher_scope(token=token, timeout=deadline.remaining())
        advertised = self._exchange(None, token=token, timeout=deadline.remaining())
        _advertisement(advertised, checked)
        _install_marker(repository, authorization["operation_marker"])
        selection = (
            authorization["commit"]
            + "\n"
            + authorization["operation_marker"]["object"]
            + "\n^"
            + authorization["base_revision"]
            + "\n"
        ).encode()
        # Git constructs only the new handoff/marker closure; excluding the existing approved base
        # prevents base-history import regardless of subsequent remote advertisement or ref races.
        package = handoff_git._run(
            repository,
            ["pack-objects", "--stdout", "--revs"],
            deadline,
            limit=handoff_git.MAX_PACKAGE_BYTES,
            input_data=selection,
        )
        response = self._exchange(_commands(checked) + package, token=token, timeout=deadline.remaining())
        _accepted(response, checked)

    def _publisher_scope(self, *, token: str, timeout: float) -> None:
        """Authenticate exact repository access using the same installation token sent to Git.

        This endpoint authenticates installation credentials; an arbitrary PAT or separately selected
        read Source cannot attest the publisher's scope. The operator must mint the installation token
        with repository_ids:[approved ID]; a broad token is outside the supported contract.
        """
        deadline = store.Deadline(timeout)
        request = HTTPRequest(
            "https://api.github.com/installation/repositories?per_page=100&page=1",
            headers={
                "User-Agent": "exacly-git-publication",
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            method="GET",
        )
        try:
            deadline.check()
            with build_opener(NoRedirect()).open(request, timeout=deadline.remaining()) as response:
                deadline.check()
                store.require(response.getcode() == 200, "publisher installation scope is unavailable")
                store.require(
                    response.headers.get("Content-Type", "").split(";", 1)[0] == "application/json",
                    "unexpected publisher installation response type",
                )
                content = _body(response, deadline)
                deadline.check()
            value = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
            store.require(
                isinstance(value, dict)
                and type(value.get("total_count")) is int
                and value["total_count"] == 1
                and isinstance(value.get("repositories"), list)
                and len(value["repositories"]) == 1,
                "publisher installation token must be scoped to exactly one approved repository",
            )
            repository = value["repositories"][0]
            store.require(
                isinstance(repository, dict)
                and type(repository.get("id")) is int
                and str(repository["id"]) == self.repository_id
                and repository.get("full_name") == self.full_name,
                "publisher installation token is scoped to another immutable repository",
            )
        except (HTTPError, URLError, HTTPException, OSError, GitHubError, json.JSONDecodeError):
            raise store.StoreError(
                "publisher installation token scope could not be independently established"
            ) from None

    def _exchange(self, data: bytes | None, *, token: str, timeout: float) -> bytes:
        store.require(0 < timeout <= 300, "invalid Git HTTP deadline")
        endpoint = "/info/refs?service=git-receive-pack" if data is None else "/git-receive-pack"
        headers = {
            "User-Agent": "exacly-git-publication",
            "Authorization": "Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode(),
            "Accept": "application/x-git-receive-pack-advertisement"
            if data is None
            else "application/x-git-receive-pack-result",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-git-receive-pack-request"
        deadline = store.Deadline(timeout)
        request = HTTPRequest(
            self.repository_url + endpoint, data=data, headers=headers, method="GET" if data is None else "POST"
        )
        try:
            # The inherited proxy and Python CA trust stay in force. Redirects are rejected before
            # credentials can leave this literal trusted GitHub origin and repository endpoint.
            deadline.check()
            with build_opener(NoRedirect()).open(request, timeout=deadline.remaining()) as response:
                store.require(response.getcode() == 200, "Git HTTP server did not accept the exact request")
                deadline.check()
                store.require(
                    response.headers.get("Content-Type", "").split(";", 1)[0] == headers["Accept"],
                    "unexpected Git HTTP response type",
                )

                return _body(response, deadline)
        except (HTTPError, URLError, HTTPException, OSError, GitHubError):
            raise store.StoreError("trusted Git HTTP transport is unavailable or rejected the operation") from None
