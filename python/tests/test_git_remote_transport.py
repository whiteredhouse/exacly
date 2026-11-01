"""Real local receive-pack with controlled GitHub metadata, not live GitHub acceptance."""

from __future__ import annotations

import datetime as dt
import io
import json
import multiprocessing
import subprocess
import time
from dataclasses import dataclass, replace
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import execution_store, publication_key
from exacly import git_remote_transport as transport
from exacly import remote_publication as remote
from exacly.github_evidence import GitHubError, NoRedirect
from handoff_fixtures import handoff_case as handoff_case
from test_remote_publication import Case
from test_remote_publication import remote_case as remote_case

Doc = dict[str, Any]
URL = "https://github.com/acme/consumer.git"


def git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["/usr/bin/git", "--git-dir=" + str(repository), *args],
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )


class LocalMetadata:
    def __init__(self, repository: Path, repository_id: str) -> None:
        self.repository, self.repository_id = repository, repository_id
        self.damage: str | None = None

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise AssertionError("publication does not read evidence artifacts")

    def get_json(self, path: str) -> Doc:
        prefix = "/repos/acme/consumer"
        if path == prefix:
            return {
                "id": int(self.repository_id) + (1 if self.damage == "repository" else 0),
                "full_name": "acme/consumer",
                "clone_url": URL,
            }
        try:
            if path.startswith(prefix + "/git/ref/"):
                reference = "refs/" + path.removeprefix(prefix + "/git/ref/")
                oid = git(self.repository, "show-ref", "--hash", "--verify", reference).decode().strip()
                kind = git(self.repository, "cat-file", "-t", oid).decode().strip()
                if self.damage == "marker" and kind == "tag":
                    oid = "0" * 40
                return {"ref": reference, "object": {"sha": oid, "type": kind}}
            if path.startswith(prefix + "/git/commits/"):
                oid = path.rsplit("/", 1)[1]
                raw = git(self.repository, "cat-file", "commit", oid).decode()
                headers = raw.split("\n\n", 1)[0].splitlines()
                return {
                    "sha": oid,
                    "tree": {"sha": headers[0].removeprefix("tree ")},
                    "parents": [
                        {"sha": line.removeprefix("parent ")} for line in headers if line.startswith("parent ")
                    ],
                }
            if path.startswith(prefix + "/git/tags/"):
                oid = path.rsplit("/", 1)[1]
                raw = git(self.repository, "cat-file", "tag", oid).decode()
                tag_headers, message = raw.split("\n\n", 1)
                fields = dict(line.split(" ", 1) for line in tag_headers.splitlines())
                return {
                    "sha": oid,
                    "tag": fields["tag"],
                    "object": {"sha": fields["object"], "type": fields["type"]},
                    "tagger": {
                        "name": "Exacly publication",
                        "email": "publication@exacly.invalid",
                        "date": dt.datetime.fromtimestamp(0, dt.UTC).isoformat(),
                    },
                    "message": message,
                }
        except subprocess.CalledProcessError:
            raise GitHubError("GitHub evidence request returned HTTP 404") from None
        raise AssertionError(path)


@dataclass
class GitCase:
    case: Case
    repository: Path
    source: LocalMetadata
    adapter: transport.GitHubGitTransport
    request: remote.Request
    authorization: Doc

    def apply(self) -> Doc:
        return remote.apply(self.request, authorization=self.authorization, transport=self.adapter)


@pytest.fixture
def git_case(remote_case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitCase:
    case = remote_case
    repository = tmp_path / "controlled-remote.git"
    subprocess.run(["/usr/bin/git", "init", "--bare", "--quiet", str(repository)], check=True)
    key_directory = tmp_path / "publication-key"
    key_directory.mkdir(mode=0o700)
    key_path = key_directory / "signing.key"
    key_id = publication_key.initialize(key_path)
    request = replace(
        case.request,
        adapter=transport.ADAPTER,
        expected_revision=case.handoff.request.base_revision,
        operation_marker_ref="refs/tags/req039-operations/controlled-one",
        operation_key=key_path,
        operation_key_id=key_id,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "--git-dir=" + str(case.handoff.repository),
            "push",
            "--quiet",
            str(repository),
            f"{case.handoff.request.base_revision}:{request.ref}",
        ],
        check=True,
        stderr=subprocess.DEVNULL,
    )
    source = LocalMetadata(repository, request.repository_id)
    adapter = transport.GitHubGitTransport(URL, request.repository_id, source)
    authorization = remote.preview(request)["authorization_template"]

    def exchange(data: bytes | None, *, token: str, timeout: float) -> bytes:
        assert token == "controlled-test-token"
        args = ["/usr/bin/git", "receive-pack", "--stateless-rpc"]
        if data is None:
            args.append("--advertise-refs")
        args.append(str(source.repository))
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout, check=False)
        if data is None:
            assert result.returncode == 0
            return transport._packet(b"# service=git-receive-pack\n") + b"0000" + result.stdout
        return result.stdout

    monkeypatch.setenv("EXACLY_REQ039_PUBLISH_TOKEN", "controlled-test-token")
    monkeypatch.setattr(adapter, "_exchange", exchange)
    monkeypatch.setattr(adapter, "_publisher_scope", lambda **kwargs: None)
    return GitCase(case, repository, source, adapter, request, authorization)


def test_atomic_git_publication_and_independent_tag_recovery(git_case: GitCase) -> None:
    item = git_case
    result = item.apply()
    assert result["status"] == "published", result
    marker = item.authorization["operation_marker"]
    assert git(item.repository, "rev-parse", item.request.ref).decode().strip() == item.authorization["commit"]
    assert git(item.repository, "rev-parse", marker["ref"]).decode().strip() == marker["object"]
    assert git(item.repository, "cat-file", "tag", marker["object"]) == marker["content"].encode()
    observed = item.adapter.inspect(result["intent"], timeout=30)
    assert observed["operation_id"] == result["operation_id"]
    assert item.apply() == result


def test_lease_race_rejects_both_refs_atomically(git_case: GitCase, monkeypatch: pytest.MonkeyPatch) -> None:
    item = git_case
    original = item.adapter._exchange
    new_commit = item.authorization["commit"]
    # Seed this exact object under another actor's fixture ref, then drift destination between
    # the preflight advertisement and the actual send. The marker must not appear on rejection.
    git(item.case.handoff.repository, "push", "--quiet", str(item.repository), f"{new_commit}:refs/heads/other-actor")

    def raced(data: bytes | None, *, token: str, timeout: float) -> bytes:
        if data is not None:
            git(item.repository, "update-ref", item.request.ref, new_commit)
        return original(data, timeout=timeout, token=token)

    monkeypatch.setattr(item.adapter, "_exchange", raced)
    result = item.apply()
    assert result["status"] == "conflict"
    assert git(item.repository, "rev-parse", item.request.ref).decode().strip() == new_commit
    with pytest.raises(subprocess.CalledProcessError):
        git(item.repository, "show-ref", "--verify", item.authorization["operation_marker"]["ref"])
    observed = item.adapter.inspect(result["intent"], timeout=30)
    assert observed["operation_id"] is None


def test_real_remote_acceptance_then_lost_response_recovers_without_second_push(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = git_case
    original = item.adapter._exchange
    sends = 0

    def lost(data: bytes | None, *, token: str, timeout: float) -> bytes:
        nonlocal sends
        result = original(data, timeout=timeout, token=token)
        if data is not None:
            sends += 1
            raise OSError("controlled lost acknowledgement")
        return result

    monkeypatch.setattr(item.adapter, "_exchange", lost)
    result = item.apply()
    assert result["status"] == "ambiguous" and sends == 1
    recovered = remote.recover(
        item.request.journal, item.request.journal_id, result["operation_id"], transport=item.adapter
    )
    assert recovered["status"] == "published" and sends == 1


def test_wrong_repository_marker_or_missing_credential_never_proves_publication(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = git_case
    item.source.damage = "repository"
    assert item.apply()["status"] == "prepared"
    item.source.damage = None
    monkeypatch.delenv("EXACLY_REQ039_PUBLISH_TOKEN")
    assert item.apply()["status"] == "ambiguous"
    with pytest.raises(subprocess.CalledProcessError):
        git(item.repository, "show-ref", "--verify", item.authorization["operation_marker"]["ref"])


def test_marker_contract_roundtrip_and_legacy_request_identity(git_case: GitCase) -> None:
    item = git_case
    assert remote.request_from_document(remote.request_document(item.request)) == item.request
    legacy = item.case.request
    document = remote.request_document(legacy)
    assert "operation_marker_ref" not in document
    assert remote.request_from_document(document) == legacy
    assert item.authorization["operation_id"] != item.case.authorization["operation_id"]
    assert item.authorization["operation_marker"]["effect"] == "create_operation_tag"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/remote",
        "https://user:secret@github.com/acme/consumer.git",
        "https://github.com/acme/consumer.git?token=x",
        "https://evil.invalid/acme/consumer.git",
    ],
)
def test_candidate_urls_are_not_transport_configuration(git_case: GitCase, url: str) -> None:
    with pytest.raises(ValueError, match="literal approved"):
        transport.GitHubGitTransport(url, git_case.request.repository_id, git_case.source)


@pytest.mark.parametrize("content", [b"xyz!", b"0003", b"0004", b"0010short", b"0" * (1024 * 1024 + 1)])
def test_malformed_or_unbounded_git_packets_are_rejected(content: bytes) -> None:
    with pytest.raises(ValueError):
        transport._packets(content)


def test_pack_contains_only_exact_delta_and_marker_excluding_base_history(
    git_case: GitCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = git_case
    original = item.adapter._exchange
    sent: list[bytes] = []

    def capture(data: bytes | None, *, token: str, timeout: float) -> bytes:
        if data is not None:
            sent.append(data)
        return original(data, timeout=timeout, token=token)

    monkeypatch.setattr(item.adapter, "_exchange", capture)
    result = item.apply()
    assert result["status"] == "published"
    package = sent[0][len(transport._commands(result["intent"])) :]
    pack = tmp_path / "delta.pack"
    index = tmp_path / "delta.idx"
    pack.write_bytes(package)
    subprocess.run(["/usr/bin/git", "index-pack", "-o", str(index), str(pack)], check=True, stdout=subprocess.DEVNULL)
    listing = subprocess.check_output(["/usr/bin/git", "verify-pack", "-v", str(index)]).decode().splitlines()
    objects = {line.split()[0] for line in listing if len(line.split()[0]) == 40}
    expected = {
        line.split()[0]
        for line in git(
            item.case.handoff.repository,
            "rev-list",
            "--objects",
            item.authorization["commit"],
            "^" + item.authorization["base_revision"],
        )
        .decode()
        .splitlines()
    }
    expected.add(item.authorization["operation_marker"]["object"])
    assert objects == expected
    assert item.authorization["base_revision"] not in objects


@pytest.mark.parametrize("missing", [b"atomic", b"report-status"])
def test_remote_without_required_atomic_capability_cannot_receive_effect(
    git_case: GitCase, missing: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = git_case
    original = item.adapter._exchange
    posts = 0

    def unavailable(data: bytes | None, *, token: str, timeout: float) -> bytes:
        nonlocal posts
        assert data is None
        content = original(data, token=token, timeout=timeout)
        packets = transport._packets(content)
        changed: list[bytes] = []
        for packet in packets:
            if b"\0" in packet:
                entry, capabilities = packet.split(b"\0", 1)
                packet = entry + b"\0" + b" ".join(item for item in capabilities.strip().split() if item != missing)
            changed.append(transport._packet(packet) if packet else b"0000")
        return b"".join(changed)

    monkeypatch.setattr(item.adapter, "_exchange", unavailable)
    assert item.apply()["status"] == "ambiguous"
    assert posts == 0
    assert git(item.repository, "rev-parse", item.request.ref).decode().strip() == item.authorization["base_revision"]


def test_slow_api_response_stops_before_another_read_or_new_effect(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = git_case
    original = item.source.get_json
    clock = [0.0]
    reads: list[str] = []

    def slow(path: str) -> Doc:
        reads.append(path)
        value = original(path)
        clock[0] += 100
        return value

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(item.source, "get_json", slow)
    assert item.apply()["status"] == "prepared"
    assert len(reads) == 1


def test_http_credentials_are_confined_to_literal_origin_and_redirects_are_rejected(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = git_case
    calls: list[str] = []
    headers: Message[str, str] = Message()
    headers["Location"] = "https://unapproved.invalid"

    class RedirectingOpener:
        def open(self, request: Any, *, timeout: float) -> Any:
            calls.append(request.full_url)
            assert request.get_header("Authorization").startswith("Basic ")
            assert "controlled-test-token" not in request.full_url
            raise HTTPError(request.full_url, 302, "redirect", headers, io.BytesIO())

    def opener(handler: Any) -> RedirectingOpener:
        assert isinstance(handler, NoRedirect)
        return RedirectingOpener()

    monkeypatch.setattr(transport, "build_opener", opener)
    # Call the real HTTP implementation, bypassing only this fixture's local receive-pack routing.
    with pytest.raises(ValueError, match="trusted Git HTTP"):
        transport.GitHubGitTransport._exchange(item.adapter, None, token="controlled-test-token", timeout=30)
    assert calls == [URL + "/info/refs?service=git-receive-pack"]


@pytest.mark.parametrize("same_marker", [True, False])
def test_racing_marker_creation_rejects_branch_update_atomically(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch, same_marker: bool
) -> None:
    item = git_case
    original = item.adapter._exchange
    marker = item.authorization["operation_marker"]

    def racing(data: bytes | None, *, token: str, timeout: float) -> bytes:
        if data is not None:
            source = marker["object"] if same_marker else item.authorization["base_revision"]
            git(item.case.handoff.repository, "push", "--quiet", str(item.repository), f"{source}:{marker['ref']}")
        return original(data, token=token, timeout=timeout)

    monkeypatch.setattr(item.adapter, "_exchange", racing)
    result = item.apply()
    assert result["status"] == "conflict"
    assert git(item.repository, "rev-parse", item.request.ref).decode().strip() == item.authorization["base_revision"]
    expected = marker["object"] if same_marker else item.authorization["base_revision"]
    assert git(item.repository, "rev-parse", marker["ref"]).decode().strip() == expected
    recovered = remote.recover(
        item.request.journal, item.request.journal_id, result["operation_id"], transport=item.adapter
    )
    assert recovered["status"] == "conflict"
    git(item.repository, "update-ref", item.request.ref, item.authorization["commit"])
    assert (
        remote.recover(item.request.journal, item.request.journal_id, result["operation_id"], transport=item.adapter)[
            "status"
        ]
        == "conflict"
    )


@pytest.mark.parametrize("damage", ["content_type", "limit", "deadline"])
def test_http_boundary_rejects_wrong_type_excessive_body_and_expired_read(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    item = git_case
    clock = [0.0]

    class Response:
        def __init__(self) -> None:
            self.headers = {
                "Content-Type": "text/html"
                if damage == "content_type"
                else "application/x-git-receive-pack-advertisement",
                "Content-Length": str(1024 * 1024 + 1) if damage == "limit" else "4",
            }

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def getcode(self) -> int:
            return 200

        def read(self, size: int) -> bytes:
            if damage == "deadline":
                clock[0] += 100
            return b"0000"

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(transport, "build_opener", lambda handler: Opener())
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    with pytest.raises(ValueError):
        transport.GitHubGitTransport._exchange(item.adapter, None, token="controlled-test-token", timeout=30)


@pytest.mark.parametrize("damage", [None, "broad", "identity", "name", "unavailable"])
def test_publisher_scope_is_authenticated_using_same_token_and_exact_single_repository(
    git_case: GitCase, monkeypatch: pytest.MonkeyPatch, damage: str | None
) -> None:
    item = git_case
    repository = {"id": int(item.request.repository_id), "full_name": "acme/consumer"}
    if damage == "identity":
        repository["id"] = 1
    if damage == "name":
        repository["full_name"] = "other/repository"
    payload = {
        "total_count": 2 if damage == "broad" else 1,
        "repositories": [repository, repository] if damage == "broad" else [repository],
    }
    encoded = json.dumps(payload).encode()
    calls: list[str] = []

    class Response(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(encoded)
            self.headers = {"Content-Type": "application/json", "Content-Length": str(len(encoded))}

        def getcode(self) -> int:
            return 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            calls.append(request.full_url)
            assert request.get_header("Authorization") == "Bearer controlled-installation-token"
            assert request.get_method() == "GET"
            if damage == "unavailable":
                headers: Message[str, str] = Message()
                raise HTTPError(request.full_url, 403, "forbidden", headers, io.BytesIO())
            return Response()

    monkeypatch.setattr(transport, "build_opener", lambda handler: Opener())
    if damage is None:
        transport.GitHubGitTransport._publisher_scope(item.adapter, token="controlled-installation-token", timeout=30)
    else:
        with pytest.raises(ValueError):
            transport.GitHubGitTransport._publisher_scope(
                item.adapter, token="controlled-installation-token", timeout=30
            )
    assert calls == ["https://api.github.com/installation/repositories?per_page=100&page=1"]


@pytest.mark.parametrize("damage", ["missing", "replacement"])
def test_missing_or_replaced_operator_key_blocks_effect_and_recovery(git_case: GitCase, damage: str) -> None:
    item = git_case
    key = item.request.operation_key
    assert key is not None
    key.unlink()
    if damage == "replacement":
        publication_key.initialize(key)
    with pytest.raises((ValueError, OSError)):
        item.apply()
    assert git(item.repository, "rev-parse", item.request.ref).decode().strip() == item.authorization["base_revision"]
    with pytest.raises(subprocess.CalledProcessError):
        git(item.repository, "show-ref", "--verify", item.authorization["operation_marker"]["ref"])


def test_marker_proof_is_bound_to_pinned_key_without_retaining_raw_secret(git_case: GitCase) -> None:
    item = git_case
    key = item.request.operation_key
    assert key is not None and item.request.operation_key_id is not None
    marker = item.authorization["operation_marker"]
    payload = json.loads(marker["content"].split("\n\n", 1)[1])
    proof = payload.pop("proof")
    assert payload["key_id"] == item.request.operation_key_id
    assert publication_key.verify(payload, proof, key, item.request.operation_key_id)
    changed = {**payload, "operation_id": "sha256:" + "0" * 64}
    assert not publication_key.verify(changed, proof, key, item.request.operation_key_id)
    result = item.apply()
    assert result["status"] == "published"
    serialized = json.dumps(result)
    assert key.read_bytes().hex() not in serialized


def _hold_key_parent(key: Path, ready: Any, release: Any) -> None:
    with execution_store.locked(key, write=True):
        ready.set()
        assert release.wait(5)


def test_signing_key_contention_consumes_same_native_wait_budget(git_case: GitCase) -> None:
    item = git_case
    key = item.request.operation_key
    assert key is not None
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_key_parent, args=(key, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(execution_store.LockTimeoutError):
            remote.preview(item.request, lock_timeout=0.05)
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_native_lookup_distinguishes_exact_absence_and_retained_operation_without_effects(remote_case: Case) -> None:
    case = remote_case
    request = case.request
    identity = case.authorization["operation_id"]
    before = request.journal.read_bytes()
    assert remote.lookup(request.journal, request.journal_id, identity) is None
    assert request.journal.read_bytes() == before
    assert case.transport.sends == [] and case.transport.inspections == 0
    published = case.apply()
    assert remote.lookup(request.journal, request.journal_id, identity) == published
    assert len(case.transport.sends) == 1


@pytest.mark.parametrize("damage", ["identity", "missing", "unsafe"])
def test_native_lookup_cannot_interpret_invalid_storage_as_absence(remote_case: Case, damage: str) -> None:
    case = remote_case
    request = case.request
    journal, journal_id = request.journal, request.journal_id
    if damage == "identity":
        journal_id = "00112233445546778899aabbccddeeff"
    elif damage == "missing":
        journal = journal.with_name("not-initialized.sqlite3")
    else:
        journal.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        remote.lookup(journal, journal_id, case.authorization["operation_id"])
    assert case.transport.sends == [] and case.transport.inspections == 0
