"""The fixed publication CLI uses the durable engine and real isolated owning publisher."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import delivery_automation as delivery
from exacly import delivery_io, delivery_store
from exacly import execution_store as store
from exacly import owning_publisher as owner
from exacly import release_publication as publication
from exacly.cli import exacly_main
from release_publication_fixtures import make_case, retained

Doc = dict[str, Any]
LIMITS = {"operations": 3, "wall_time_ms": 600000, "reads": 64, "rollbacks": 0}


def invoke(arguments: list[str]) -> int:
    try:
        return exacly_main(["delivery", *arguments])
    except SystemExit as error:
        assert type(error.code) is int
        return error.code


def snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.stat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


def reapprove(case: Doc, journal: Path, journal_id: str) -> None:
    """Independent controlled bootstrap changes owner approval before pinning dispatch inputs."""
    authority = case["owner"]["delivery_authority"]
    authority.update(journal={"path": str(journal), "id": journal_id}, limits=copy.deepcopy(LIMITS))
    case["owner"]["scope"] = {
        key: copy.deepcopy(case["request"][key]) for key in ("objective", "operation_key", "subject")
    }
    configuration = case["configuration"]
    configuration["owner_configuration"]["digest"] = retained(
        Path(configuration["owner_configuration"]["path"]), case["owner"]
    )
    adapter = publication.ReleasePublicationAdapter(configuration, case["policy"])
    case["adapter"] = adapter
    case["payload"]["operation_profile_digest"] = adapter.operation_profile_digest
    case["request"]["native_payload_digest"] = delivery.digest(case["payload"])
    selected = case["request"]
    case["approval"].update(
        **copy.deepcopy(authority),
        objective=copy.deepcopy(selected["objective"]),
        operation_key=selected["operation_key"],
        request_digest=delivery.digest(selected),
        subject_digest=delivery.digest(selected["subject"]),
        resource_key=delivery.resource_key(selected),
        adapter={"id": adapter.adapter_id, "configuration_digest": adapter.configuration_digest},
    )
    operator = Path(case["root"]) / "operator"
    for name, document in {
        "request": selected,
        "payload": case["payload"],
        "policy": case["policy"],
        "config": configuration,
        "approval": case["approval"],
    }.items():
        retained(operator / (name + ".json"), document)


@dataclass
class PublicationCase:
    native: Doc
    journal: Path
    journal_id: str

    @property
    def operator(self) -> Path:
        return Path(self.native["root"]) / "operator"

    @property
    def version(self) -> Path:
        subject = self.native["request"]["subject"]
        return owner.version_path(
            self.native["owner"]["registry"], subject["destination"]["package"], subject["version"]
        )

    def approval_args(self) -> list[str]:
        approved = self.native["approval"]
        return [
            "--approval", str(self.operator / "approval.json"),
            "--approved-approval-digest", delivery.digest(approved),
            "--issuer", approved["issuer"], "--approval-id", approved["approval_id"],
        ]  # fmt: skip

    def publish_args(self, *, approved: bool = False, apply: bool = False) -> list[str]:
        arguments = [
            "publish", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--request", str(self.operator / "request.json"), "--payload", str(self.operator / "payload.json"),
            "--policy", str(self.operator / "policy.json"), "--config", str(self.operator / "config.json"), "--json",
        ]  # fmt: skip
        if approved:
            arguments.extend(self.approval_args())
        if apply:
            arguments.append("--apply")
        return arguments

    def reconcile_args(self) -> list[str]:
        return [
            "reconcile", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--operation-key", self.native["request"]["operation_key"],
            "--policy", str(self.operator / "policy.json"), "--config", str(self.operator / "config.json"),
            *self.approval_args(), "--json",
        ]  # fmt: skip

    def show_args(self) -> list[str]:
        return [
            "show", "--journal", str(self.journal), "--journal-id", self.journal_id,
            "--operation-key", self.native["request"]["operation_key"], "--json",
        ]  # fmt: skip


@pytest.fixture
def publication_case(tmp_path: Path) -> PublicationCase:
    case = make_case(tmp_path / "private-publication")
    journal = Path(case["owner"]["delivery_authority"]["journal"]["path"])
    metadata = delivery_store.initialize(
        journal,
        objective=case["request"]["objective"],
        limits=copy.deepcopy(LIMITS),
        native_limits={"wall_time_ms": 120000, "read_calls": 1, "write_calls": 1},
    )
    reapprove(case, journal, metadata["id"])
    return PublicationCase(case, journal, metadata["id"])


def prohibit_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("publication preview or blocked preflight launched an owning workflow")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def audit_real_dispatch(case: PublicationCase, monkeypatch: pytest.MonkeyPatch, *, discard_reply: bool = False) -> Path:
    """Keep the real Python -I owner and only audit stdin release or discard its completed reply."""
    actual_popen = subprocess.Popen
    audit = case.operator / "owning-dispatch.jsonl"

    class AuditedProcess:
        def __init__(self, arguments: list[str], **kwargs: Any) -> None:
            assert arguments[:4] == [sys.executable, "-I", "-m", "exacly.owning_publisher"]
            assert arguments[4:] == [
                "--configuration", case.native["configuration"]["owner_configuration"]["path"],
                "--configuration-digest", case.native["configuration"]["owner_configuration"]["digest"],
            ]  # fmt: skip
            self.process: subprocess.Popen[bytes] = actual_popen(arguments, **kwargs)

        @property
        def returncode(self) -> int | None:
            return self.process.returncode

        def communicate(self, data: bytes, *, timeout: float) -> tuple[bytes, bytes | None]:
            envelope = json.loads(data)
            history = delivery_store.show(case.journal, case.journal_id, case.native["request"]["operation_key"])
            operation = history["operation"]
            assert operation["intent"] == envelope["intent"]
            assert operation["payload"] == envelope["payload"]
            assert operation["approval"] == envelope["authorization"]
            assert operation["phase"] == "may_have_started"
            assert operation["calls"][-1]["method"] == "invoke"
            assert operation["calls"][-1]["owner"] is not None
            assert stat.S_IMODE(case.journal.stat().st_mode) == 0o600
            assert not case.version.exists()
            descriptor = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(
                    json.dumps({"intent": operation["intent"]["id"], "private_intent_before_owner": True}) + "\n"
                )
                output.flush()
                os.fsync(output.fileno())
            reply, error = self.process.communicate(data, timeout=timeout)
            return (b"", error) if discard_reply else (reply, error)

        def kill(self) -> None:
            self.process.kill()

        def wait(self) -> int:
            return self.process.wait()

    monkeypatch.setattr(subprocess, "Popen", AuditedProcess)
    return audit


@pytest.mark.parametrize("approved", [False, True])
def test_publication_preview_is_local_and_cannot_claim_current_verification_or_publish(
    publication_case: PublicationCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    approved: bool,
) -> None:
    case = publication_case
    prohibit_dispatch(monkeypatch)
    for field in ("key", "verification_key"):
        Path(case.native["owner"][field]["path"]).unlink()
    Path(case.native["owner"]["verification"]["path"]).unlink()
    blob = case.native["payload"]["inputs"][0]["blob"].removeprefix("sha256:")
    (Path(case.native["owner"]["artifact_store"]["path"]) / blob).unlink()
    registry = snapshot(Path(case.native["owner"]["registry"]["path"]))
    journal = case.journal.read_bytes()
    assert invoke(case.publish_args(approved=approved)) == (0 if approved else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == ("preview" if approved else "approval_required")
    assert report["live_checked"] is False and report["effect_authorized"] is False
    assert report["packet"]["live_preconditions_checked"] is False
    assert report["packet"]["adapter"]["id"] == publication.PROFILE
    assert not any(report["claims"].values())
    assert case.journal.read_bytes() == journal
    assert snapshot(Path(case.native["owner"]["registry"]["path"])) == registry


@pytest.mark.parametrize("damage", ["pin", "issuer", "expired", "version", "destination", "source", "policy"])
def test_wrong_or_expired_publication_scope_is_rejected_before_effect_or_budget_reservation(
    publication_case: PublicationCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    case = publication_case
    prohibit_dispatch(monkeypatch)
    arguments = case.publish_args(approved=True, apply=True)
    if damage == "pin":
        arguments[arguments.index("--approved-approval-digest") + 1] = delivery.digest("another external decision")
    elif damage == "issuer":
        arguments[arguments.index("--issuer") + 1] = "another independently selected issuer"
    elif damage == "expired":
        approval = copy.deepcopy(case.native["approval"])
        approval["expires_at"] = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
        retained(case.operator / "approval.json", approval)
        arguments[arguments.index("--approved-approval-digest") + 1] = delivery.digest(approval)
    elif damage == "policy":
        policy = copy.deepcopy(case.native["policy"])
        policy["contract"]["model"] = delivery.digest("different reviewed acceptance meaning")
        retained(case.operator / "policy.json", policy)
    else:
        request = copy.deepcopy(case.native["request"])
        if damage == "version":
            request["subject"]["version"] = "9.9.9"
        elif damage == "destination":
            request["subject"]["destination"]["package"] = "other-package"
        else:
            request["subject"]["source"]["revision"] = "f" * 40
        retained(case.operator / "request.json", request)
    registry = snapshot(Path(case.native["owner"]["registry"]["path"]))
    journal = case.journal.read_bytes()
    inventory = set(case.operator.iterdir())
    assert invoke(arguments) == 2
    assert capsys.readouterr().out == ""
    assert case.journal.read_bytes() == journal and set(case.operator.iterdir()) == inventory
    assert snapshot(Path(case.native["owner"]["registry"]["path"])) == registry


def test_real_isolated_owner_runs_only_after_private_engine_intent_and_publishes_exact_prebuilt_bytes(
    publication_case: PublicationCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = publication_case
    audit = audit_real_dispatch(case, monkeypatch)
    assert invoke(case.publish_args(approved=True, apply=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "applied" and report["historical"] is False
    assert report["receipt"]["stage"] == "publish" and not any(report["claims"].values())
    dispatches = [json.loads(line) for line in audit.read_text().splitlines()]
    assert dispatches == [{"intent": report["intent"]["id"], "private_intent_before_owner": True}]
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    assert (case.version / "claim.json").is_file() and (case.version / "published").is_dir()
    for path in case.version.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    subject = case.native["request"]["subject"]
    actual = owner.read_published_artifact(
        case.native["owner"]["registry"],
        subject["destination"]["package"],
        subject["version"],
        subject["artifacts"][0],
        subject["source"],
        case.native["owner"]["key"],
        subject["workflow"],
        deadline=store.Deadline(30),
    )
    assert actual == case.native["artifact_bytes"]
    history = delivery_store.show(case.journal, case.journal_id, case.native["request"]["operation_key"])
    proof = history["operation"]["calls"][-1]["result"]["provenance"]
    assert proof["version_state"] == "published" and proof["retrieved_artifacts"] == subject["artifacts"]
    assert proof["receipt"]["claims"] == delivery.CLAIMS


@pytest.mark.parametrize(
    "damage", ["missing_artifact", "artifact_bytes", "verification_identity", "verification_signature"]
)
def test_changed_prebuilt_bytes_or_independent_verification_cannot_start_the_publisher(
    publication_case: PublicationCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    case = publication_case
    prohibit_dispatch(monkeypatch)
    if damage in {"missing_artifact", "artifact_bytes"}:
        blob = case.native["payload"]["inputs"][0]["blob"].removeprefix("sha256:")
        selected = Path(case.native["owner"]["artifact_store"]["path"]) / blob
        if damage == "missing_artifact":
            selected.unlink()
        else:
            retained(selected, b"substituted prebuilt bytes")
    else:
        proof = copy.deepcopy(case.native["verification"])
        if damage == "verification_identity":
            proof["source"]["revision"] = "f" * 40
            proof = owner.sign_verification(
                {key: value for key, value in proof.items() if key != "signature"},
                case.native["owner"]["verification_key"],
            )
        else:
            proof["signature"] = "0" * 64
        # Even independently selecting these exact bytes cannot make a wrong-source or
        # unauthenticated verifier statement an applicable publication prerequisite.
        case.native["owner"]["verification"]["digest"] = retained(
            Path(case.native["owner"]["verification"]["path"]), proof
        )
        case.native["payload"]["verification_digest"] = case.native["owner"]["verification"]["digest"]
        reapprove(case.native, case.journal, case.journal_id)
    registry = snapshot(Path(case.native["owner"]["registry"]["path"]))
    assert invoke(case.publish_args(approved=True, apply=True)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "prepared" and report["receipt"] is None
    assert not case.version.exists()
    assert snapshot(Path(case.native["owner"]["registry"]["path"])) == registry


def test_exact_terminal_publication_replay_and_history_do_not_require_original_configs_keys_or_factory(
    publication_case: PublicationCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = publication_case
    assert invoke(case.publish_args(approved=True, apply=True)) == 0
    applied = json.loads(capsys.readouterr().out)
    before_journal = case.journal.read_bytes()
    before_registry = snapshot(Path(case.native["owner"]["registry"]["path"]))
    for path in (
        case.operator / "config.json",
        case.operator / "policy.json",
        Path(case.native["configuration"]["owner_configuration"]["path"]),
        Path(case.native["owner"]["key"]["path"]),
        Path(case.native["owner"]["verification_key"]["path"]),
    ):
        path.unlink()

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("terminal publication history acquired an adapter or owning workflow")

    monkeypatch.setattr(delivery_io, "publication_adapter", unavailable)
    monkeypatch.setattr(delivery_io, "merge_adapter", unavailable)
    monkeypatch.setattr(subprocess, "Popen", unavailable)
    assert invoke(case.publish_args(approved=True, apply=True)) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["historical"] is True and replay["live_checked"] is False
    assert replay["receipt"] == applied["receipt"]
    assert invoke(case.reconcile_args()) == 0
    assert json.loads(capsys.readouterr().out)["receipt"] == applied["receipt"]
    assert invoke(case.show_args()) == 0
    history = json.loads(capsys.readouterr().out)
    assert history["operation"]["receipt"] == applied["receipt"]
    assert case.journal.read_bytes() == before_journal
    assert snapshot(Path(case.native["owner"]["registry"]["path"])) == before_registry


def test_lost_real_owner_reply_is_not_republished_and_retained_stage_routes_discovery(
    publication_case: PublicationCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = publication_case
    audit = audit_real_dispatch(case, monkeypatch, discard_reply=True)
    assert invoke(case.publish_args(approved=True, apply=True)) == 1
    uncertain = json.loads(capsys.readouterr().out)
    assert uncertain["status"] == "uncertain" and (case.version / "published").is_dir()
    original_registry = snapshot(Path(case.native["owner"]["registry"]["path"]))
    original_dispatch = audit.read_bytes()
    assert invoke(case.publish_args(approved=True, apply=True)) == 1
    replay = json.loads(capsys.readouterr().out)
    assert replay["historical"] is True and replay["receipt"] == uncertain["receipt"]

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("publication reconciliation selected merge or reinvoked its owner")

    monkeypatch.setattr(delivery_io, "merge_adapter", unavailable)
    monkeypatch.setattr(subprocess, "Popen", unavailable)
    assert invoke(case.reconcile_args()) == 0
    discovered = json.loads(capsys.readouterr().out)
    assert discovered["status"] == "applied" and discovered["live_checked"] is True
    assert discovered["receipt"]["effect"]["version"] == case.native["request"]["subject"]["version"]
    assert snapshot(Path(case.native["owner"]["registry"]["path"])) == original_registry
    assert audit.read_bytes() == original_dispatch


def test_publishing_a_new_version_preserves_an_unrelated_authenticated_version(
    publication_case: PublicationCase, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = publication_case
    other = make_case(tmp_path / "independently-approved-other-version", data=b"earlier immutable release")
    other["owner"]["registry"] = copy.deepcopy(case.native["owner"]["registry"])
    other["request"]["subject"]["destination"] = copy.deepcopy(case.native["request"]["subject"]["destination"])
    other["request"]["subject"]["version"] = "0.9.0"
    other["request"]["operation_key"] = delivery.digest("independently approved previous version")
    reapprove(other, case.journal, case.journal_id)
    previous = PublicationCase(other, case.journal, case.journal_id)
    assert invoke(previous.publish_args(approved=True, apply=True)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    before = snapshot(previous.version)
    assert invoke(case.publish_args(approved=True, apply=True)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert snapshot(previous.version) == before
    subject = other["request"]["subject"]
    assert (
        owner.read_published_artifact(
            other["owner"]["registry"],
            subject["destination"]["package"],
            subject["version"],
            subject["artifacts"][0],
            subject["source"],
            other["owner"]["key"],
            subject["workflow"],
            deadline=store.Deadline(30),
        )
        == other["artifact_bytes"]
    )
