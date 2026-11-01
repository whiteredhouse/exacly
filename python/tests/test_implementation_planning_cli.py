"""Immutable implementation proposals retain their exact inputs without granting effects."""

from __future__ import annotations

import copy
import json
import os
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import evidence_retention, implementation_planning, implementation_planning_io, planning
from exacly.cli import exacly_main
from test_applicability import WHEN
from test_implementation_planning import Inputs, inputs, invalid_observation_inputs

Doc = dict[str, Any]


@dataclass
class Scenario:
    model: Path
    intent: Path
    policy: Path
    target: Path
    out: Path
    intent_doc: Doc
    policy_doc: Doc
    intent_pin: str
    policy_pin: str

    def args(self, operation: str = "propose", *, bundle: Path | None = None) -> list[str]:
        result = [
            "implementation", operation, "-m", str(self.model), "--intent", str(self.intent),
            "--policy", str(self.policy), "--target", str(self.target), "--date", WHEN.isoformat(),
            "--approved-intent-digest", self.intent_pin, "--approved-policy-digest", self.policy_pin,
            "--issuer", self.policy_doc["issuer"], "--approval-id", self.policy_doc["approval_id"], "--json",
        ]  # fmt: skip
        return result + (["--out", str(self.out)] if operation == "propose" else ["--bundle", str(bundle or self.out)])

    def revise_pins(self, *, new_root: bool = False) -> None:
        """Fixture-only independent review; a new lineage needs its own explicit genesis pin."""
        if new_root:
            loaded = implementation_planning_io.load_inputs(
                [self.model], intent=self.intent, policy=self.policy, target=self.target
            )
            from exacly.applicability import Target

            self.policy_doc["genesis_contract_digest"] = implementation_planning.genesis_digest(
                loaded.documents, self.intent_doc, self.policy_doc, Target(loaded.target), WHEN
            )
        self.intent.write_text(json.dumps(self.intent_doc))
        self.policy.write_text(json.dumps(self.policy_doc))
        self.intent_pin = planning.digest(self.intent_doc)
        self.policy_pin = planning.digest(self.policy_doc)


def scenario(tmp_path: Path, *, selected_inputs: Inputs | None = None) -> Scenario:
    docs, intent, policy, target = inputs() if selected_inputs is None else selected_inputs
    model, controlled = tmp_path / "model", tmp_path / "controlled"
    model.mkdir()
    controlled.mkdir()
    for name, document in docs.items():
        (model / f"{name}.json").write_text(json.dumps(document, indent=1))
    intent_path, policy_path, target_path = (controlled / f"{name}.json" for name in ("intent", "policy", "target"))
    intent_path.write_text(json.dumps(intent, indent=3) + "\n")
    policy_path.write_text(json.dumps(policy, indent=2) + "\n")
    target_path.write_text(json.dumps(target.data))
    return Scenario(
        model,
        intent_path,
        policy_path,
        target_path,
        tmp_path / "retained" / "revision-1",
        intent,
        policy,
        planning.digest(intent),
        planning.digest(policy),
    )


def contents(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_propose_retains_exact_inputs_and_check_is_read_only_without_network_or_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = scenario(tmp_path)
    originals = contents(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("implementation planning attempted a network call or process effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert exacly_main(selected.args()) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["status"] == "work_proposed" and proposal["advisory"] is True
    assert proposal["tasks"] and not any(proposal["claims"].values())
    assert implementation_planning_io.load_bundle(selected.out) == proposal
    manifest = json.loads((selected.out / "manifest.json").read_text())
    for entry in manifest["inputs"]:
        assert (selected.out / entry["path"]).read_bytes() == Path(entry["source"]).read_bytes()
    for name, data in originals.items():
        assert (tmp_path / name).read_bytes() == data
    before_check = contents(tmp_path)
    assert exacly_main(selected.args("check")) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "current" and checked["ready_for_separate_execution_review"] is True
    assert not any(checked["claims"].values())
    assert contents(tmp_path) == before_check


@pytest.mark.parametrize("mask", [0o022, 0o000, 0o777])
def test_proposal_directories_and_files_are_private_independently_of_umask(tmp_path: Path, mask: int) -> None:
    selected = scenario(tmp_path)
    previous = os.umask(mask)
    try:
        assert exacly_main(selected.args()) == 0
    finally:
        os.umask(previous)
    for path in (selected.out.parent, selected.out, *selected.out.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_complete_bundle_remains_independently_readable_after_original_inputs_disappear(tmp_path: Path) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    expected = implementation_planning_io.load_bundle(selected.out)
    for path in (selected.intent, selected.policy, selected.target, *selected.model.iterdir()):
        path.unlink()
    assert implementation_planning_io.load_bundle(selected.out) == expected


def test_existing_complete_and_empty_destinations_are_never_replaced(tmp_path: Path) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    saved = contents(selected.out)
    assert exacly_main(selected.args()) == 2
    assert contents(selected.out) == saved
    selected.out = tmp_path / "already-empty"
    selected.out.mkdir()
    assert exacly_main(selected.args()) == 2
    assert list(selected.out.iterdir()) == []


@pytest.mark.parametrize("location", ["model", "model-symlink", "input", "dangling-output-symlink"])
def test_output_paths_cannot_alias_inputs_or_model_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    selected = scenario(tmp_path)
    if location == "model":
        selected.out = selected.model / "new-report"
    elif location == "model-symlink":
        alias = tmp_path / "model-alias"
        alias.symlink_to(selected.model, target_is_directory=True)
        selected.out = alias / "new-report"
    elif location == "input":
        selected.out = selected.intent
    else:
        selected.out = tmp_path / "output-alias"
        selected.out.symlink_to(tmp_path / "absent")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsafe output was not refused before proposal generation")

    monkeypatch.setattr(implementation_planning, "create", forbidden)
    assert exacly_main(selected.args()) == 2


def test_concurrent_empty_destination_creation_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = scenario(tmp_path)
    mkdir = Path.mkdir

    def race(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == selected.out:
            mkdir(path, *args, **kwargs)
            raise FileExistsError("another invocation reserved this destination")
        mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race)
    assert exacly_main(selected.args()) == 2
    assert selected.out.is_dir() and list(selected.out.iterdir()) == []


@pytest.mark.parametrize("content", ['{"id": 1, "id": 2}', "base: &a []\ncopy: *a", '{"number": NaN}'])
def test_ambiguous_or_nonfinite_inputs_are_rejected_without_output(tmp_path: Path, content: str) -> None:
    selected = scenario(tmp_path)
    selected.intent = selected.intent.with_suffix(".yaml" if "&a" in content else ".json")
    selected.intent.write_text(content)
    assert exacly_main(selected.args()) == 2
    assert not selected.out.exists()


@pytest.mark.parametrize("kind", ["oversize", "depth", "fifo"])
def test_input_reads_are_bounded_and_regular(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    selected = scenario(tmp_path)
    if kind == "oversize":
        monkeypatch.setattr(implementation_planning_io, "MAX_DOCUMENT_BYTES", 512)
    elif kind == "depth":
        selected.intent.write_text("[" * 70 + "0" + "]" * 70)
    else:
        selected.intent.unlink()
        os.mkfifo(selected.intent)
    assert exacly_main(selected.args()) == 2
    assert not selected.out.exists()


@pytest.mark.parametrize("stage", ["manifest", "marker"])
def test_write_failure_keeps_private_incomplete_output_without_a_committed_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    selected = scenario(tmp_path)
    write = evidence_retention.write_private

    def fail_write(path: Path, data: bytes) -> None:
        if path.name == "manifest.json":
            raise OSError("injected manifest storage failure")
        write(path, data)

    def fail_link(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected proposal commit failure")

    if stage == "manifest":
        monkeypatch.setattr(evidence_retention, "write_private", fail_write)
    else:
        monkeypatch.setattr(os, "link", fail_link)
    assert exacly_main(selected.args()) == 2
    assert selected.out.is_dir() and not (selected.out / "proposal.json").exists()
    with pytest.raises(OSError):
        implementation_planning_io.load_bundle(selected.out)


def test_commit_marker_is_the_final_fallible_storage_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = scenario(tmp_path)
    link, sync = os.link, evidence_retention.sync_directory
    committed: list[bool] = []

    def mark(*args: Any, **kwargs: Any) -> None:
        link(*args, **kwargs)
        committed.append(True)

    def guarded_sync(path: Path) -> None:
        assert not committed, "fallible storage work followed the complete marker"
        sync(path)

    monkeypatch.setattr(os, "link", mark)
    monkeypatch.setattr(evidence_retention, "sync_directory", guarded_sync)
    assert exacly_main(selected.args()) == 0
    assert committed == [True]


def test_snapshots_use_evaluated_bytes_even_if_source_changes_during_proposal_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = scenario(tmp_path)
    original = selected.intent.read_bytes()
    create = implementation_planning.create

    def racing_create(*args: Any, **kwargs: Any) -> Doc:
        answer = create(*args, **kwargs)
        selected.intent.write_text('{"unapproved": "concurrently changed"}')
        return answer

    monkeypatch.setattr(implementation_planning, "create", racing_create)
    assert exacly_main(selected.args()) == 0
    manifest = json.loads((selected.out / "manifest.json").read_text())
    retained = next(entry for entry in manifest["inputs"] if entry["role"] == "intent")
    assert (selected.out / retained["path"]).read_bytes() == original
    monkeypatch.setattr(implementation_planning, "create", create)
    assert implementation_planning_io.load_bundle(selected.out)["approval"]["intent_digest"] == selected.intent_pin
    assert exacly_main(selected.args("check")) == 2


@pytest.mark.parametrize("tamper", ["snapshot", "proposal", "escape", "symlink", "role", "role-shape"])
def test_bundle_check_rejects_tampering_and_escaping_retained_paths(tmp_path: Path, tamper: str) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    manifest_path = selected.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["inputs"][0]
    retained = selected.out / entry["path"]
    if tamper == "snapshot":
        retained.write_bytes(retained.read_bytes() + b" ")
    elif tamper == "proposal":
        (selected.out / "proposal.json").write_text('{"advisory": false}')
    elif tamper == "symlink":
        retained.unlink()
        retained.symlink_to(selected.model / "config.json")
    else:
        if tamper == "escape":
            entry["path"] = "../controlled/intent.json"
        elif tamper == "role-shape":
            entry["role"] = []
        else:
            entry["role"] = "intent"
        manifest.pop("digest")
        manifest["digest"] = planning.digest(manifest)
        manifest_path.write_text(json.dumps(manifest))
    assert exacly_main(selected.args("check")) == 2


def test_bundle_rejects_unrelated_snapshots_even_when_the_manifest_is_rehashed(tmp_path: Path) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    manifest_path = selected.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["inputs"] if item["role"] == "intent")
    modified = copy.deepcopy(selected.intent_doc)
    modified["id"] = "another-product-change"
    raw = json.dumps(modified).encode()
    (selected.out / entry["path"]).write_bytes(raw)
    import hashlib

    entry.update(
        bytes=len(raw),
        raw_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        document_digest=planning.digest(modified),
    )
    manifest.pop("digest")
    manifest["digest"] = planning.digest(manifest)
    manifest_path.write_text(json.dumps(manifest))
    assert exacly_main(selected.args("check")) == 2


def test_explicit_revision_retains_predecessor_without_overwriting_previous_bundle(tmp_path: Path) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    earlier_path = selected.out / "proposal.json"
    earlier = implementation_planning_io.load_bundle(selected.out)
    saved = contents(selected.out)
    selected.intent_doc.update(revision=2, supersedes=earlier["digest"])
    selected.intent_doc["goals"][0]["findings"][0]["reason"] = "A separately reviewed explicit revision."
    selected.revise_pins()
    first_bundle = selected.out
    selected.out = selected.out.parent / "revision-2"
    assert exacly_main([*selected.args(), "--previous", str(earlier_path)]) == 0
    latest = implementation_planning_io.load_bundle(selected.out)
    assert latest["id"] == earlier["id"] and latest["revision"] == 2
    assert latest["supersedes"] == earlier["digest"] and latest["digest"] != earlier["digest"]
    assert contents(first_bundle) == saved
    assert exacly_main([*selected.args("check"), "--previous", str(earlier_path)]) == 0


def test_changed_initial_intent_requires_explicit_approval_of_a_distinct_genesis(tmp_path: Path) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    earlier = implementation_planning_io.load_bundle(selected.out)
    first_bundle, saved = selected.out, contents(selected.out)
    selected.out = selected.out.parent / "new-lineage"
    selected.intent_doc["goals"][0]["findings"][0]["reason"] = "A newly reviewed initial finding."
    selected.revise_pins()
    # Merely selecting the changed intent pin cannot replace the scope's approved genesis.
    assert exacly_main(selected.args()) == 2
    assert not selected.out.exists()
    selected.revise_pins(new_root=True)
    assert exacly_main(selected.args()) == 0
    latest = implementation_planning_io.load_bundle(selected.out)
    assert latest["id"] != earlier["id"]
    assert latest["revision"] == 1 and latest["supersedes"] is None
    assert contents(first_bundle) == saved


def test_new_observations_cannot_silently_reset_the_approved_initial_contract(tmp_path: Path) -> None:
    from test_applicability import documents

    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    saved, first_bundle = contents(selected.out), selected.out
    (selected.model / "new-state.json").write_text(json.dumps(documents()["state"]))
    selected.out = selected.out.parent / "unreviewed-observation-reset"
    assert exacly_main(selected.args()) == 2
    assert not selected.out.exists()
    assert contents(first_bundle) == saved


def test_contract_change_proposals_stay_blocked_and_unapproved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = scenario(tmp_path)
    selected.intent_doc["proposed_contract_changes"] = [
        {"kind": "tests", "record": "REQ-1", "description": "Proposed new checks", "content": {"case": "review me"}}
    ]
    selected.revise_pins(new_root=True)
    assert exacly_main(selected.args()) == 1
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["status"] == "blocked"
    assert proposal["proposed_contract_changes"][0]["review_status"] == "unapproved"
    assert not proposal["claims"]["contract_changes_approved"]
    assert exacly_main(selected.args("check")) == 1
    assert not json.loads(capsys.readouterr().out)["ready_for_separate_execution_review"]


def test_invalid_observations_keep_tasks_but_block_dependency_and_separate_execution_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = scenario(tmp_path, selected_inputs=invalid_observation_inputs())
    # This fixture independently pins its bad observation snapshot. The failure must be
    # the actual validation/dependency result rather than an unrelated genesis mismatch.
    assert exacly_main(selected.args()) == 1
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["status"] == "blocked"
    assert any(task["record"] == "REQ-2" for task in proposal["tasks"])
    assert any(blocker["code"] == "invalid_observations" for blocker in proposal["blockers"])
    prerequisite = next(item for item in proposal["observations"] if item["record"] == "REQ-1")
    assert prerequisite["validation"]["conforms"] is False
    assert prerequisite["usable_as_prerequisite"] is False
    assert implementation_planning_io.load_bundle(selected.out) == proposal
    assert exacly_main(selected.args("check")) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "blocked" and not checked["ready_for_separate_execution_review"]


def test_cli_requires_explicit_model_selection(tmp_path: Path) -> None:
    selected = scenario(tmp_path)
    args = selected.args()
    position = args.index("-m")
    del args[position : position + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not selected.out.exists()


def test_serialized_output_limit_is_checked_before_reserving_a_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = scenario(tmp_path)
    loaded = implementation_planning_io.load_inputs(
        [selected.model], intent=selected.intent, policy=selected.policy, target=selected.target
    )
    from exacly.applicability import Target

    proposal = implementation_planning.create(
        loaded.documents, loaded.intent, loaded.policy, Target(loaded.target), WHEN,
        approved_intent_digest=selected.intent_pin, approved_policy_digest=selected.policy_pin,
        issuer=selected.policy_doc["issuer"], approval_id=selected.policy_doc["approval_id"],
    )  # fmt: skip
    monkeypatch.setattr(implementation_planning_io, "MAX_DOCUMENT_BYTES", 512)
    with pytest.raises(ValueError, match="document size limit"):
        implementation_planning_io.retain(selected.out, loaded, proposal)
    assert not selected.out.exists()


def test_bundle_model_inventory_uses_the_same_bound_as_input_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = scenario(tmp_path)
    assert exacly_main(selected.args()) == 0
    manifest_path = selected.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    extra = copy.deepcopy(next(entry for entry in manifest["inputs"] if entry["role"] == "model"))
    extra["path"] = "inputs/model-0001.json"
    (selected.out / extra["path"]).write_bytes((selected.out / "inputs/model-0000.json").read_bytes())
    manifest["inputs"].append(extra)
    manifest.pop("digest")
    manifest["digest"] = planning.digest(manifest)
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(implementation_planning_io, "MAX_DOCUMENTS", 1)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("bundle inventory bound was not checked before native proposal reproduction")

    monkeypatch.setattr(implementation_planning, "create", forbidden)
    with pytest.raises(ValueError, match="model input inventory"):
        implementation_planning_io.load_bundle(selected.out)
