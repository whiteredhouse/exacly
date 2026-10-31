"""Controlled transport contract tests; these do not establish live remote publication."""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import handoff, planning
from exacly import remote_publication as remote
from handoff_fixtures import HandoffCase
from handoff_fixtures import handoff_case as handoff_case

Doc = dict[str, Any]


class ControlledTransport:
    adapter = "controlled-cas-v1"

    def __init__(self) -> None:
        self.revision: str | None = None
        self.operation_id: str | None = None
        self.commit_present = False
        self.sends: list[Doc] = []
        self.inspections = 0
        self.failure: str | None = None
        self.before_send: Any = None

    def inspect(self, operation: Doc, *, timeout: float) -> Doc:
        self.inspections += 1
        assert 0 < timeout <= 300
        if self.failure == "inspect":
            raise OSError("controlled unavailable transport")
        return {
            "repository_id": operation["request"]["repository_id"],
            "ref": operation["request"]["ref"],
            "revision": self.revision,
            "commit_present": self.commit_present,
            "operation_id": self.operation_id,
        }

    def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
        if self.before_send:
            self.before_send(operation)
        assert repository.is_dir() and 0 < timeout <= 300
        self.sends.append(copy.deepcopy(operation))
        if self.failure == "before":
            raise OSError("controlled connection reset before acceptance")
        assert self.revision == operation["request"]["expected_revision"]
        self.revision = operation["authorization"]["commit"]
        self.operation_id = operation["id"]
        self.commit_present = True
        if self.failure == "after":
            raise OSError("controlled connection reset after acceptance")


@dataclass
class Case:
    handoff: HandoffCase
    request: remote.Request
    transport: ControlledTransport
    authorization: Doc

    def apply(self, **options: Any) -> Doc:
        return remote.apply(self.request, authorization=self.authorization, transport=self.transport, **options)

    def recover(self) -> Doc:
        return remote.recover(
            self.request.journal, self.request.journal_id, self.authorization["operation_id"], transport=self.transport
        )


@pytest.fixture
def remote_case(handoff_case: HandoffCase, tmp_path: Path) -> Case:
    published = handoff_case.apply()
    assert published["status"] == "published"
    directory = tmp_path / "remote-private"
    directory.mkdir(mode=0o700)
    journal = directory / "remote.sqlite3"
    request = remote.Request(
        journal,
        remote.initialize(journal),
        handoff_case.journal,
        handoff_case.request.policy["storage"]["journal_id"],
        published["handoff_id"],
        "1411700094",
        ControlledTransport.adapter,
        "refs/heads/nonproduction-output",
        None,
    )
    authorization = remote.preview(request)["authorization_template"]
    return Case(handoff_case, request, ControlledTransport(), authorization)


def test_preview_binds_exact_native_handoff_and_remote_destination_without_effects(remote_case: Case) -> None:
    case = remote_case
    before = case.request.journal.read_bytes(), case.handoff.authoritative_bytes()
    result = remote.preview(case.request)
    authorization = result["authorization_template"]
    assert result["status"] == "ready"
    assert authorization["effect"] == "publish_remote_git_ref"
    assert authorization["object_scope"] == "handoff-delta-only-existing-base"
    assert authorization["base_revision"] == case.handoff.request.base_revision
    assert authorization["identity"]["request"]["repository_id"] == case.request.repository_id
    assert authorization["identity"]["request"]["expected_revision"] is None
    native = handoff.show(case.handoff.journal, case.request.handoff_id)
    assert authorization["commit"] == native["receipt"]["commit"]
    assert (case.request.journal.read_bytes(), case.handoff.authoritative_bytes()) == before
    assert case.transport.sends == []
    assert result["claims"] == {**remote.CLAIMS, "remote_published": False}


def test_one_exact_send_is_retained_before_transport_and_replayed_without_republication(remote_case: Case) -> None:
    case = remote_case

    def before(operation: Doc) -> None:
        connection = sqlite3.connect(case.request.journal.as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT document FROM operations WHERE id=?", (operation["id"],)).fetchone()
            assert row is not None
            record = remote._decoded(row[0])
            assert record["events"][-1]["phase"] == "sent"
            assert record["intent"]["authorization"] == case.authorization
        finally:
            connection.close()

    case.transport.before_send = before
    authority = case.handoff.authoritative_bytes()
    result = case.apply()
    assert result["status"] == "published" and result["claims"]["remote_published"] is True
    assert result["claims"]["completion_authorized"] is False
    assert result["intent"]["handoff"]["digest"] == case.authorization["identity"]["handoff_receipt"]
    assert len(case.transport.sends) == 1
    assert case.apply() == result and case.recover() == result
    assert len(case.transport.sends) == 1
    assert case.handoff.authoritative_bytes() == authority
    assert remote.show(case.request.journal, case.request.journal_id, result["operation_id"]) == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_id", "1"),
        ("ref", "refs/heads/other"),
        ("expected_revision", "a" * 40),
        ("handoff_journal_id", "00112233445546778899aabbccddeeff"),
    ],
)
def test_changed_scope_cannot_reuse_exact_authorization(remote_case: Case, field: str, value: str) -> None:
    case = remote_case
    changes: Doc = {field: value}
    with pytest.raises(ValueError):
        remote.apply(replace(case.request, **changes), authorization=case.authorization, transport=case.transport)
    assert case.transport.sends == []


def test_unpublished_native_handoff_cannot_supply_authority(remote_case: Case) -> None:
    case = remote_case
    with pytest.raises(ValueError, match="native published handoff"):
        remote.preview(replace(case.request, handoff_id="sha256:" + "0" * 64))
    assert case.transport.sends == []


def test_ref_drift_conflicts_without_overwriting_other_actor(remote_case: Case) -> None:
    case = remote_case
    case.transport.revision = "b" * 40
    result = case.apply()
    assert result["status"] == "conflict"
    assert case.transport.revision == "b" * 40 and case.transport.sends == []
    assert case.recover()["status"] == "conflict"


def test_exact_ref_without_operation_correlation_or_objects_does_not_establish_publication(remote_case: Case) -> None:
    case = remote_case
    case.transport.failure = "after"
    assert case.apply()["status"] == "ambiguous"
    case.transport.failure = None
    case.transport.operation_id = None
    assert case.recover()["status"] == "ambiguous"
    case.transport.operation_id = case.authorization["operation_id"]
    case.transport.commit_present = False
    assert case.recover()["status"] == "ambiguous"
    case.transport.commit_present = True
    assert case.recover()["status"] == "published"
    assert len(case.transport.sends) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, 301])
def test_unbounded_or_invalid_transport_deadline_is_rejected(remote_case: Case, timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        remote_case.apply(timeout=timeout)
    assert remote_case.transport.sends == []


def test_wrong_adapter_or_replacement_journal_cannot_reuse_authority(remote_case: Case, tmp_path: Path) -> None:
    case = remote_case
    case.transport.adapter = "other-adapter"
    with pytest.raises(ValueError, match="adapter"):
        case.apply()
    case.transport.adapter = ControlledTransport.adapter
    directory = tmp_path / "replacement"
    directory.mkdir(mode=0o700)
    replacement = directory / "remote.sqlite3"
    remote.initialize(replacement)
    with pytest.raises(ValueError, match="instance"):
        remote.apply(
            replace(case.request, journal=replacement), authorization=case.authorization, transport=case.transport
        )
    assert case.transport.sends == []


def test_published_receipt_is_content_identified_and_preserves_native_authority(remote_case: Case) -> None:
    result = remote_case.apply()
    assert result["digest"] == planning.digest({key: value for key, value in result.items() if key != "digest"})
    assert result["intent"]["handoff"]["authorization"]["effect"] == "publish_local_git_ref"
