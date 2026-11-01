"""Controlled exact-byte owning-workflow acceptance; these are not live registry acceptance."""

from __future__ import annotations

import copy
import datetime as dt
import json
import multiprocessing
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from exacly import delivery_automation as delivery
from exacly import delivery_store
from exacly import execution_store as store
from exacly import owning_publisher as owner
from exacly import release_publication as publication
from exacly.delivery_engine import DeliveryEngine
from release_publication_fixtures import make_case, retained
from test_release_publication_cli import LIMITS, reapprove

Doc = dict[str, Any]


def publish(case: Doc) -> Doc:
    return dict(case["adapter"].invoke(case["intent"], case["payload"], case["approval"], deadline=store.Deadline(120)))


def discover(case: Doc) -> Doc:
    return dict(case["adapter"].reconcile(case["intent"], case["payload"], deadline=store.Deadline(120)))


def refresh_owner(case: Doc) -> None:
    case["configuration"]["owner_configuration"]["digest"] = retained(
        Path(case["configuration"]["owner_configuration"]["path"]), case["owner"]
    )
    case["adapter"] = publication.ReleasePublicationAdapter(case["configuration"], case["policy"])


def test_publication_needs_exact_artifact_version_and_destination_authority(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(120))["ready"]
    for field, replacement in (
        ("version", "9.9.9"),
        ("destination", {"registry": "other", "package": "other"}),
        ("source", {"repository": {"id": 99, "full_name": "other/repo"}, "revision": "f" * 40}),
    ):
        request = copy.deepcopy(case["request"])
        request["subject"][field] = replacement
        with pytest.raises(ValueError, match="scope"):
            case["adapter"].validate_payload(request, case["payload"])
    wrong_stage = {**case["request"], "stage": "merge"}
    with pytest.raises(ValueError):
        case["adapter"].validate_payload(wrong_stage, case["payload"])
    result = publish(case)
    assert result["outcome"] == "applied"
    actual = owner.read_published_artifact(
        case["owner"]["registry"],
        "approved-package",
        "1.2.3",
        case["request"]["subject"]["artifacts"][0],
        case["request"]["subject"]["source"],
        case["owner"]["key"],
        case["request"]["subject"]["workflow"],
        deadline=store.Deadline(30),
    )
    assert actual == case["artifact_bytes"]


def test_publisher_cannot_rebuild_or_take_ownership_of_a_workflow(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    original_workflow = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"
    before = original_workflow.read_bytes()
    blob = Path(case["owner"]["artifact_store"]["path"]) / case["payload"]["inputs"][0]["blob"].split(":")[1]
    retained(blob, b"substituted artifact bytes")
    with pytest.raises(ValueError):
        case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(30))
    failed = publish(case)
    assert failed["outcome"] == "uncertain" and failed["provenance"]["version_state"] == "unknown"
    assert original_workflow.read_bytes() == before
    assert not owner.version_path(case["owner"]["registry"], "approved-package", "1.2.3").exists()


def test_existing_version_and_uncertain_publication_cannot_be_overwritten(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    result = publish(case)
    before = snapshot(Path(case["owner"]["registry"]["path"]))
    assert discover(case) == result
    assert publish(case) == result
    assert snapshot(Path(case["owner"]["registry"]["path"])) == before
    # A different retained intent cannot claim an existing version, even for the same bytes.
    changed = copy.deepcopy(case["intent"])
    changed["id"] = delivery.digest("different-retained-intent")
    conflict = owner.discover(case["owner"], changed, case["payload"], deadline=store.Deadline(30))
    assert conflict["outcome"] == "rejected" and conflict["effect"] is None
    assert snapshot(Path(case["owner"]["registry"]["path"])) == before


def test_publication_is_not_deployment_or_operational_evidence(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    result = publish(case)
    assert result["outcome"] == "applied"
    receipt = result["provenance"]["receipt"]
    assert receipt["claims"] == delivery.CLAIMS
    assert all(value is False for value in receipt["claims"].values())
    assert not any(field in result["effect"] for field in ("environment", "generation", "operational_health"))


def snapshot(root: Path) -> dict[str, tuple[bytes | None, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None, path.stat().st_mtime_ns)
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("field", ["source", "artifacts", "producer", "contract", "result", "key_id"])
def test_independent_verification_tampering_blocks_publication(tmp_path: Path, field: str) -> None:
    case = make_case(tmp_path)
    proof = copy.deepcopy(case["verification"])
    proof[field] = "tampered"
    retained(Path(case["owner"]["verification"]["path"]), proof)
    with pytest.raises(ValueError):
        case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(30))
    assert not owner.version_path(case["owner"]["registry"], "approved-package", "1.2.3").exists()


@pytest.mark.parametrize("expired", [False, True])
def test_wrong_verification_window_is_not_applicable(tmp_path: Path, expired: bool) -> None:
    case = make_case(tmp_path)
    proof = {key: value for key, value in case["verification"].items() if key != "signature"}
    now = dt.datetime.now(dt.UTC)
    proof["issued_at"] = (now + dt.timedelta(minutes=2) if not expired else now - dt.timedelta(hours=2)).isoformat()
    proof["expires_at"] = (now + dt.timedelta(hours=2) if not expired else now - dt.timedelta(minutes=2)).isoformat()
    signed = owner.sign_verification(proof, case["owner"]["verification_key"])
    case["owner"]["verification"]["digest"] = retained(Path(case["owner"]["verification"]["path"]), signed)
    refresh_owner(case)
    with pytest.raises(ValueError, match="verification"):
        owner.inspect(case["owner"], case["request"], case["payload"], deadline=store.Deadline(30))


def test_owner_rechecks_expired_delivery_approval_without_writing(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    approval = copy.deepcopy(case["approval"])
    approval["expires_at"] = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
    intent = copy.deepcopy(case["intent"])
    intent["authority_digest"] = delivery.digest(approval)
    intent["id"] = delivery.digest({k: v for k, v in intent.items() if k != "id"})
    with pytest.raises(ValueError, match="current"):
        owner.publish(
            case["owner"],
            intent,
            case["payload"],
            approval,
            adapter_configuration=case["configuration"],
            policy=case["policy"],
            deadline=store.Deadline(30),
        )
    assert len(list(Path(case["owner"]["registry"]["path"]).iterdir())) == 1


def test_owner_rejects_a_different_journal_and_original_budget(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    approval = copy.deepcopy(case["approval"])
    approval["limits"]["operations"] += 1
    intent = copy.deepcopy(case["intent"])
    intent["authority_digest"] = delivery.digest(approval)
    intent["id"] = delivery.digest({k: v for k, v in intent.items() if k != "id"})
    with pytest.raises(ValueError, match="owner scope"):
        owner.publish(
            case["owner"],
            intent,
            case["payload"],
            approval,
            adapter_configuration=case["configuration"],
            policy=case["policy"],
            deadline=store.Deadline(30),
        )


def test_missing_reply_is_discoverable_without_republishing(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    publish(case)  # Deliberately discard the caller's response, as after transport loss.
    before = snapshot(Path(case["owner"]["registry"]["path"]))
    result = discover(case)
    assert result["outcome"] == "applied"
    assert snapshot(Path(case["owner"]["registry"]["path"])) == before


def test_absence_during_reconciliation_never_authorizes_retry(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    before = snapshot(Path(case["owner"]["registry"]["path"]))
    result = discover(case)
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert result["provenance"]["version_state"] == "absent"
    assert snapshot(Path(case["owner"]["registry"]["path"])) == before


def _crash_after_file(case: Doc, name: str) -> None:
    original = owner._write_new

    def crash(path: Path, content: bytes) -> None:
        original(path, content)
        if path.name == name:
            os.kill(os.getpid(), signal.SIGKILL)

    owner._write_new = crash
    owner.publish(
        case["owner"],
        case["intent"],
        case["payload"],
        case["approval"],
        adapter_configuration=case["configuration"],
        policy=case["policy"],
        deadline=store.Deadline(120),
    )


@pytest.mark.parametrize("name", ["claim.json", "package.whl", "verification.json", "receipt.json"])
def test_real_process_crash_preserves_partial_reservation(tmp_path: Path, name: str) -> None:
    case = make_case(tmp_path)
    process = multiprocessing.get_context("fork").Process(target=_crash_after_file, args=(case, name))
    process.start()
    process.join(30)
    assert process.exitcode == -signal.SIGKILL
    before = snapshot(Path(case["owner"]["registry"]["path"]))
    result = discover(case)
    assert result["outcome"] == "uncertain"
    assert result["provenance"]["version_state"] == "reserved"
    assert result["provenance"]["reservation"]["intent_id"] == case["intent"]["id"]
    assert result["provenance"]["partial_artifacts"] == (
        [] if name == "claim.json" else case["request"]["subject"]["artifacts"]
    )
    assert publish(case)["outcome"] == "uncertain"
    assert snapshot(Path(case["owner"]["registry"]["path"])) == before


def _concurrent_publish(case: Doc, start: Any, result: Any) -> None:
    start.wait(30)
    result.put(publish(case))


def test_concurrent_duplicate_events_publish_one_immutable_version(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    context = multiprocessing.get_context("fork")
    start, result = context.Event(), context.Queue()
    processes = [context.Process(target=_concurrent_publish, args=(case, start, result)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    outputs = [result.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert all(output["outcome"] == "applied" for output in outputs)
    assert outputs[0] == outputs[1]
    assert len(list(Path(case["owner"]["registry"]["path"]).iterdir())) == 2


def test_actual_published_content_corruption_invalidates_reconciliation(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    path = owner.version_path(case["owner"]["registry"], "approved-package", "1.2.3")
    retained(path / "package.whl", b"bad")
    with pytest.raises(ValueError):
        discover(case)


def test_owner_receipt_tampering_cannot_create_success(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    path = owner.version_path(case["owner"]["registry"], "approved-package", "1.2.3")
    receipt = owner.decode((path / "receipt.json").read_bytes())
    receipt["subject"]["source"]["revision"] = "f" * 40
    retained(path / "receipt.json", receipt)
    with pytest.raises(ValueError, match="authenticate"):
        discover(case)


@pytest.mark.parametrize("kind", ["owner-config", "artifact", "key", "registry-marker"])
def test_private_inputs_reject_symlink_substitution(tmp_path: Path, kind: str) -> None:
    case = make_case(tmp_path)
    if kind == "owner-config":
        path = Path(case["configuration"]["owner_configuration"]["path"])
    elif kind == "artifact":
        path = Path(case["owner"]["artifact_store"]["path"]) / case["payload"]["inputs"][0]["blob"].split(":")[1]
    elif kind == "key":
        path = Path(case["owner"]["verification_key"]["path"])
    else:
        path = Path(case["owner"]["registry"]["path"]) / "identity.json"
    target = path.with_name(path.name + ".original")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(30))


def test_publisher_and_verifier_key_purposes_are_not_interchangeable(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    with pytest.raises(ValueError, match="purpose"):
        owner.sign_verification(
            {key: part for key, part in case["verification"].items() if key != "signature"},
            {"path": case["owner"]["key"]["path"], "id": case["owner"]["verification_key"]["id"]},
        )


@pytest.mark.parametrize("field", ["profile", "owner_configuration", "call_limits"])
def test_missing_required_adapter_configuration_is_rejected(tmp_path: Path, field: str) -> None:
    case = make_case(tmp_path)
    config = copy.deepcopy(case["configuration"])
    del config[field]
    with pytest.raises(ValueError):
        publication.ReleasePublicationAdapter(config, case["policy"])


def test_unsafe_version_and_metadata_collisions_are_rejected(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    for unsafe in ("../escape", "/escape", "a/b"):
        case["owner"]["scope"]["subject"]["version"] = unsafe
        refresh_owner(case)
        with pytest.raises(ValueError, match="unsafe"):
            _ = case["adapter"].configuration_digest


def test_version_resource_encoding_is_unambiguous(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert owner.version_path(case["owner"]["registry"], "a--b", "c") != owner.version_path(
        case["owner"]["registry"], "a", "b--c"
    )


def test_read_only_phase_write_bounds_cannot_be_enabled(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    config = copy.deepcopy(case["configuration"])
    config["call_limits"]["reconcile"]["write_calls"] = 1
    with pytest.raises(ValueError, match="write bounds"):
        publication.ReleasePublicationAdapter(config, case["policy"])


def test_success_without_owner_receipt_or_actual_inventory_is_rejected(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    result = publish(case)
    result["provenance"]["retrieved_artifacts"] = []
    with pytest.raises(ValueError, match="content"):
        case["adapter"].validate_result("invoke", case["request"], case["payload"], result)


def test_registry_uuid_prevents_aliasing_replacement_directory(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    registry = Path(case["owner"]["registry"]["path"])
    retained(registry / "identity.json", {"profile": "local-release-registry-v1", "id": "f" * 32})
    with pytest.raises(ValueError, match="storage identity"):
        case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(30))


@pytest.mark.parametrize("field", ["source", "artifacts", "producer", "contract"])
def test_authentic_verification_of_other_subject_is_not_applicable(tmp_path: Path, field: str) -> None:
    case = make_case(tmp_path)
    proof = {key: value for key, value in copy.deepcopy(case["verification"]).items() if key != "signature"}
    if field == "source":
        proof[field]["revision"] = "f" * 40
    elif field == "artifacts":
        proof[field][0]["digest"] = delivery.digest("other-bytes")
    elif field == "producer":
        proof[field]["revision"] = "f" * 40
    else:
        proof[field]["criteria"] = delivery.digest("weaker-criteria")
    signed = owner.sign_verification(proof, case["owner"]["verification_key"])
    case["owner"]["verification"]["digest"] = retained(Path(case["owner"]["verification"]["path"]), signed)
    # Even an independently selected new file pin cannot turn another subject into applicable proof.
    with pytest.raises(ValueError, match="verification does not cover"):
        owner.inspect(case["owner"], case["request"], case["payload"], deadline=store.Deadline(30))


def test_content_pin_alone_cannot_authenticate_forged_verification(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    forged = copy.deepcopy(case["verification"])
    forged["signature"] = "0" * 64
    case["owner"]["verification"]["digest"] = retained(Path(case["owner"]["verification"]["path"]), forged)
    with pytest.raises(ValueError, match="authenticate"):
        owner.inspect(case["owner"], case["request"], case["payload"], deadline=store.Deadline(30))


def test_noncanonical_verification_bytes_are_not_reinterpreted(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    raw = (store.canonical(case["verification"]) + "\n").encode()
    case["owner"]["verification"]["digest"] = retained(Path(case["owner"]["verification"]["path"]), raw)
    with pytest.raises(ValueError, match="canonical exact bytes"):
        owner.inspect(case["owner"], case["request"], case["payload"], deadline=store.Deadline(30))


def test_artifact_hardlink_is_rejected(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    path = Path(case["owner"]["artifact_store"]["path"]) / case["payload"]["inputs"][0]["blob"].split(":")[1]
    os.link(path, path.with_name("hardlink"))
    with pytest.raises(ValueError, match="single-link"):
        owner.inspect(case["owner"], case["request"], case["payload"], deadline=store.Deadline(30))


def test_public_configuration_permissions_are_rejected(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    Path(case["configuration"]["owner_configuration"]["path"]).chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        _ = case["adapter"].configuration_digest


def test_changed_owning_workflow_contract_requires_new_review(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    case["owner"]["workflow_code_digest"] = delivery.digest("unapproved-replacement-workflow")
    refresh_owner(case)
    with pytest.raises(ValueError, match="workflow code changed"):
        _ = case["adapter"].configuration_digest


def test_extra_provider_and_executable_selectors_are_rejected(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    for field, value in (("provider_url", "https://attacker.invalid"), ("executable", "/tmp/unreviewed")):
        config = {**case["configuration"], field: value}
        with pytest.raises(ValueError, match="Additional properties"):
            publication.ReleasePublicationAdapter(config, case["policy"])


def test_consumer_rejects_cross_revision_and_workflow_receipts(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    subject = case["request"]["subject"]
    for field in ("source", "workflow"):
        wrong = copy.deepcopy(subject[field])
        wrong["revision"] = "f" * 40
        with pytest.raises(ValueError, match="selected source"):
            owner.read_published_artifact(
                case["owner"]["registry"],
                "approved-package",
                "1.2.3",
                subject["artifacts"][0],
                wrong if field == "source" else subject["source"],
                case["owner"]["key"],
                wrong if field == "workflow" else subject["workflow"],
                deadline=store.Deadline(30),
            )


def test_authority_expiring_during_profile_work_blocks_first_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    original = publication.configuration_digest
    calls = 0

    def expired_after_profile(configuration: Doc, policy: Doc) -> str:
        nonlocal calls
        result = original(configuration, policy)
        calls += 1
        if calls == 2:
            case["owner"]["expires_at"] = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
        return result

    monkeypatch.setattr(publication, "configuration_digest", expired_after_profile)
    with pytest.raises(ValueError, match="not current"):
        owner.publish(
            case["owner"],
            case["intent"],
            case["payload"],
            case["approval"],
            adapter_configuration=case["configuration"],
            policy=case["policy"],
            deadline=store.Deadline(120),
        )
    assert calls == 2
    assert len(list(Path(case["owner"]["registry"]["path"]).iterdir())) == 1


def test_scope_must_outlive_whole_bounded_workflow_call(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    case["owner"]["expires_at"] = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10)).isoformat()
    with pytest.raises(ValueError, match="outlive"):
        owner.publish(
            case["owner"],
            case["intent"],
            case["payload"],
            case["approval"],
            adapter_configuration=case["configuration"],
            policy=case["policy"],
            deadline=store.Deadline(120),
        )
    assert len(list(Path(case["owner"]["registry"]["path"]).iterdir())) == 1


@pytest.mark.parametrize("method", ["inspect", "invoke", "reconcile"])
def test_zero_owner_call_capacity_is_rejected(tmp_path: Path, method: str) -> None:
    case = make_case(tmp_path)
    assert case["configuration"]["call_limits"][method]["read_calls"] == 1
    config = copy.deepcopy(case["configuration"])
    config["call_limits"][method]["read_calls"] = 0
    with pytest.raises(ValueError):
        publication.ReleasePublicationAdapter(config, case["policy"])


@pytest.mark.parametrize("value", [False, "1", -1, 2**63])
def test_malformed_owner_call_capacity_is_rejected(tmp_path: Path, value: Any) -> None:
    case = make_case(tmp_path)
    config = copy.deepcopy(case["configuration"])
    config["call_limits"]["invoke"]["write_calls"] = value
    with pytest.raises(ValueError):
        publication.ReleasePublicationAdapter(config, case["policy"])


def test_missing_publisher_key_blocks_before_any_version_reservation(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    Path(case["owner"]["key"]["path"]).unlink()
    with pytest.raises(OSError):
        case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(30))
    assert publish(case)["outcome"] == "uncertain"
    assert len(list(Path(case["owner"]["registry"]["path"]).iterdir())) == 1


def test_changed_publisher_key_blocks_before_any_version_reservation(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    retained(Path(case["owner"]["key"]["path"]), b"wrong key bytes")
    with pytest.raises(ValueError):
        case["adapter"].inspect(case["request"], case["payload"], deadline=store.Deadline(30))
    assert publish(case)["outcome"] == "uncertain"
    assert len(list(Path(case["owner"]["registry"]["path"]).iterdir())) == 1


def resign_receipt(case: Doc, changes: Doc) -> None:
    path = owner.version_path(case["owner"]["registry"], "approved-package", "1.2.3") / "receipt.json"
    receipt = owner.decode(path.read_bytes())
    body = {key: value for key, value in receipt.items() if key != "signature"}
    body.update(changes)
    retained(path, {**body, "signature": owner._signature(body, case["owner"]["key"], "publisher")})


def consume(case: Doc, **expectations: Any) -> bytes:
    subject = case["request"]["subject"]
    return owner.read_published_artifact(
        case["owner"]["registry"],
        "approved-package",
        "1.2.3",
        subject["artifacts"][0],
        subject["source"],
        case["owner"]["key"],
        subject["workflow"],
        deadline=store.Deadline(30),
        **expectations,
    )


def test_future_owner_signed_publication_is_rejected_by_reconcile_and_consumer(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    resign_receipt(case, {"published_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()})
    for read in (discover, consume):
        with pytest.raises(ValueError, match="timestamp is in the future"):
            read(case)


def test_owner_signed_publication_outside_original_window_is_rejected(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    before = dt.datetime.fromisoformat(case["owner"]["not_before"]) - dt.timedelta(seconds=1)
    resign_receipt(case, {"published_at": before.isoformat()})
    for read in (discover, consume):
        with pytest.raises(ValueError, match="original owning publisher window"):
            read(case)


def test_retained_original_owner_window_cannot_be_substituted(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    resign_receipt(
        case,
        {
            "owner_window": {
                "not_before": (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).isoformat(),
                "expires_at": case["owner"]["expires_at"],
            }
        },
    )
    with pytest.raises(ValueError, match="original owning publisher window"):
        discover(case)


def test_expired_current_scope_keeps_original_applicable_publication_historical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    original = publish(case)
    assert original["outcome"] == "applied"
    real_datetime = dt.datetime

    class LaterDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> LaterDateTime:
            return cls.fromtimestamp(real_datetime.now(tz).timestamp() + 7200, tz=tz)

    monkeypatch.setattr(dt, "datetime", LaterDateTime)
    with pytest.raises(ValueError, match="not current"):
        owner._current(case["owner"])
    with pytest.raises(ValueError, match="verification"):
        owner._verification(case["owner"], deadline=store.Deadline(30))
    assert discover(case) == original
    assert consume(case) == case["artifact_bytes"]


def test_consumer_binds_separately_selected_artifact_acceptance_contract_and_producer(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    assert (
        consume(
            case,
            expected_verification_contract=case["owner"]["contract"],
            expected_verification_producer=case["owner"]["verification_producer"],
        )
        == case["artifact_bytes"]
    )
    wrong_contract = {**case["owner"]["contract"], "criteria": delivery.digest("different-acceptance-meaning")}
    with pytest.raises(ValueError, match="verification contract differs"):
        consume(case, expected_verification_contract=wrong_contract)
    wrong_producer = {**case["owner"]["verification_producer"], "revision": "f" * 40}
    with pytest.raises(ValueError, match="verification producer differs"):
        consume(case, expected_verification_producer=wrong_producer)
    with pytest.raises(ValueError):
        consume(case, expected_verification_contract={**case["owner"]["contract"], "unknown": "unapproved"})


def test_consumer_does_not_reinterpret_changed_retained_verification_bytes(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    path = owner.version_path(case["owner"]["registry"], "approved-package", "1.2.3") / "verification.json"
    retained(path, path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="retained verification bytes differ"):
        consume(case)


def test_historical_consumer_needs_no_current_owner_grant_or_verifier_key(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert publish(case)["outcome"] == "applied"
    Path(case["configuration"]["owner_configuration"]["path"]).unlink()
    Path(case["owner"]["verification"]["path"]).unlink()
    Path(case["owner"]["verification_key"]["path"]).unlink()
    assert (
        consume(
            case,
            expected_verification_contract=case["owner"]["contract"],
            expected_verification_producer=case["owner"]["verification_producer"],
        )
        == case["artifact_bytes"]
    )


def different_authority_intent(intent: Doc, approval: Doc) -> tuple[Doc, Doc]:
    approved = copy.deepcopy(approval)
    approved["expires_at"] = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=7)).isoformat()
    selected = copy.deepcopy(intent)
    selected["authority_digest"] = delivery.digest(approved)
    selected["id"] = delivery.digest({key: value for key, value in selected.items() if key != "id"})
    delivery.validate_intent(selected, approval=approved)
    assert selected["id"] != intent["id"]
    assert selected["request"] == intent["request"]
    return selected, approved


def test_genuine_other_intent_child_reply_is_uncertain_without_discarding_actual_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    other_intent, other_approval = different_authority_intent(case["intent"], case["approval"])
    # Obtain B's real private publication and genuine signed receipt through the fixed owner process.
    foreign = case["adapter"].invoke(other_intent, case["payload"], other_approval, deadline=store.Deadline(120))
    assert foreign["outcome"] == "applied"
    assert foreign["provenance"]["receipt"]["intent_id"] == other_intent["id"]
    original = snapshot(Path(case["owner"]["registry"]["path"]))

    class ForeignReply:
        returncode = 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def communicate(self, *args: Any, **kwargs: Any) -> tuple[bytes, None]:
            return store.canonical(foreign).encode(), None

    monkeypatch.setattr(
        publication,
        "subprocess",
        SimpleNamespace(
            Popen=ForeignReply,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    actual = publish(case)
    assert actual["outcome"] == "uncertain" and actual["effect"] is None
    assert actual["provenance"] == foreign["provenance"]
    assert actual["provenance"]["receipt"]["intent_id"] != case["intent"]["id"]
    assert case["adapter"].validate_result("invoke", case["request"], case["payload"], actual) == actual
    assert snapshot(Path(case["owner"]["registry"]["path"])) == original
    # The same authenticated reply remains applicable to its actual intent, without a mutable cache.
    applicable = case["adapter"].invoke(other_intent, case["payload"], other_approval, deadline=store.Deadline(120))
    assert applicable == foreign
    assert discover(case)["outcome"] == "rejected"
    assert snapshot(Path(case["owner"]["registry"]["path"])) == original


def test_engine_retains_foreign_intent_uncertainty_and_fences_replay_and_conflicting_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    journal = Path(case["owner"]["delivery_authority"]["journal"]["path"])
    metadata = delivery_store.initialize(
        journal,
        objective=case["request"]["objective"],
        limits=copy.deepcopy(LIMITS),
        native_limits={"wall_time_ms": 120000, "read_calls": 1, "write_calls": 1},
    )
    reapprove(case, journal, metadata["id"])
    audit = Path(case["root"]) / "operator" / "foreign-owner-reply.json"

    class ForeignOwnerProcess:
        returncode = 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def communicate(self, data: bytes, *, timeout: float) -> tuple[bytes, None]:
            envelope = json.loads(data)
            other_intent, other_approval = different_authority_intent(envelope["intent"], envelope["authorization"])
            # The transport substitutes a genuinely produced B receipt after A's current preflight.
            foreign = owner.publish(
                case["owner"],
                other_intent,
                envelope["payload"],
                other_approval,
                adapter_configuration=case["configuration"],
                policy=case["policy"],
                deadline=store.Deadline(timeout),
            )
            assert foreign["outcome"] == "applied"
            retained(
                audit, {"selected_intent": envelope["intent"]["id"], "actual_receipt": foreign["provenance"]["receipt"]}
            )
            return store.canonical(foreign).encode(), None

    monkeypatch.setattr(
        publication,
        "subprocess",
        SimpleNamespace(
            Popen=ForeignOwnerProcess,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    engine = DeliveryEngine(journal, journal_id=metadata["id"], adapter_factory=lambda: case["adapter"])

    def pin() -> delivery.ApprovalPin:
        return delivery.ApprovalPin(
            case["approval"]["issuer"], case["approval"]["approval_id"], delivery.digest(case["approval"])
        )

    result = engine.apply(case["request"], case["payload"], approval=case["approval"], pin=pin())
    assert result["status"] == "uncertain" and result["receipt"]["effect"] is None
    saved = delivery_store.show(journal, metadata["id"], case["request"]["operation_key"])["operation"]
    actual = owner.decode(audit.read_bytes())
    assert actual["selected_intent"] == saved["intent"]["id"]
    assert actual["actual_receipt"]["intent_id"] != saved["intent"]["id"]
    assert saved["calls"][-1]["result"]["provenance"]["receipt"] == actual["actual_receipt"]
    assert saved["phase"] == "uncertain" and saved["receipt"]["effect"] is None
    journal_before, audit_before = journal.read_bytes(), audit.read_bytes()
    registry_before = snapshot(Path(case["owner"]["registry"]["path"]))
    replay = engine.apply(case["request"], case["payload"], approval=case["approval"], pin=pin())
    assert replay["status"] == "uncertain" and replay["historical"] is True
    assert journal.read_bytes() == journal_before and audit.read_bytes() == audit_before
    assert snapshot(Path(case["owner"]["registry"]["path"])) == registry_before
    # Even a separately reviewed new operation cannot acquire this unresolved version resource.
    case["request"]["operation_key"] = delivery.digest("another-operation-for-same-version")
    reapprove(case, journal, metadata["id"])
    with pytest.raises(ValueError, match="unresolved delivery operation blocks this resource"):
        engine.apply(case["request"], case["payload"], approval=case["approval"], pin=pin())
    assert journal.read_bytes() == journal_before and audit.read_bytes() == audit_before
    assert snapshot(Path(case["owner"]["registry"]["path"])) == registry_before
