"""Delivery CLI selects one installed merge profile and keeps local history separate from effects."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import stat
import subprocess
import uuid
from collections.abc import Iterator, MutableMapping, MutableSequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError

import pytest
from automatic_merge_fixtures import BASE_REF, HEAD, HEAD_REF, MERGE, TOKEN_ENVIRONMENT, MergeCase, make_case
from exacly import delivery_automation as contracts
from exacly import delivery_io, delivery_store
from exacly.cli import exacly_main
from exacly.github_merge import GitHubMergeAdapter

Doc = dict[str, Any]


def invoke(args: list[str]) -> int:
    try:
        return exacly_main(["delivery", *args])
    except SystemExit as error:
        assert type(error.code) is int
        return error.code


@pytest.fixture
def private(tmp_path: Path) -> Path:
    result = tmp_path / "operator"
    result.mkdir(mode=0o700)
    return result


def write_document(directory: Path, name: str, document: Any) -> Path:
    path = directory / name
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("local setup/history attempted provider network or a process effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def init_files(private: Path) -> tuple[Path, Path, Path, Path]:
    objective = write_document(
        private,
        "objective.json",
        {"id": contracts.digest("delivery objective"), "original_goal": contracts.digest("goal")},
    )
    limits = write_document(
        private, "limits.json", {"operations": 2, "wall_time_ms": 10000, "reads": 20, "rollbacks": 0}
    )
    native = write_document(private, "native-limits.json", {"wall_time_ms": 1000, "read_calls": 4, "write_calls": 2})
    return private / "delivery.sqlite3", objective, limits, native


def init_args(private: Path) -> list[str]:
    journal, objective, limits, native = init_files(private)
    return [
        "init", "--journal", str(journal), "--objective", str(objective), "--limits", str(limits),
        "--native-limits", str(native), "--json",
    ]  # fmt: skip


def test_delivery_json_reader_is_bounded_strict_and_regular(private: Path) -> None:
    path = write_document(private, "input.json", {"selected": "exact input", "count": 3})
    assert delivery_io.read_document(path) == {"selected": "exact input", "count": 3}


@pytest.mark.parametrize(
    "encoded",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"[]",
        b'{"x":"\xff"}',
        b'{"x":"\\u0000"}',
        b'{"x":"\\ud800"}',
        b'{"x":' * 70 + b"0" + b"}" * 70,
    ],
)
def test_delivery_reader_rejects_ambiguous_or_unbounded_structure(private: Path, encoded: bytes) -> None:
    path = private / "invalid.json"
    path.write_bytes(encoded)
    with pytest.raises(ValueError):
        delivery_io.read_document(path)


def test_delivery_reader_does_not_consume_oversized_input(private: Path) -> None:
    path = private / "oversized.json"
    path.write_bytes(b'{"value":"' + b"x" * contracts.MAX_DOCUMENT_BYTES + b'"}')
    with pytest.raises(ValueError, match=r"limit|size"):
        delivery_io.read_document(path)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_delivery_reader_never_follows_final_aliases_or_reads_special_files(private: Path, kind: str) -> None:
    path = private / "selected.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(write_document(private, "actual.json", {"value": "not implicitly selected"}))
    with pytest.raises((ValueError, OSError)):
        delivery_io.read_document(path)


@pytest.mark.parametrize("mask", [0o022, 0o000, 0o777])
def test_explicit_delivery_key_setup_is_private_fresh_and_offline(
    private: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mask: int
) -> None:
    no_provider(monkeypatch)
    path = private / "merge.key"
    old_mask = os.umask(mask)
    try:
        assert invoke(["key-init", "--key", str(path), "--json"]) == 0
    finally:
        os.umask(old_mask)
    first_output = capsys.readouterr().out
    report = json.loads(first_output)
    assert isinstance(report, dict)
    assert path.read_bytes().startswith(b"EXACLY DELIVERY MERGE KEY v1\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = path.read_bytes()
    assert invoke(["key-init", "--key", str(path), "--json"]) == 2
    assert path.read_bytes() == saved
    secret = saved[len(b"EXACLY DELIVERY MERGE KEY v1\n") :].hex()
    assert secret not in first_output + capsys.readouterr().out


def test_explicit_delivery_journal_initialization_is_private_and_cannot_reset_limits(
    private: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    no_provider(monkeypatch)
    args = init_args(private)
    assert invoke(args) == 0
    report = json.loads(capsys.readouterr().out)
    journal = private / "delivery.sqlite3"
    assert journal.is_file() and stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert isinstance(report, dict)
    saved = journal.read_bytes()
    limits = private / "limits.json"
    limits.write_text(json.dumps({"operations": 20, "wall_time_ms": 1000000, "reads": 200, "rollbacks": 10}))
    assert invoke(args) == 2
    assert journal.read_bytes() == saved


def test_explicit_setup_and_local_history_honor_nondefault_lock_timeout(
    private: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    no_provider(monkeypatch)
    assert invoke([*init_args(private), "--lock-timeout", "0.5"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    journal = private / "delivery.sqlite3"
    saved = journal.read_bytes()
    assert (
        invoke(
            ["show", "--journal", str(journal), "--journal-id", initialized["id"], "--lock-timeout", "0.5", "--json"]
        )
        == 0
    )
    assert journal.read_bytes() == saved


def test_history_read_never_initializes_a_missing_delivery_journal(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_provider(monkeypatch)
    journal = private / "missing.sqlite3"
    before = set(private.iterdir())
    assert invoke(["show", "--journal", str(journal), "--journal-id", uuid.uuid4().hex, "--json"]) == 2
    assert not journal.exists() and set(private.iterdir()) == before


@pytest.mark.parametrize(
    "extra", [["--date", "2030-01-01"], ["--now", "2030-01-01T00:00:00Z"], ["--provider", "other"]]
)
def test_operational_cli_cannot_select_a_clock_or_provider(
    private: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    no_provider(monkeypatch)
    journal = private / "not-initialized.sqlite3"
    args = [
        "merge", "--journal", str(journal), "--journal-id", uuid.uuid4().hex,
        "--request", "missing-request", "--payload", "missing-payload", "--policy", "missing-policy",
        "--config", "missing-config", *extra,
    ]  # fmt: skip
    assert invoke(args) == 2
    assert not journal.exists()


@pytest.mark.parametrize(
    "approval_args",
    [[], ["--approval", "approval.json"], ["--approved-approval-digest", "sha256:" + "a" * 64]],
)
def test_apply_requires_the_whole_separately_selected_approval_before_reading_inputs(
    private: Path, monkeypatch: pytest.MonkeyPatch, approval_args: list[str]
) -> None:
    def forbidden(path: Path) -> Doc:
        pytest.fail("partial approval reached input reads")

    monkeypatch.setattr(delivery_io, "read_document", forbidden)
    journal = private / "missing.sqlite3"
    args = [
        "merge", "--journal", str(journal), "--journal-id", uuid.uuid4().hex,
        "--request", "request.json", "--payload", "payload.json", "--policy", "policy.json",
        "--config", "config.json", "--apply", *approval_args,
    ]  # fmt: skip
    assert invoke(args) == 2
    assert not journal.exists()


def test_delivery_parser_redacts_the_dedicated_runtime_token(
    private: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "synthetic-delivery-token-not-a-real-secret"
    monkeypatch.setenv("EXACLY_DELIVERY_MERGE_TOKEN", token)
    assert invoke(["key-init", "--key", str(private / "merge.key"), "--unrecognized", token]) == 2
    captured = capsys.readouterr()
    assert token not in captured.err and "[redacted]" in captured.err
    assert not (private / "merge.key").exists()


def test_unknown_native_report_status_is_not_printed_as_success(
    private: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def malformed(args: Any) -> Doc:
        return {"status": "unexpected-provider-state", "claims": dict(contracts.CLAIMS)}

    monkeypatch.setattr(delivery_io, "run", malformed)
    assert (
        invoke(["show", "--journal", str(private / "unused.sqlite3"), "--journal-id", uuid.uuid4().hex, "--json"]) == 2
    )
    captured = capsys.readouterr()
    assert captured.out == "" and "unsupported" in captured.err


def test_credential_bearing_native_report_is_withheld_without_changing_its_identity(
    private: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "synthetic-protected-delivery-token"
    monkeypatch.setenv("EXACLY_DELIVERY_MERGE_TOKEN", token)
    report = {"status": "applied", "id": contracts.digest("native receipt"), "reason": token}
    before = copy.deepcopy(report)

    def sensitive(args: Any) -> Doc:
        return report

    monkeypatch.setattr(delivery_io, "run", sensitive)
    assert (
        invoke(["show", "--journal", str(private / "unused.sqlite3"), "--journal-id", uuid.uuid4().hex, "--json"]) == 2
    )
    captured = capsys.readouterr()
    assert captured.out == "" and token not in captured.err
    assert report == before


@dataclass
class OperationalCase:
    """Real CLI/engine/native profile, with process-shared controlled provider state only."""

    merge: MergeCase
    directory: Path
    journal: Path
    journal_id: str

    def approval_args(self) -> list[str]:
        return [
            "--approval", str(self.directory / "approval.json"),
            "--approved-approval-digest", self.merge.pin.digest,
            "--issuer", self.merge.pin.issuer, "--approval-id", self.merge.pin.approval_id,
        ]  # fmt: skip

    def merge_args(self, *, approved: bool = False, apply: bool = False) -> list[str]:
        args = [
            "merge", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--request", str(self.directory / "request.json"),
            "--payload", str(self.directory / "payload.json"),
            "--policy", str(self.directory / "policy.json"),
            "--config", str(self.directory / "config.json"), "--json",
        ]  # fmt: skip
        if approved:
            args.extend(self.approval_args())
        if apply:
            args.append("--apply")
        return args

    def reconcile_args(self) -> list[str]:
        return [
            "reconcile", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--operation-key", self.merge.request["operation_key"],
            "--policy", str(self.directory / "policy.json"),
            "--config", str(self.directory / "config.json"), *self.approval_args(), "--json",
        ]  # fmt: skip

    def show_args(self) -> list[str]:
        return [
            "show", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--operation-key", self.merge.request["operation_key"], "--json",
        ]  # fmt: skip


class SharedMapping(MutableMapping[str, Any]):
    """Private test state survives native watchdog forks without a network service."""

    def __init__(self, path: Path, value: Doc) -> None:
        self.path = path
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def read(self) -> Doc:
        return cast(Doc, json.loads(self.path.read_text()))

    def __getitem__(self, key: str) -> Any:
        return self.read()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        current = self.read()
        current[key] = value
        self.path.write_text(json.dumps(current))

    def __delitem__(self, key: str) -> None:
        current = self.read()
        del current[key]
        self.path.write_text(json.dumps(current))

    def __iter__(self) -> Iterator[str]:
        return iter(self.read())

    def __len__(self) -> int:
        return len(self.read())

    def __deepcopy__(self, memo: Any) -> Doc:
        return self.read()


class SharedSequence(MutableSequence[Any]):
    def __init__(self, path: Path, value: list[Any]) -> None:
        self.path = path
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def read(self) -> list[Any]:
        return cast(list[Any], json.loads(self.path.read_text()))

    def __getitem__(self, index: Any) -> Any:
        return self.read()[index]

    def __setitem__(self, index: Any, value: Any) -> None:
        current = self.read()
        current[index] = value
        self.path.write_text(json.dumps(current))

    def __delitem__(self, index: Any) -> None:
        current = self.read()
        del current[index]
        self.path.write_text(json.dumps(current))

    def __len__(self) -> int:
        return len(self.read())

    def insert(self, index: int, value: Any) -> None:
        current = self.read()
        current.insert(index, value)
        self.path.write_text(json.dumps(current))

    def __deepcopy__(self, memo: Any) -> list[Any]:
        return self.read()


@pytest.fixture
def operational(private: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OperationalCase:
    merge = make_case(tmp_path, monkeypatch)
    # The actual watchdog runs callbacks in a child. Shared provider objects let a later,
    # independently launched reconciliation discover the first call's accepted transaction.
    for name in ("refs", "pull_request", "tags"):
        setattr(merge.opener, name, SharedMapping(private / (name + "-provider.json"), getattr(merge.opener, name)))
    for name in ("calls", "mutations"):
        setattr(merge.opener, name, SharedSequence(private / (name + "-provider.json"), getattr(merge.opener, name)))
    journal = private / "delivery.sqlite3"
    limits = {"operations": 3, "wall_time_ms": 600000, "reads": 1000, "rollbacks": 0}
    initialized = delivery_store.initialize(
        journal,
        objective=merge.request["objective"],
        limits=limits,
        native_limits={"wall_time_ms": 120000, "read_calls": 64, "write_calls": 2},
    )
    merge.authorization.update(journal={"path": str(journal), "id": initialized["id"]}, limits=limits)
    merge.pin = contracts.ApprovalPin(
        merge.authorization["issuer"], merge.authorization["approval_id"], contracts.digest(merge.authorization)
    )
    for name, document in {
        "request": merge.request,
        "payload": merge.payload,
        "config": merge.configuration,
        "policy": merge.policy,
        "approval": merge.authorization,
    }.items():
        write_document(private, name + ".json", document)

    def native(configuration: Doc, policy: Doc) -> GitHubMergeAdapter:
        return GitHubMergeAdapter(configuration, policy, opener=merge.opener)

    monkeypatch.setattr(delivery_io, "merge_adapter", native)
    return OperationalCase(merge, private, journal, initialized["id"])


@pytest.mark.parametrize("approved", [False, True])
def test_default_merge_preview_is_local_readonly_and_has_no_live_readiness_claim(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], approved: bool
) -> None:
    saved = operational.journal.read_bytes()
    inventory = set(operational.directory.iterdir())
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    Path(operational.merge.configuration["key"]["path"]).unlink()
    assert invoke(operational.merge_args(approved=approved)) == (0 if approved else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == ("preview" if approved else "approval_required")
    assert report["live_checked"] is False and report["effect_authorized"] is False
    assert report["packet"]["live_preconditions_checked"] is False
    assert not any(report["claims"].values())
    assert list(operational.merge.opener.calls) == []
    assert operational.journal.read_bytes() == saved
    assert set(operational.directory.iterdir()) == inventory


@pytest.mark.parametrize("changed", ["pin", "policy", "configuration", "request", "expired"])
def test_apply_rejects_scope_drift_before_any_provider_call_or_reservation(
    operational: OperationalCase, capsys: pytest.CaptureFixture[str], changed: str
) -> None:
    saved = operational.journal.read_bytes()
    inventory = set(operational.directory.iterdir())
    args = operational.merge_args(approved=True, apply=True)
    if changed == "pin":
        args[args.index("--approved-approval-digest") + 1] = contracts.digest("another approval")
    elif changed == "policy":
        policy = copy.deepcopy(operational.merge.policy)
        policy["minimum_approvals"] += 1
        write_document(operational.directory, "policy.json", policy)
    elif changed == "configuration":
        config = copy.deepcopy(operational.merge.configuration)
        config["actor"]["identity_approval_digest"] = contracts.digest("different operator identity review")
        write_document(operational.directory, "config.json", config)
    elif changed == "request":
        request = copy.deepcopy(operational.merge.request)
        request["subject"]["pull_request"] += 1
        write_document(operational.directory, "request.json", request)
    else:
        approval = copy.deepcopy(operational.merge.authorization)
        approval.update(not_before="2020-01-01T00:00:00Z", expires_at="2021-01-01T00:00:00Z")
        write_document(operational.directory, "approval.json", approval)
        args[args.index("--approved-approval-digest") + 1] = contracts.digest(approval)
    assert invoke(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert list(operational.merge.opener.calls) == []
    assert operational.journal.read_bytes() == saved
    assert set(operational.directory.iterdir()) == inventory


def test_cli_applies_exact_native_merge_then_replays_and_reads_history_offline(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(operational.merge_args(approved=True, apply=True)) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied" and applied["historical"] is False
    assert applied["receipt"]["effect"]["commit"] == MERGE
    assert operational.merge.opener.refs[BASE_REF] == MERGE
    assert operational.merge.opener.refs[HEAD_REF] == HEAD
    assert len(operational.merge.opener.mutations) == 1
    assert len(operational.merge.opener.writes) == 2
    assert not any(applied["claims"].values())
    saved = operational.journal.read_bytes()
    writes = copy.deepcopy(operational.merge.opener.writes)
    for name in ("config.json", "policy.json"):
        (operational.directory / name).unlink()
    Path(operational.merge.configuration["key"]["path"]).unlink()
    monkeypatch.delenv(TOKEN_ENVIRONMENT)

    def unavailable(*args: Any, **kwargs: Any) -> GitHubMergeAdapter:
        pytest.fail("historical operation acquired an adapter")

    monkeypatch.setattr(delivery_io, "merge_adapter", unavailable)
    assert invoke(operational.merge_args(approved=True, apply=True)) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["historical"] is True and replay["live_checked"] is False
    assert replay["receipt"] == applied["receipt"]
    assert invoke(operational.show_args()) == 0
    history = json.loads(capsys.readouterr().out)
    assert history["operation"]["receipt"] == applied["receipt"]
    assert operational.journal.read_bytes() == saved
    assert operational.merge.opener.writes == writes


def test_uncertain_merge_is_not_reinvoked_and_reconciliation_only_discovers_accepted_effect(
    operational: OperationalCase, capsys: pytest.CaptureFixture[str]
) -> None:
    def lose_acknowledgement(provider: Any) -> None:
        raise URLError("controlled acknowledgement lost after atomic transaction")

    operational.merge.opener.after_update = lose_acknowledgement
    assert invoke(operational.merge_args(approved=True, apply=True)) == 1
    uncertain = json.loads(capsys.readouterr().out)
    assert uncertain["status"] == "uncertain"
    assert len(operational.merge.opener.mutations) == 1
    writes = copy.deepcopy(operational.merge.opener.writes)
    assert invoke(operational.merge_args(approved=True, apply=True)) == 1
    replay = json.loads(capsys.readouterr().out)
    assert replay["historical"] is True and replay["receipt"] == uncertain["receipt"]
    assert operational.merge.opener.writes == writes
    assert invoke(operational.reconcile_args()) == 0
    discovered = json.loads(capsys.readouterr().out)
    assert discovered["status"] == "applied" and discovered["live_checked"] is True
    assert discovered["receipt"]["effect"]["commit"] == MERGE
    assert len(operational.merge.opener.mutations) == 1
    assert operational.merge.opener.writes == writes


def test_rotated_credential_recovery_cannot_apply_and_can_discover_after_scope_withdrawal(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def lose_acknowledgement(provider: Any) -> None:
        raise URLError("controlled acknowledgement lost after atomic transaction")

    operational.merge.opener.after_update = lose_acknowledgement
    assert invoke(operational.merge_args(approved=True, apply=True)) == 1
    uncertain = json.loads(capsys.readouterr().out)
    assert uncertain["status"] == "uncertain" and len(operational.merge.opener.mutations) == 1
    writes = copy.deepcopy(operational.merge.opener.writes)
    assert invoke([
        "revoke", "--journal", str(operational.journal), "--journal-id", operational.journal_id,
        "--reason", "Withdraw effects; review read-only discovery independently", "--json",
    ]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out)["revoked"] is True

    rotated_token = "controlled-rotated-delivery-token"
    config = copy.deepcopy(operational.merge.configuration)
    config["actor"].update(
        token_digest="sha256:" + hashlib.sha256(rotated_token.encode()).hexdigest(),
        identity_approval_digest=contracts.digest("independently reviewed rotated credential identity"),
    )
    monkeypatch.setenv(TOKEN_ENVIRONMENT, rotated_token)
    operational.merge.opener.expected_token = rotated_token
    write_document(operational.directory, "config.json", config)
    actual = delivery_io.merge_adapter(config, operational.merge.policy)
    original = operational.merge.authorization
    intent = uncertain["intent"]
    recovery = {
        "schema_version": contracts.VERSION,
        "profile": contracts.PROFILE,
        "kind": "delivery-read-recovery",
        "issuer": "independent delivery recovery reviewer",
        "approval_id": "separate read-only recovery review",
        "objective": copy.deepcopy(intent["request"]["objective"]),
        "journal": copy.deepcopy(intent["journal"]),
        "operation_key": intent["request"]["operation_key"],
        "intent_id": intent["id"],
        "request_digest": intent["request_digest"],
        "native_payload_digest": intent["request"]["native_payload_digest"],
        "old_configuration_digest": intent["adapter"]["configuration_digest"],
        "new_configuration_digest": actual.configuration_digest,
        "operation_profile_digest": operational.merge.payload["operation_profile_digest"],
        "policy_digest": original["policy_digest"],
        "contract": copy.deepcopy(original["contract"]),
        "limits": copy.deepcopy(original["limits"]),
        "read_limits": copy.deepcopy(config["call_limits"]["reconcile"]),
        "not_before": "2020-01-01T00:00:00Z",
        "expires_at": "2100-01-01T00:00:00Z",
    }
    assert recovery["old_configuration_digest"] != recovery["new_configuration_digest"]
    assert actual.operation_profile_digest == recovery["operation_profile_digest"]
    assert recovery["read_limits"]["write_calls"] == 0
    recovery_path = write_document(operational.directory, "read-recovery.json", recovery)

    def select_recovery(args: list[str]) -> list[str]:
        for option, value in {
            "--approval": str(recovery_path),
            "--approved-approval-digest": contracts.digest(recovery),
            "--issuer": recovery["issuer"],
            "--approval-id": recovery["approval_id"],
        }.items():
            args[args.index(option) + 1] = value
        return args

    saved = operational.journal.read_bytes()
    assert invoke(select_recovery(operational.merge_args(approved=True, apply=True))) == 2
    assert capsys.readouterr().out == ""
    assert operational.journal.read_bytes() == saved and operational.merge.opener.writes == writes
    before_reads = len(operational.merge.opener.calls)
    assert invoke(select_recovery(operational.reconcile_args())) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "applied" and recovered["receipt"]["effect"]["commit"] == MERGE
    assert recovered["receipt"]["intent_id"] == intent["id"]
    assert recovered["receipt"]["authority_digest"] == intent["authority_digest"]
    assert recovered["receipt"]["adapter"] == intent["adapter"]
    assert operational.merge.opener.writes == writes and len(operational.merge.opener.mutations) == 1
    assert all(call["method"] == "GET" for call in list(operational.merge.opener.calls)[before_reads:])
    history = delivery_store.show(operational.journal, operational.journal_id, intent["request"]["operation_key"])
    assert history["revoked"] is True
    assert history["operation"]["calls"][-1]["method"] == "reconcile"
    assert history["operation"]["calls"][-1]["approval"] == recovery


def test_local_scope_revocation_prevents_new_apply_and_preserves_inspection(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> GitHubMergeAdapter:
        pytest.fail("local revocation or history acquired a provider")

    with monkeypatch.context() as local:
        local.setattr(delivery_io, "merge_adapter", unavailable)
        assert invoke([
            "revoke", "--journal", str(operational.journal), "--journal-id", operational.journal_id,
            "--reason", "Operator withdrew this exact objective", "--lock-timeout", "0.5", "--json",
        ]) == 0  # fmt: skip
        revoked = json.loads(capsys.readouterr().out)
        assert revoked["revoked"] is True and revoked["status"] == "inspected"
    assert invoke(operational.merge_args(approved=True, apply=True)) == 2
    assert capsys.readouterr().out == ""
    assert list(operational.merge.opener.calls) == []
    with monkeypatch.context() as local:
        local.setattr(delivery_io, "merge_adapter", unavailable)
        assert invoke(operational.show_args()) == 1
        history = json.loads(capsys.readouterr().out)
        assert history["revoked"] is True and history["operation"] is None


def test_native_merge_never_falls_back_to_generic_github_credentials(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    monkeypatch.setenv("GH_TOKEN", "synthetic-generic-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-generic-github-token")
    assert invoke(operational.merge_args(approved=True, apply=True)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "prepared" and report["receipt"] is None
    assert list(operational.merge.opener.calls) == []
    assert len(operational.merge.opener.mutations) == 0


def test_native_profile_rejects_alternate_delivery_token_selector_without_reading_its_value(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    alternate = "synthetic-unselected-other-delivery-token"
    monkeypatch.setenv("EXACLY_DELIVERY_OTHER_TOKEN", alternate)
    config = copy.deepcopy(operational.merge.configuration)
    config["token_environment"] = "EXACLY_DELIVERY_OTHER_TOKEN"
    write_document(operational.directory, "config.json", config)
    saved = operational.journal.read_bytes()
    inventory = set(operational.directory.iterdir())
    assert invoke(operational.merge_args()) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and alternate not in captured.err
    assert operational.journal.read_bytes() == saved and set(operational.directory.iterdir()) == inventory
    assert list(operational.merge.opener.calls) == []


def test_reconcile_cli_cannot_replace_retained_native_request_or_payload(
    operational: OperationalCase, capsys: pytest.CaptureFixture[str]
) -> None:
    saved = operational.journal.read_bytes()
    assert invoke([*operational.reconcile_args(), "--payload", "candidate-replacement.json"]) == 2
    assert capsys.readouterr().out == ""
    assert operational.journal.read_bytes() == saved and list(operational.merge.opener.calls) == []


def test_merge_front_door_rejects_other_valid_delivery_stage_before_adapter_or_journal_work(
    operational: OperationalCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = copy.deepcopy(operational.merge.request)
    request.update(
        stage="plan",
        subject={
            "source": {"repository": request["subject"]["repository"], "revision": HEAD},
            "intent_digest": contracts.digest("independently reviewed product intent"),
        },
    )
    contracts.validate_request(request)
    write_document(operational.directory, "request.json", request)
    saved = operational.journal.read_bytes()

    def unavailable(*args: Any, **kwargs: Any) -> GitHubMergeAdapter:
        pytest.fail("merge front door constructed an adapter for another delivery stage")

    monkeypatch.setattr(delivery_io, "merge_adapter", unavailable)
    assert invoke(operational.merge_args()) == 2
    assert capsys.readouterr().out == ""
    assert operational.journal.read_bytes() == saved and list(operational.merge.opener.calls) == []
