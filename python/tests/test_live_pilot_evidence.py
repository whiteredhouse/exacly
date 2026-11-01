"""Controlled verifier/export tests; none are observations of a live unattended pilot."""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from dispatch_fixtures import DATE
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import (
    completion,
    completion_store,
    controller_store,
    dispatch,
    executor,
    handoff,
    handoff_git,
    output_association,
    planning,
    publication_key,
)
from exacly import execution_store as store
from exacly import live_pilot_evidence as live
from exacly import remote_publication as remote
from exacly import standing_authorization as accounting
from exacly.applicability import Target
from handoff_fixtures import handoff_case as handoff_case
from handoff_fixtures import object_bytes
from test_admission import ATTEMPT, RUN_ID
from test_handoff_end_to_end import observed_git_revision
from test_unattended_output_association import Case
from test_unattended_output_association import case as case

Doc = dict[str, Any]


class ControlledTransport:
    adapter = "controlled-live-verifier-v1"

    def __init__(self) -> None:
        self.observed: Doc | None = None
        self.drop_response = False

    def inspect(self, operation: Doc, *, timeout: float) -> Doc:
        return self.observed or {
            "repository_id": operation["request"]["repository_id"],
            "ref": operation["request"]["ref"],
            "revision": operation["request"]["expected_revision"],
            "commit_present": False,
            "operation_id": None,
        }

    def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
        self.observed = {
            "repository_id": operation["request"]["repository_id"],
            "ref": operation["request"]["ref"],
            "revision": operation["authorization"]["commit"],
            "commit_present": True,
            "operation_id": operation["id"],
        }
        if self.drop_response:
            raise OSError("Controlled lost reply after the actual adapter effect.")


@pytest.fixture
def native_case(case: Case, tmp_path: Path) -> Doc:
    item = case.handoff.item
    associated = case.apply()
    assert associated["status"] == "associated"
    handed = handoff.show(case.handoff.journal, case.request.handoff_id)
    target = Target(handed["receipt"]["target"])
    source = item.dispatch.source
    observed_git_revision(source, case.handoff.repository, target.data["revision"], handed["receipt"]["tree"])
    completed = completion.complete(
        source,
        item.request.plan,
        item.request.action_id,
        item.request.readiness_policy,
        item.request.trust_policy,
        target,
        RUN_ID,
        ATTEMPT,
        DATE,
        ledger=item.ledger,
        binding_id=associated["receipt"]["binding"],
        state_path=item.state_path,
    )
    assert completed["status"] == "completed", completed
    with sqlite3.connect(item.dispatch.journal) as connection:
        row = connection.execute("SELECT id FROM dispatch_intents").fetchone()
        assert row is not None
    dispatched = dispatch.show(item.dispatch.journal, row[0])
    dispatch_receipt = next(
        event["data"]["receipt"]
        for event in dispatched["record"]["events"]
        if event["data"].get("kind") == "registered"
    )
    private = tmp_path / "remote"
    private.mkdir(mode=0o700)
    journal = private / "journal.sqlite3"
    keys = tmp_path / "publication-key"
    keys.mkdir(mode=0o700)
    key = keys / "publication.key"
    key_id = publication_key.initialize(key)
    request = remote.Request(
        journal,
        remote.initialize(journal),
        case.handoff.journal,
        case.handoff.request.policy["storage"]["journal_id"],
        handed["handoff_id"],
        str(source.policy["repository"]["id"]),
        ControlledTransport.adapter,
        "refs/heads/" + source.branch,
        handed["receipt"]["base_revision"] if getattr(source, "pilot_profile", False) else None,
        "refs/tags/req039-operations/controlled-native-main",
        key,
        key_id,
    )
    transport = ControlledTransport()
    transport.drop_response = getattr(source, "pilot_profile", False)
    published = remote.apply(
        request, authorization=remote.preview(request)["authorization_template"], transport=transport
    )
    if published["status"] == "ambiguous" and transport.drop_response:
        published = remote.recover(journal, request.journal_id, published["operation_id"], transport=transport)
    assert published["status"] == "published"
    with store.locked(item.ledger, write=False) as (path, _), store.database(path, create=False) as (connection, _):
        retained = completion_store.get(connection, completed["receipt"]["id"])
        binding = completion_store.envelope(connection, associated["receipt"]["binding"])
        original_binding = completion_store.envelope(connection, case.request.binding_id)
    correlations = {
        "dispatch": dispatch_receipt,
        "execute": case.handoff.execution["receipt"],
        "handoff": handed["receipt"],
        "publish": published,
        "associate": associated["receipt"],
        "complete": completed["receipt"],
        "binding_id": associated["receipt"]["binding"],
        "original_binding_id": case.request.binding_id,
    }
    packet = {
        "repository": source.policy["repository"],
        "consumer_branch": source.branch,
        "input_revision": handed["receipt"]["base_revision"],
        "producer_policy": source.policy,
        "original_goal": live.ORIGINAL_GOAL if getattr(source, "pilot_profile", False) else "sha256:" + "a" * 64,
    }
    native = {
        "dispatch": dispatched,
        "execute": executor.show(item.journal, case.handoff.execution["execution_id"]),
        "handoff": handed,
        "publish": published,
        "associate": output_association.show(case.storage, associated["association_id"]),
        "binding": binding,
        "original_binding": original_binding,
        "completion": completion.show(item.ledger, completed["receipt"]["id"]),
    }
    evidence = {key + "_b64": base64.b64encode(retained[key]).decode() for key in live._BLOBS}
    evidence.update({key: retained[key] for key in ("phase", "admission_receipt", "model_documents", "admitted_state")})
    account_dir = tmp_path / "accounting"
    account_dir.mkdir(mode=0o700)
    account_path = account_dir / "budget.sqlite3"
    account_id = accounting.initialize(account_path)["store_id"]
    objective = {"schema_version": "0.1.0", "id": "sha256:" + "b" * 64, "original_goal": packet["original_goal"]}
    limits = {"attempts": 12, "wall_time_ms": 120, "model_requests": 0, "model_spend_microunits": 0}
    accounting.enroll(account_path, store_id=account_id, objective=objective, limits=limits)
    correlations["authorizations"] = {}
    for phase in ("dispatch", "execute", "handoff", "publish", "associate", "complete"):
        proposal: Doc = {
            "schema_version": "0.1.0",
            "id": planning.digest({"phase": phase}),
            "delegation_id": "controlled-schema-only",
            "policy_digest": planning.digest(packet["producer_policy"]),
            "effect": {"kind": phase},
            "preconditions": {},
            "authorization": {"phase": phase},
            "cost": {"attempts": 1, "wall_time_ms": 10, "model_requests": 0, "model_spend_microunits": 0},
        }
        decision = accounting.reserve(
            account_path,
            store_id=account_id,
            objective_id=objective["id"],
            proposal=proposal,
            approved=True,
            reason="Controlled accounting schema; no live authority.",
        )
        accounting.settle(
            account_path,
            store_id=account_id,
            objective_id=objective["id"],
            proposal_id=proposal["id"],
            outcome={"status": "succeeded", "receipt": correlations[phase]},
        )
        correlations["authorizations"][phase] = {"proposal_id": proposal["id"], "digest": decision["digest"]}
    packet["delegation"] = {
        "accounting": {"path": str(account_path), "store_id": account_id},
        "objective": objective,
        "limits": limits,
    }
    anchor_dir = tmp_path / "ledger-anchor"
    anchor_dir.mkdir(mode=0o700)
    anchor = controller_store.initialize_ledger_anchor(anchor_dir / "anchor.json", ledger=item.ledger)
    components = {
        "controller": {"journal": str(tmp_path / "controller.sqlite3"), "journal_id": "unused-controlled-controller"},
        "ledger": str(item.ledger),
        "state": str(item.state_path),
        "association": case.storage,
        "ledger_anchor": anchor,
        "native_stores": {
            "dispatch": {"path": str(item.dispatch.journal), "id": item.dispatch.policy["storage"]["journal_id"]},
            "execute": {"path": str(item.journal), "id": item.policy["storage"]["journal_id"]},
            "handoff": {"path": str(case.handoff.journal), "id": case.handoff.request.policy["storage"]["journal_id"]},
            "publish": {"path": str(journal), "id": request.journal_id},
            "complete": {"path": str(item.ledger), "id": anchor["digest"]},
        },
    }
    return {
        "packet": packet,
        "native": native,
        "controller": {"event": {"status": "completed", "correlations": correlations}},
        "completion_evidence": evidence,
        "accounting": accounting.inspect(account_path, store_id=account_id, objective_id=objective["id"]),
        "_bundle": {"components": components, "delegation": packet["delegation"]},
        "_request": request,
        "_transport": transport,
        "_source": source,
        "_case": case,
    }


def test_controlled_native_receipts_rederive_exact_completion_without_becoming_live_proof(native_case: Doc) -> None:
    result = live.validate_native(native_case)
    assert result["phase"] == "committed"
    assert result["receipt"] == native_case["native"]["completion"]["receipt"]
    assert "verified_live_chain" not in planning.canonical(result["receipt"])
    live.validate_accounting(native_case)


def test_exporter_reads_real_native_views_and_completion_blobs(
    native_case: Doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = importlib.import_module

    def selected(name: str) -> Any:
        if name == "exacly.automation_controller":
            # Only the coordinator view is a test double. Every component and completion blob is
            # exported from the actual checked journals built by the controlled fixture above.
            return SimpleNamespace(show=lambda *args: native_case["controller"])
        return original(name)

    monkeypatch.setattr(importlib, "import_module", selected)
    exported = live.export_native(native_case["_bundle"], native_case["controller"])
    combined = {**native_case, **exported}
    assert live.validate_native(combined)["phase"] == "committed"
    live.validate_accounting(combined)
    assert exported["native"]["binding"]["completed_receipt"] == native_case["native"]["completion"]["receipt"]


@pytest.mark.parametrize(
    "damage", ["phase_only", "binding", "handoff", "archive", "state", "admission", "foreign_model", "foreign_dispatch"]
)
def test_native_provenance_or_completion_byte_changes_fail_closed(native_case: Doc, damage: str) -> None:
    value = copy.deepcopy(native_case)
    if damage == "phase_only":
        value["native"]["completion"]["receipt"] = {"status": "completed"}
    elif damage == "binding":
        value["native"]["binding"]["active"] = False
    elif damage == "handoff":
        value["native"]["handoff"]["receipt"]["commit"] = "0" * 40
    elif damage == "archive":
        value["completion_evidence"]["archive_b64"] = base64.b64encode(b"not authenticated evidence").decode()
    elif damage == "state":
        value["completion_evidence"]["after_state_b64"] = value["completion_evidence"]["before_state_b64"]
    elif damage == "admission":
        value["completion_evidence"]["admission_receipt"]["status"] = "rejected"
    elif damage == "foreign_model":
        value["completion_evidence"]["model_documents"] = {"foreign/state.json": {"serial": 99}}
    else:
        forged = {"dispatch_id": "sha256:" + "0" * 64, "binding_id": "sha256:" + "1" * 64}
        value["native"]["dispatch"] = {
            "status": "registered",
            "record": {"events": [{"data": {"kind": "registered", "receipt": forged}}]},
        }
        value["controller"]["event"]["correlations"]["dispatch"] = forged
    with pytest.raises((ValueError, KeyError, TypeError)):
        live.validate_native(value)


def test_pilot_opt_out_is_explicitly_unverified_protection() -> None:
    receipt: Doc = {
        "schema_version": "0.2.0",
        "claims": {"branch_protection_verified": False},
        "origin": {"branch_protection": "not_required", "branch_protection_checked_at": None},
    }
    live._pilot_protection(receipt)
    for changed in (
        {**receipt, "schema_version": "0.1.0"},
        {**receipt, "claims": {"branch_protection_verified": True}},
        {**receipt, "claims": {"branch_protection_verified": 0}},
        {**receipt, "origin": {**receipt["origin"], "branch_protection_checked_at": "2026-11-01T00:00:00Z"}},
        {**receipt, "origin": {**receipt["origin"], "branch_protection_identity": {"pretend": True}}},
    ):
        with pytest.raises(ValueError, match="branch protection"):
            live._pilot_protection(changed)


def _seed_code_tree(repository: Path, entries: list[Doc]) -> str:
    entries = copy.deepcopy(entries)
    checkout = Path(live.__file__).resolve().parents[3]
    for relative in live._CONTRACT_PATHS:
        entries.append(
            {
                "path": relative,
                "mode": "100644",
                "type": "blob",
                "sha": object_bytes(repository, "blob", (checkout / relative).read_bytes()),
            }
        )
    tree: Doc = {}
    for entry in entries:
        cursor = tree
        parts = entry["path"].split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = entry

    def retain(children: Doc) -> str:
        encoded: list[tuple[bytes, bytes]] = []
        for name, value in children.items():
            if "sha" in value and "path" in value:
                mode, oid, sort = value["mode"], value["sha"], name.encode()
            else:
                mode, oid, sort = "40000", retain(value), name.encode() + b"/"
            encoded.append((sort, mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)))
        return object_bytes(repository, "tree", b"".join(raw for _, raw in sorted(encoded)))

    root = retain(tree)
    return object_bytes(
        repository,
        "commit",
        (
            f"tree {root}\nauthor Controlled <c@example.invalid> 0 +0000\n"
            "committer Controlled <c@example.invalid> 0 +0000\n\ncontrolled code tree\n"
        ).encode(),
    )


def test_actual_code_fingerprint_rejects_stale_tree_and_other_import_location(tmp_path: Path) -> None:
    checkout = Path(live.__file__).resolve().parents[3]
    private = tmp_path / "fingerprint"
    private.mkdir(mode=0o700)
    repository = private / "source.git"
    handoff_git.initialize_repository(repository)
    entries = live._current_entries(checkout)
    revision = _seed_code_tree(repository, entries)
    fingerprint = live.implementation_fingerprint(checkout, repository, revision)
    assert fingerprint["digest"] == live.current_implementation(checkout)["digest"]
    changed = copy.deepcopy(entries)
    selected = next(item for item in changed if item["path"] == "python/src/exacly/live_pilot_evidence.py")
    selected["sha"] = "0" * 40
    stale = _seed_code_tree(repository, changed)
    with pytest.raises(ValueError, match="differ"):
        live.implementation_fingerprint(checkout, repository, stale)
    with pytest.raises(ValueError, match="another checkout"):
        live.implementation_fingerprint(tmp_path, repository, revision)


def test_fingerprint_selects_extra_source_and_rejects_symlinks(tmp_path: Path) -> None:
    folder = tmp_path / "python/src/exacly"
    folder.mkdir(parents=True)
    module = folder / "extra.py"
    raw = b"unexpected imported module\n"
    module.write_bytes(raw)
    selected = live._current_entries(tmp_path)
    assert (
        next(item for item in selected if item["path"].endswith("extra.py"))["sha"]
        == hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    )
    module.unlink()
    module.symlink_to(tmp_path / "unselected.py")
    with pytest.raises(ValueError, match="symbolic link"):
        live._current_entries(tmp_path)


def test_cache_directory_cannot_hide_executable_extra_source(tmp_path: Path) -> None:
    folder = tmp_path / "python/src/exacly/__pycache__"
    folder.mkdir(parents=True)
    (folder / "hidden.py").write_text("raise RuntimeError('not approved code')\n")
    with pytest.raises(ValueError, match="non-cache content"):
        live._current_entries(tmp_path)


@pytest.mark.parametrize(
    "reference",
    [
        {},
        {"run_id": 1, "attempt": 11, "artifact_id": 2},
        {"run_id": True, "attempt": 1, "artifact_id": 2},
        {"run_id": 1, "attempt": 1, "artifact_id": 2, "local_proof": {"passed": True}},
    ],
)
def test_local_proof_or_ambiguous_reference_cannot_be_live_authority(reference: Doc) -> None:
    with pytest.raises(ValueError):
        live._reference(reference)


def _projection_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "python/src/exacly"
    package.mkdir(parents=True)
    (package / "live_pilot_evidence.py").write_text("# controlled verifier\n")
    monkeypatch.setattr(live, "__file__", str(package / "live_pilot_evidence.py"))
    # These projection tests supply a virtual checkout, not an executed implementation.
    monkeypatch.setattr(live, "_loaded_sources", lambda checkout: None)
    for relative, numbers in zip(live._CONTRACT_PATHS, (range(34, 37), range(37, 40)), strict=True):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "records": [
                        {
                            "id": f"REQ-{number:03}",
                            "type": "Requirement",
                            "statement": "original",
                            "acceptance_criteria": ["actual native proof"],
                            "observe": {"command": "fixed"},
                            "lifecycle": "proposed",
                            "evidence": [],
                            "last_reviewed_at": "2026-01-01",
                        }
                        for number in numbers
                    ]
                }
            )
        )
    return tmp_path


def test_activation_metadata_changes_preserve_runtime_and_semantic_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _projection_checkout(tmp_path, monkeypatch)
    original = live.current_implementation(checkout)
    selector = checkout / "verification-references/req039.json"
    selector.parent.mkdir()
    selector.write_text('{"repo":"whiteredhouse/exacly","run_id":1,"attempt":1,"artifact_id":2}')
    gate = checkout / "ci/verification.yaml"
    gate.parent.mkdir()
    gate.write_text("requirements: [REQ-037]\n")
    for relative in live._CONTRACT_PATHS:
        path = checkout / relative
        document = yaml.safe_load(path.read_bytes())
        for record in document["records"]:
            record.update(lifecycle="active", evidence=[{"actual": "retained"}], last_reviewed_at="2026-11-01")
        path.write_text(yaml.safe_dump(document))
    assert live.current_implementation(checkout) == original
    selector.write_text('{"repo":"whiteredhouse/exacly","run_id":3,"attempt":2,"artifact_id":4}')
    gate.write_text("requirements: [REQ-037, REQ-039]\n")
    assert live.current_implementation(checkout) == original


@pytest.mark.parametrize("field", ["statement", "acceptance_criteria", "observe"])
def test_acceptance_semantics_changes_invalidate_only_semantic_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    checkout = _projection_checkout(tmp_path, monkeypatch)
    original = live.current_implementation(checkout)
    path = checkout / live._CONTRACT_PATHS[0]
    document = yaml.safe_load(path.read_bytes())
    document["records"][0][field] = "different required behavior"
    path.write_text(yaml.safe_dump(document))
    changed = live.current_implementation(checkout)
    assert changed["digest"] == original["digest"]
    assert changed["requirements_digest"] != original["requirements_digest"]


@pytest.mark.parametrize(
    "relative",
    [
        "ci/new_helper.py",
        "python/src/exacly/new.py",
        "python/src/exacly/live_pilot_evidence.py",
        "tests/test_workflow.py",
        "fixtures/unattended-pilot/model.yaml",
        ".github/workflows/req039-evidence.yml",
    ],
)
def test_executable_verifier_tests_and_fixed_consumer_model_are_in_runtime_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    checkout = _projection_checkout(tmp_path, monkeypatch)
    original = live.current_implementation(checkout)
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed executable or fixed consumer input\n")
    assert live.current_implementation(checkout)["digest"] != original["digest"]


@pytest.mark.parametrize("kind", ["executable", "symlink", "directory"])
def test_gate_exclusion_cannot_hide_executable_alias_or_directory(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "ci/verification.yaml"
    path.parent.mkdir()
    if kind == "directory":
        path.mkdir()
        (path / "hidden.py").write_text("unapproved code\n")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "elsewhere")
    else:
        path.write_text("#!/usr/bin/python\n")
        path.chmod(0o755)
    with pytest.raises(ValueError):
        live._current_entries(tmp_path)
    for entry in (
        {"path": "ci/verification.yaml", "type": "blob", "mode": "100755", "sha": "0" * 40},
        {"path": "ci/verification.yaml/hidden.py", "type": "blob", "mode": "100644", "sha": "0" * 40},
    ):
        with pytest.raises(ValueError):
            live._runtime_entries([entry])
    assert live._runtime_entries([{"path": "ci/verification.yaml.py", "type": "blob", "mode": "100644"}])


def test_retained_selector_is_optional_metadata_and_never_supplies_proof(tmp_path: Path) -> None:
    assert live.configured_reference(tmp_path, {}) is None
    assert live.configured_reference(
        tmp_path, {"EXACLY_REQ039_LIVE_RUN_ID": "1", "EXACLY_REQ039_LIVE_ARTIFACT_ID": "2"}
    ) == {"run_id": 1, "attempt": 1, "artifact_id": 2}
    path = tmp_path / "verification-references/req039.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"repo": live.REPOSITORY, "run_id": 3, "attempt": 2, "artifact_id": 4}))
    assert live.configured_reference(tmp_path, {}) is None
    enabled = {"EXACLY_REQ039_VERIFY_RETAINED_PROOF": "1"}
    assert live.configured_reference(tmp_path, enabled) == {"run_id": 3, "attempt": 2, "artifact_id": 4}
    path.write_text(json.dumps({"repo": "foreign/repo", "run_id": 3, "attempt": 2, "artifact_id": 4}))
    with pytest.raises(ValueError):
        live.configured_reference(tmp_path, enabled)
    with pytest.raises(ValueError):
        live.configured_reference(tmp_path, {"EXACLY_REQ039_LIVE_RUN_ID": "1"})


@pytest.mark.parametrize(
    "value,expected",
    [("2026-11-01T00:00:00Z", 1000000), ("2026-11-01T00:00:00.123Z", 1000), ("2026-11-01T00:00:00.123456Z", 1)],
)
def test_server_timestamp_precision_has_only_represented_rounding_interval(value: str, expected: int) -> None:
    assert live._timestamp_precision(value) == dt.timedelta(microseconds=expected)


def _actual_export(native_case: Doc, monkeypatch: pytest.MonkeyPatch) -> Doc:
    original = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(show=lambda *args: native_case["controller"])
            if name == "exacly.automation_controller"
            else original(name)
        ),
    )
    return live.export_native(native_case["_bundle"], native_case["controller"])


def test_export_remains_verifiable_after_all_original_private_stores_are_unavailable(
    native_case: Doc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _actual_export(native_case, monkeypatch)
    combined = {key: native_case[key] for key in ("packet", "controller")} | exported
    detached = tmp_path.with_name(tmp_path.name + "-detached")
    tmp_path.rename(detached)
    try:
        assert not tmp_path.exists()
        assert live.validate_native(combined)["phase"] == "committed"
        live.validate_accounting(combined)
    finally:
        detached.rename(tmp_path)


def test_native_export_excludes_reader_publisher_and_raw_signing_key(
    native_case: Doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = "reader-only-distinct-sentinel-47c931"
    publisher = "publisher-only-distinct-sentinel-892ba0"
    monkeypatch.setenv("GITHUB_TOKEN", reader)
    monkeypatch.setenv("EXACLY_REQ039_PUBLISH_TOKEN", publisher)
    key = native_case["_request"].operation_key.read_bytes()
    exported = _actual_export(native_case, monkeypatch)
    serialized = planning.canonical(exported).encode()
    for secret in (reader.encode(), publisher.encode(), key, key.hex().encode(), base64.b64encode(key)):
        assert secret not in serialized
    assert native_case["_request"].operation_key_id.encode() in serialized
    assert str(native_case["_request"].operation_key).encode() in serialized


@pytest.mark.parametrize("kind", ["dispatch", "execute", "handoff", "complete"])
def test_export_rejects_other_pinned_native_store_identity(
    native_case: Doc, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    native_case["_bundle"]["components"]["native_stores"][kind]["id"] = "foreign-instance"
    with pytest.raises(ValueError, match=r"identity|anchor"):
        _actual_export(native_case, monkeypatch)


def test_export_rejects_dispatch_replacement_during_actual_public_view_read(
    native_case: Doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = dispatch.show
    path = Path(native_case["_bundle"]["components"]["native_stores"]["dispatch"]["path"])
    retained = path.with_suffix(".retained")

    def replaced(*args: Any, **kwargs: Any) -> Doc:
        result = original(*args, **kwargs)
        raw = path.read_bytes()
        path.rename(retained)
        path.write_bytes(raw)
        path.chmod(0o600)
        return result

    monkeypatch.setattr(dispatch, "show", replaced)
    try:
        with pytest.raises(ValueError, match=r"changed|replaced|identity"):
            _actual_export(native_case, monkeypatch)
    finally:
        if retained.exists():
            path.unlink()
            retained.rename(path)


@pytest.mark.parametrize("conflict", [False, True])
def test_native_readonly_remote_precondition_uses_actual_handoff_and_never_persists_or_sends(
    native_case: Doc, conflict: bool
) -> None:
    request = replace(native_case["_request"], operation_marker_ref="refs/tags/req039-operations/controlled-probe")
    transport = ControlledTransport()
    if conflict:
        transport.observed = {
            "repository_id": request.repository_id,
            "ref": request.ref,
            "revision": native_case["native"]["handoff"]["receipt"]["commit"],
            "commit_present": True,
            "operation_id": None,
        }
    before = request.journal.read_bytes()
    grant = remote.preview(request)["authorization_template"]
    result = remote.inspect_precondition(request, authorization=grant, transport=transport)
    assert result["status"] == ("conflict" if conflict else "ref_matches")
    assert result["claims"]["effect_authorized"] is False
    assert result["claims"]["remote_published"] is False
    assert result["claims"]["marker_absence_verified"] is False
    assert result["digest"] == planning.digest({key: value for key, value in result.items() if key != "digest"})
    assert remote.lookup(request.journal, request.journal_id, result["operation_id"]) is None
    assert before == request.journal.read_bytes()
    assert transport.observed is None or transport.observed["operation_id"] is None


@pytest.mark.parametrize("damage", ["authorization", "adapter", "retained"])
def test_native_readonly_remote_precondition_rejects_wrong_grant_adapter_or_released_operation(
    native_case: Doc, damage: str
) -> None:
    request = native_case["_request"]
    if damage != "retained":
        request = replace(request, operation_marker_ref="refs/tags/req039-operations/controlled-probe")
    grant = remote.preview(request)["authorization_template"]
    transport = ControlledTransport()
    if damage == "authorization":
        grant["commit"] = "0" * 40
    if damage == "adapter":
        transport.adapter = "foreign-adapter"
    before = request.journal.read_bytes()
    with pytest.raises(ValueError, match=r"proposal|adapter|unreleased"):
        remote.inspect_precondition(request, authorization=grant, transport=transport)
    assert before == request.journal.read_bytes()
    assert transport.observed is None


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("queued", None), ("completed", "failure")])
def test_nonterminal_or_failed_coordinator_can_never_establish_live_acceptance(
    status: str, conclusion: str | None
) -> None:
    class Source:
        def get_json(self, path: str) -> Doc:
            return {
                "id": 1,
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "repository": {"id": live.REPOSITORY_ID},
                "head_repository": {"id": live.REPOSITORY_ID},
                "path": live.COORDINATOR,
                "status": status,
                "conclusion": conclusion,
            }

        def download_artifact(self, repository: str, identity: int) -> bytes:
            raise AssertionError("invalid coordinator must fail before downloading proof")

    expected = {
        "schema_version": live.VERSION,
        "paths": live.IMPLEMENTATION_PATHS,
        "excluded_paths": live.EXCLUDED_PATHS,
        "digest": "sha256:" + "1" * 64,
    }
    with pytest.raises(ValueError, match="coordinator run"):
        live.verify_live(Source(), {"run_id": 1, "attempt": 1, "artifact_id": 2}, expected, lambda *args: None)


@pytest.mark.parametrize("selected_case", live.REQUIRED_CASES)
def test_required_live_cases_reject_missing_and_generic_boolean_proofs(native_case: Doc, selected_case: str) -> None:
    # Native fixture is real, but deliberately lacks every live case. Native success must not
    # substitute for process recovery, explicit lost response or any declared negative.
    for standin in ({}, {"passed": True}, True):
        damaged = {
            **native_case,
            "recovery": standin,
            "controller_crash": standin,
            "remote_response_drop": standin,
            "replay_checks": standin,
            "negative_cases": standin,
        }
        with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
            live.validate_recovery_case(damaged, selected_case)
    with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
        live.validate_required_cases(native_case)


def _metered_case(native_case: Doc) -> tuple[Doc, Doc, Doc]:
    packet = native_case["packet"]
    contract = {"case": "controlled-negative-proof"}
    delegated = packet["delegation"]
    delegated.update(
        id="controlled-delegation",
        approval_id="controlled-human",
        contract={"pinned": True},
        reads={
            "negative-admission": {
                "adapter_id": "native-negative-admission-v1",
                "max_wall_time_ms": 10,
                "contract_digest": planning.digest(contract),
            }
        },
    )
    packet["digest"] = planning.digest(packet)
    request = {"contract": contract, "case": "wrong-revision"}
    slot = "0" * 32
    operation = planning.digest({"packet": packet["digest"], "case": "wrong-revision"})
    proposal = {
        "schema_version": "0.1.0",
        "id": planning.digest(
            {"store": delegated["accounting"]["store_id"], "objective": delegated["objective"]["id"], "slot": slot}
        ),
        "delegation_id": delegated["id"],
        "policy_digest": planning.digest(delegated),
        "effect": {
            "meter": {
                "slot": slot,
                "purpose": "negative-admission",
                "operation_id": operation,
                "adapter_id": "native-negative-admission-v1",
                "request": request,
                "wall_time_ms": 10,
            }
        },
        "preconditions": {"approval_id": delegated["approval_id"], "contract": delegated["contract"]},
        "authorization": {
            "operation": "bounded-control-call",
            "purpose": "negative-admission",
            "adapter_id": "native-negative-admission-v1",
            "slot": slot,
            "wall_time_ms": 10,
        },
        "cost": {"attempts": 0, "wall_time_ms": 10, "model_requests": 0, "model_spend_microunits": 0},
    }
    path = Path(delegated["accounting"]["path"])
    decision = accounting.reserve(
        path,
        store_id=delegated["accounting"]["store_id"],
        objective_id=delegated["objective"]["id"],
        proposal=proposal,
        approved=True,
        reason="Controlled genuine native reservation; no live authority.",
    )
    result = {"status": "controlled-proof-only"}
    decision = accounting.settle(
        path,
        store_id=delegated["accounting"]["store_id"],
        objective_id=delegated["objective"]["id"],
        proposal_id=proposal["id"],
        outcome={"status": "succeeded", "receipt": {"result": result, "slot": slot}},
    )
    report = {
        "schema_version": "0.1.0",
        "status": "completed",
        "kind": "negative-admission",
        "effect_id": operation,
        "reason": "Fresh bounded verification read retained.",
        "reservation": {
            "store_id": decision["store_id"],
            "objective_id": decision["objective_id"],
            "proposal_id": proposal["id"],
            "digest": decision["digest"],
            "status": decision["status"],
        },
        "receipt": {"result": result},
    }
    native_case["accounting"] = accounting.inspect(
        path, store_id=delegated["accounting"]["store_id"], objective_id=delegated["objective"]["id"]
    )
    return native_case, report, request


def test_metered_negative_proof_joins_real_native_budget_decision_and_exact_settled_result(native_case: Doc) -> None:
    evidence, report, request = _metered_case(native_case)
    assert live._metered_report(evidence, report, request, "negative-admission", "wrong-revision")["request"] == request
    for damage in ("result", "request", "reservation", "purpose"):
        changed = copy.deepcopy(report)
        selected = copy.deepcopy(request)
        if damage == "result":
            changed["receipt"]["result"] = {"status": "invented rejection"}
        elif damage == "request":
            selected["case"] = "wrong-attempt"
        elif damage == "reservation":
            changed["reservation"]["proposal_id"] = "sha256:" + "0" * 64
        else:
            changed["kind"] = "another-reader"
        with pytest.raises(ValueError):
            live._metered_report(evidence, changed, selected, "negative-admission", "wrong-revision")


def test_bare_rejected_boolean_is_not_native_admission_even_with_digest() -> None:
    receipt = {"status": "rejected"}
    receipt["digest"] = planning.digest(receipt)
    with pytest.raises(ValueError):
        live._rejected_admission(receipt, {})


def test_explicit_retained_profile_without_reference_is_inconclusive(tmp_path: Path) -> None:
    assert live.configured_reference(tmp_path, {"EXACLY_REQ039_VERIFY_RETAINED_PROOF": "1"}) is None
    with pytest.raises(ValueError):
        live.configured_reference(tmp_path, {"EXACLY_REQ039_VERIFY_RETAINED_PROOF": "invalid"})


def test_configured_live_proof_without_read_token_fails_instead_of_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_unattended_live_end_to_end import live_acceptance

    monkeypatch.setattr(live, "configured_reference", lambda checkout: {"run_id": 1, "attempt": 1, "artifact_id": 2})
    monkeypatch.delenv("EXACLY_LIVE_EVIDENCE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    wrapped: Any = live_acceptance
    with pytest.raises(AssertionError, match="authenticated read credential"):
        wrapped.__wrapped__()


def test_generic_native_goal_cannot_be_relabelled_as_the_fixed_consumer(native_case: Doc) -> None:
    value = copy.deepcopy(native_case)
    value["packet"]["workflow_contract"] = {"goal": {"record": "REQ-001", "dimension": "verification"}}
    value["packet"]["original_goal"] = live.ORIGINAL_GOAL
    with pytest.raises(ValueError, match="actual native goal"):
        live._fixed_native_goal(value)


def test_posthoc_native_budget_rows_cannot_establish_standing_effect_authority(native_case: Doc) -> None:
    # Native component success and accounting alone lack the saved independently approved
    # delegation, Authorizer Facts and exact native grants required by the live profile.
    with pytest.raises(ValueError, match="full closed independently approved standing delegation"):
        live._standing_native_authority(native_case)


@pytest.mark.parametrize("name", ["ci.req039_pilot", "ci.req039_bootstrap", "ci.req039_history", "__main__"])
def test_actual_imported_ci_helper_cannot_come_from_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    checkout = Path(live.__file__).resolve().parents[3]
    monkeypatch.setitem(sys.modules, name, SimpleNamespace(__file__=str(tmp_path / "req039_pilot.py")))
    with pytest.raises(ValueError, match="CI module escaped"):
        live._loaded_sources(checkout)


class RetainedAPI:
    """Controlled authenticated API seam; the production native packet/export remain unchanged."""

    def __init__(self, factory: Doc) -> None:
        from exacly import admission

        self.source = factory["source"]
        self.evidence = copy.deepcopy(factory["evidence"])
        self.context = factory["packet"]["context"]
        self.now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
        self.base = "/repos/" + live.REPOSITORY
        self.reference = {
            "run_id": int(self.context["run_id"]),
            "attempt": int(self.context["run_attempt"]),
            "artifact_id": 900004,
        }
        self.artifact: Doc = {
            "id": 900004,
            "expired": False,
            "name": f"{live.ARTIFACT_PREFIX}{self.reference['run_id']}-{self.reference['attempt']}",
            "workflow_run": {
                "id": self.reference["run_id"],
                "repository_id": live.REPOSITORY_ID,
                "head_repository_id": live.REPOSITORY_ID,
                "head_sha": self.context["coordinator_revision"],
            },
            "created_at": self.now.isoformat(),
        }
        self.run = {
            "id": self.reference["run_id"],
            "run_attempt": self.reference["attempt"],
            "event": "workflow_dispatch",
            "head_branch": "main",
            "repository": factory["packet"]["repository"],
            "head_repository": factory["packet"]["repository"],
            "path": live.COORDINATOR,
            "workflow_id": self.context["workflow_id"],
            "head_sha": self.context["coordinator_revision"],
            "status": "completed",
            "conclusion": "success",
            "created_at": self.context["created_at"],
        }
        self.jobs = {
            "total_count": 1,
            "jobs": [
                {
                    "id": int(self.context["host"]["job_id"]),
                    "run_id": self.reference["run_id"],
                    "head_sha": self.context["coordinator_revision"],
                    "name": self.context["job_key"],
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [{"name": live.UPLOAD_STEP, "status": "completed", "conclusion": "success"}],
                }
            ],
        }
        self.replace_evidence(self.evidence)
        assert self.artifact["digest"] == admission.sha256(self.archive)

    def replace_evidence(self, value: Doc) -> None:
        from exacly import admission
        from test_admission import bundle

        self.evidence = value
        self.archive = bundle([(live.MEMBER, (planning.canonical(value) + "\n").encode(), 0o100644)])
        self.artifact["digest"] = admission.sha256(self.archive)

    def get_json(self, path: str) -> Doc:
        run_path = f"{self.base}/actions/runs/{self.reference['run_id']}/attempts/{self.reference['attempt']}"
        if path == run_path:
            return copy.deepcopy(self.run)
        if path == run_path + "/jobs?per_page=100":
            return copy.deepcopy(self.jobs)
        if path == f"{self.base}/actions/workflows/{self.context['workflow_id']}":
            return {"id": self.context["workflow_id"], "path": live.COORDINATOR}
        if path == f"{self.base}/actions/artifacts/{self.reference['artifact_id']}":
            return copy.deepcopy(self.artifact)
        return dict(self.source.get_json(path))

    def download_artifact(self, repository: str, identity: int) -> bytes:
        if identity == self.reference["artifact_id"]:
            assert repository == live.REPOSITORY
            return self.archive
        return bytes(self.source.download_artifact(repository, identity))


def test_actual_authorizer_fixed_recipe_full_api_chain_survives_detached_private_stores(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from req039_native_factory import build

    factory = build(request, monkeypatch, tmp_path)
    api = RetainedAPI(factory)
    checkout = Path(live.__file__).resolve().parents[3]
    expected = live.current_implementation(checkout)
    assert factory["bundle"]["implementation"] == {**expected, "revision": api.context["coordinator_revision"]}
    original = copy.deepcopy(api.evidence)
    root = factory["root"]
    detached = root.with_name(root.name + "-hidden")
    root.rename(detached)
    try:
        assert not root.exists()

        def approval(packet: Doc, receipt: Doc) -> None:
            # This controlled channel is independently selected by the test. Live tests use the
            # actual fixed original-human REST/GraphQL reader; no local proof selects that channel.
            assert packet == factory["packet"]
            assert receipt == {"channel": "controlled-authenticated-reader"}

        result = live.verify_live(api, api.reference, expected, approval, now=api.now)
        assert result["status"] == "verified_live_chain"
        assert result["claims"] == {"authority_granted": False, "state_imported": False, "completion_invoked": False}
        assert result["fresh_admission"]["claims"]["branch_protection_verified"] is False
        names = {
            "divergent_ref": "divergent-ref",
            "producer_unavailable": "unavailable-producer",
            "wrong_revision": "wrong-revision",
            "wrong_attempt": "wrong-attempt",
            "criteria_changed": "changed-criteria",
            "verifier_changed": "changed-verifier",
            "delegation_revoked": "revoked-delegation",
            "delegation_expired": "expired-delegation",
            "objective_budget_exhausted": "exhausted-objective-budget",
        }
        for selected_case in live.REQUIRED_CASES:
            damaged = copy.deepcopy(original)
            if selected_case in names:
                del damaged["negative_cases"][names[selected_case]]
            else:
                del damaged[
                    {
                        "dispatch_restart": "recovery",
                        "publication_response_loss": "remote_response_drop",
                        "duplicate_events": "replay_checks",
                    }[selected_case]
                ]
            api.replace_evidence(damaged)
            with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
                live.verify_live(api, api.reference, expected, approval, now=api.now)
    finally:
        detached.rename(root)
