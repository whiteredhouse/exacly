"""Reviewed handoff integration through actual executor journals and a sealed Git destination."""

from __future__ import annotations

import copy
import shutil
import subprocess
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import handoff, handoff_git, isolation, planning
from handoff_fixtures import HandoffCase, make_handoff_scenario, read_git_file, repository_bytes
from handoff_fixtures import handoff_case as handoff_case

Doc = dict[str, Any]


def test_preview_identifies_exact_changes_and_commit_without_any_storage_effect(handoff_case: HandoffCase) -> None:
    case = handoff_case
    before = case.authoritative_bytes(), case.journal.read_bytes(), repository_bytes(case.repository)
    proposal = case.preview()
    assert proposal["status"] == "ready" and proposal["dry_run"] is True, proposal
    authorization = proposal["authorization_template"]
    assert authorization["source_receipt"] == case.execution["receipt"]["digest"]
    assert authorization["build_id"] == case.execution["receipt"]["build"]["id"]
    assert authorization["base_revision"] == case.request.base_revision
    assert authorization["ref"] == case.request.ref and authorization["expected_revision"] is None
    prepared = proposal["proposal"]["prepared"]
    assert "objects" not in prepared
    assert [change["path"] for change in prepared["changes"]] == ["src/service.py"]
    assert prepared["changes"][0]["after"]["size"] == len(case.payload)
    assert authorization["tree"] == prepared["tree"] and authorization["commit"] == prepared["commit"]
    assert case.preview()["authorization_template"] == authorization
    assert handoff_git.read_ref(case.repository, case.request.ref) is None
    assert (case.authoritative_bytes(), case.journal.read_bytes(), repository_bytes(case.repository)) == before


def test_publication_records_provenance_and_preserves_unrelated_base_and_authority(handoff_case: HandoffCase) -> None:
    case = handoff_case
    authority = case.authoritative_bytes()
    authorization = case.authorization
    result = case.apply(authorization)
    assert result["status"] == "published", result
    receipt = result["receipt"]
    assert receipt["source_receipt"] == case.execution["receipt"]["digest"]
    assert receipt["build"] == case.execution["receipt"]["build"]
    assert receipt["authorization"] == authorization
    assert receipt["commit"] == authorization["commit"]
    assert receipt["base_revision"] == case.request.base_revision
    assert receipt["target"] == {**case.item.request.target.data, "revision": receipt["commit"]}
    assert receipt["claims"] == {
        "artifacts_to_git_verified": True,
        "evidence_admitted": False,
        "completion_authorized": False,
        "remote_pushed": False,
        "binding_changed": False,
    }
    assert handoff_git.read_ref(case.repository, case.request.ref) == receipt["commit"]
    assert read_git_file(case.repository, receipt["commit"], "src/service.py") == case.payload
    assert read_git_file(case.repository, receipt["commit"], "README.md") == b"preserve this unrelated base file\n"
    source = case.item.dispatch.source
    assert read_git_file(case.repository, receipt["commit"], source.workflow_path) == source.workflow
    assert case.authoritative_bytes() == authority
    assert receipt["digest"] == planning.digest({key: value for key, value in receipt.items() if key != "digest"})
    assert handoff.show(case.journal, result["handoff_id"])["receipt"] == receipt
    assert "objects" not in result["proposal"]["prepared"]


def test_binary_artifacts_are_retained_as_exact_git_blob_bytes(
    dispatch_scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "placeholder-runtime"
    runtime_root.mkdir(mode=0o700)
    payload = b"\0\xffbinary\r\n\x80\x00"
    case = make_handoff_scenario(
        dispatch_scenario,
        isolation.Runtime(runtime_root, "sha256:" + "1" * 64, "/usr/bin/python3"),
        tmp_path,
        monkeypatch,
        payload=payload,
    )
    result = case.apply()
    assert result["status"] == "published", result
    assert read_git_file(case.repository, result["receipt"]["commit"], "src/service.py") == payload


@pytest.mark.parametrize("damage", ["bytes", "missing", "symlink", "parent_symlink", "hardlink"])
def test_artifact_drift_after_review_never_prepares_or_publishes(
    handoff_case: HandoffCase, tmp_path: Path, damage: str
) -> None:
    case = handoff_case
    authorization = case.authorization
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    if damage == "bytes":
        case.artifact.write_bytes(b"changed after review")
    elif damage == "parent_symlink":
        directory = case.artifact.parent
        moved = tmp_path / "unapproved-artifact-directory"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    else:
        content = case.artifact.read_bytes()
        case.artifact.unlink()
        if damage in {"symlink", "hardlink"}:
            other = tmp_path / "unapproved.txt"
            other.write_bytes(content)
            other.chmod(0o600)
            if damage == "symlink":
                case.artifact.symlink_to(other)
            else:
                case.artifact.hardlink_to(other)
    with pytest.raises((ValueError, OSError)):
        case.apply(authorization)
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before
    assert handoff.show(case.journal, authorization["handoff_id"])["status"] == "missing"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_invalid_unrelated_base_objects_block_review_and_old_approval_without_effects(
    handoff_case: HandoffCase, damage: str, operation: str
) -> None:
    case = handoff_case
    authorization = case.authorization
    listed = subprocess.check_output(
        ["/usr/bin/git", "--git-dir=" + str(case.repository), "ls-tree", case.request.base_revision, "README.md"]
    )
    identity = listed.split()[2].decode()
    source = case.repository / "objects" / identity[:2] / identity[2:]
    if damage == "missing":
        source.unlink()
    else:
        source.write_bytes(zlib.compress(b"blob 7\0changed"))
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    with pytest.raises(ValueError, match="trusted Git operation failed"):
        if operation == "preview":
            case.preview()
        else:
            case.apply(authorization)
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before
    assert handoff.show(case.journal, authorization["handoff_id"])["status"] == "missing"
    assert handoff_git.read_ref(case.repository, case.request.ref) is None


@pytest.mark.parametrize(
    "mapping",
    [
        {"artifacts/result.txt": "../escape"},
        {"artifacts/result.txt": "/absolute"},
        {"artifacts/result.txt": ".git/config"},
        {"artifacts/result.txt": "src/.GiT/config"},
        {"artifacts/result.txt": "tests/unapproved.py"},
        {"artifacts/missing": "src/service.py"},
        {"stdout.bin": "src/service.py"},
        {"artifacts/result.txt": "src/a", "artifacts/another": "src/a"},
        {"artifacts/result.txt": "src/a", "artifacts/another": "src/a/b"},
    ],
)
def test_unreviewed_or_ambiguous_artifact_destinations_are_rejected_before_effects(
    handoff_case: HandoffCase, mapping: dict[str, str]
) -> None:
    case = handoff_case
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    with pytest.raises((ValueError, OSError)):
        case.preview(request=replace(case.request, mappings=mapping))
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


@pytest.mark.parametrize("field", ["journal_id", "executor_journal_id", "repository_id"])
def test_storage_instance_identity_is_part_of_the_reviewed_authority(handoff_case: HandoffCase, field: str) -> None:
    case = handoff_case
    policy = copy.deepcopy(case.request.policy)
    policy["storage"][field] = "00112233445546778899aabbccddeeff"
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    with pytest.raises((ValueError, OSError)):
        case.preview(request=replace(case.request, policy=policy))
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


def test_existing_branch_update_requires_the_exact_reviewed_base(handoff_case: HandoffCase) -> None:
    case = handoff_case
    request = replace(case.request, ref="refs/heads/base", expected_revision=case.request.base_revision)
    authorization = case.preview(request=request)["authorization_template"]
    result = case.apply(authorization, request=request)
    assert result["status"] == "published", result
    assert handoff_git.read_ref(case.repository, request.ref) == result["receipt"]["commit"]
    commit = read_git_file(case.repository, result["receipt"]["commit"], "src/service.py")
    assert commit == case.payload


def test_a_ref_move_after_preview_blocks_stale_authorization(handoff_case: HandoffCase) -> None:
    case = handoff_case
    authorization = case.authorization
    handoff_git.compare_and_swap(case.repository, case.request.ref, None, case.request.base_revision)
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    with pytest.raises(ValueError):
        case.apply(authorization)
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


def test_preview_of_a_published_request_returns_history_without_new_authority(handoff_case: HandoffCase) -> None:
    case = handoff_case
    result = case.apply(case.authorization)
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    preview = case.preview()
    assert preview["status"] == "published" and preview["dry_run"] is True
    assert preview["authorization_template"] is None and preview["receipt"] == result["receipt"]
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


@pytest.mark.parametrize("damage", ["base", "attempt", "authorization", "metadata"])
def test_changed_publication_scope_requires_another_exact_review(handoff_case: HandoffCase, damage: str) -> None:
    case = handoff_case
    authorization = case.authorization
    request = case.request
    if damage == "base":
        request = replace(request, base_revision="f" * 40)
    elif damage == "attempt":
        request = replace(request, attempt=2)
    elif damage == "authorization":
        authorization = {**authorization, "commit": "f" * 40}
    else:
        request = replace(request, message="unreviewed commit metadata\n")
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    with pytest.raises((ValueError, OSError)):
        case.apply(authorization, request=request)
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


def test_exact_replay_is_historical_and_independent_of_source_or_later_ref(handoff_case: HandoffCase) -> None:
    case = handoff_case
    authorization = case.authorization
    result = case.apply(authorization)
    receipt = result["receipt"]
    handoff_git.compare_and_swap(case.repository, case.request.ref, receipt["commit"], case.request.base_revision)
    shutil.rmtree(Path(case.execution["receipt"]["output"]))
    case.item.journal.unlink()
    case.item.documents.clear()
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.item.authoritative_bytes()
    assert case.apply(authorization)["receipt"] == receipt
    assert handoff.show(case.journal, result["handoff_id"])["receipt"] == receipt
    assert handoff.recover(case.journal, result["handoff_id"])["receipt"] == receipt
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.item.authoritative_bytes()) == before
    assert handoff_git.read_ref(case.repository, case.request.ref) == case.request.base_revision


def test_an_authorization_cannot_rewrite_an_existing_publication(handoff_case: HandoffCase) -> None:
    case = handoff_case
    authorization = case.authorization
    receipt = case.apply(authorization)["receipt"]
    with pytest.raises(ValueError):
        case.apply(authorization, request=replace(case.request, message="replace immutable history"))
    with pytest.raises(ValueError):
        case.apply({**authorization, "tree": "e" * 40})
    assert handoff.show(case.journal, authorization["handoff_id"])["receipt"] == receipt


@pytest.mark.parametrize("damage", ["repository", "bytes", "mapping"])
def test_inconsistent_git_codec_output_cannot_be_reviewed_or_poison_history(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    case = handoff_case
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    prepare = handoff_git.prepare

    def inconsistent(repository: Path, base: str, files: Any, **options: Any) -> Doc:
        if damage == "bytes":
            files = {name: b"bytes not retained by the approved executor" for name in files}
        elif damage == "mapping":
            files = {"src/unreviewed.py": next(iter(files.values()))}
        package = prepare(repository, base, files, **options)
        if damage == "repository":
            package["repository"]["instance_id"] = "00112233445546778899aabbccddeeff"
        return package

    monkeypatch.setattr(handoff_git, "prepare", inconsistent)
    with pytest.raises(ValueError):
        case.preview()
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before


@pytest.mark.parametrize("damage", ["model_revision", "context", "contract_target", "contract_storage"])
def test_source_provenance_cannot_be_relabelled_by_recomputing_handoff_authorization(
    handoff_case: HandoffCase, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    case = handoff_case
    before = case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()
    select = handoff._selection

    def inconsistent(*args: Any, **options: Any) -> Any:
        contract, package = select(*args, **options)
        source = contract["source"]
        if damage == "model_revision":
            source["input_target"]["model_revision"] = "d" * 40
        elif damage == "context":
            source["input_target"]["context"] = "unrelated-context"
        elif damage == "contract_target":
            source["contract"]["target"]["model_revision"] = "d" * 40
            source["input_target"]["model_revision"] = "d" * 40
        else:
            source["contract"]["policy"]["storage"]["journal_id"] = "00112233445546778899aabbccddeeff"
        return contract, package

    monkeypatch.setattr(handoff, "_selection", inconsistent)
    # Preview derives a fresh handoff authorization from these inconsistent facts. Its digest
    # cannot substitute for the original executor authorization's source-contract commitment.
    with pytest.raises(ValueError):
        case.preview()
    assert (case.journal.read_bytes(), repository_bytes(case.repository), case.authoritative_bytes()) == before
