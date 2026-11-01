"""Controlled verification of the separate observer authority, proof parsing and one-call reservation."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from automatic_merge_fixtures import MergeCase, make_case
from exacly import automatic_merge_live as observer
from exacly import delivery_automation as delivery
from exacly import delivery_store, execution_store, executor_runtime, github_merge
from test_delivery_contracts import identify
from test_delivery_contracts import intent as common_intent

Doc = dict[str, Any]


@pytest.fixture
def observer_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MergeCase, Doc, Path]:
    case = make_case(tmp_path, monkeypatch)
    private = tmp_path / "observer-private"
    private.mkdir(mode=0o700)
    path = private / "delivery.sqlite3"
    initialized = delivery_store.initialize(
        path,
        objective=case.request["objective"],
        limits=case.authorization["limits"],
        native_limits=case.adapter.call_limits["invoke"],
    )
    case.authorization["journal"] = {"path": str(path), "id": initialized["id"]}
    case.pin = delivery.ApprovalPin(
        case.authorization["issuer"], case.authorization["approval_id"], delivery.digest(case.authorization)
    )
    case.intent = common_intent(case.request, case.authorization)
    case.intent["cost"].update(wall_time_ms=120000, reads=64)
    identify(case.intent)
    result = case.invoke()
    assert result["outcome"] == "applied"
    receipt = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-receipt",
        "intent_id": case.intent["id"],
        "operation_key": case.request["operation_key"],
        "request_digest": delivery.digest(case.request),
        "authority_digest": delivery.digest(case.authorization),
        "stage": "merge",
        "adapter": copy.deepcopy(case.authorization["adapter"]),
        **{name: copy.deepcopy(result[name]) for name in ("outcome", "effect", "reason")},
        "provenance_digest": delivery.digest(result["provenance"]),
        "claims": copy.deepcopy(delivery.CLAIMS),
    }
    identify(receipt)
    now = dt.datetime.now(dt.UTC)
    with delivery_store.opened(path, initialized["id"], write=True) as retained:
        retained.prepare(case.intent, case.payload, case.authorization, case.pin, now)
        call = retained.reserve_call(
            case.request["operation_key"],
            "invoke",
            case.adapter.call_limits["invoke"],
            case.authorization,
            case.pin,
            now,
        )
        # Explicitly controlled provenance: actual engine/watchdog integration has separate tests.
        retained.update_call(
            case.request["operation_key"],
            call["id"],
            lambda value: value.update(
                owner={"process": executor_runtime.identity(os.getpid()), "deadline_monotonic": time.monotonic() + 120},
                cleanup=True,
            ),
        )
        retained.finish_call(case.request["operation_key"], call["id"], result, receipt)
    approval = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-read-recovery",
        "issuer": "independently approved acceptance observer",
        "approval_id": "one controlled observer call",
        "objective": copy.deepcopy(case.request["objective"]),
        "journal": copy.deepcopy(case.authorization["journal"]),
        "operation_key": case.request["operation_key"],
        "intent_id": case.intent["id"],
        "request_digest": delivery.digest(case.request),
        "native_payload_digest": delivery.digest(case.payload),
        "old_configuration_digest": case.adapter.configuration_digest,
        "new_configuration_digest": case.adapter.configuration_digest,
        "operation_profile_digest": case.adapter.operation_profile_digest,
        "policy_digest": delivery.digest(case.policy),
        "contract": copy.deepcopy(case.policy["contract"]),
        "limits": copy.deepcopy(case.authorization["limits"]),
        "read_limits": case.adapter.call_limits["reconcile"],
        "not_before": "2020-01-01T00:00:00Z",
        "expires_at": "2100-01-01T00:00:00Z",
    }
    packet = {
        "schema_version": "0.1.0",
        "kind": "delivery-merge-live-observation",
        "source": observer.SOURCE,
        "observation_id": "7" * 32,
        "output_bundle": str(private / "observation-once"),
        "maximum_calls": 1,
        "limits": case.adapter.call_limits["reconcile"],
        "journal": copy.deepcopy(case.authorization["journal"]),
        "operation_key": case.request["operation_key"],
        "intent_id": case.intent["id"],
        "receipt_id": receipt["id"],
        "original_approval_pin": delivery_store.pin_document(case.pin),
        "original_configuration": copy.deepcopy(case.configuration),
        "configuration": copy.deepcopy(case.configuration),
        "policy": copy.deepcopy(case.policy),
        "read_approval": approval,
        "read_approval_pin": {
            "issuer": approval["issuer"],
            "approval_id": approval["approval_id"],
            "digest": delivery.digest(approval),
        },
    }
    return case, packet, path


def test_observer_uses_real_watchdog_read_only_and_leaves_effect_journal_unchanged(
    observer_case: tuple[MergeCase, Doc, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, packet, path = observer_case
    actual = github_merge.GitHubMergeAdapter
    monkeypatch.setattr(
        github_merge, "GitHubMergeAdapter", lambda config, policy: actual(config, policy, opener=case.opener)
    )
    before = path.read_bytes()
    receipt = observer.observe(packet, packet_digest=delivery.digest(packet))
    assert receipt["accepted"] is True and receipt["provider_writes"] == 0
    assert path.read_bytes() == before
    bundle = Path(packet["output_bundle"])
    assert {item.name for item in bundle.iterdir()} == {
        "reservation.json",
        "owner.json",
        "cleanup.json",
        "result.json",
        "receipt.json",
    }
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in bundle.iterdir())
    assert json.loads((bundle / "cleanup.json").read_text()) == {"cleaned": True}
    with pytest.raises(FileExistsError):
        observer.observe(packet, packet_digest=delivery.digest(packet))
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["source", "pin", "authority", "scope", "write-capacity", "original-pin"])
def test_observer_rejects_wrong_source_pin_or_authority(
    observer_case: tuple[MergeCase, Doc, Path], damage: str
) -> None:
    case, packet, _ = observer_case
    pin = delivery.digest(packet)
    if damage == "source":
        packet["source"] = "candidate-selected-fake-provider"
    elif damage == "pin":
        pin = delivery.digest("some other independently selected packet")
    elif damage == "authority":
        packet["read_approval"]["expires_at"] = "2021-01-01T00:00:00Z"
        packet["read_approval_pin"]["digest"] = delivery.digest(packet["read_approval"])
    elif damage == "scope":
        packet["intent_id"] = delivery.digest("another intent")
    elif damage == "write-capacity":
        packet["limits"]["write_calls"] = 1
    else:
        packet["original_approval_pin"]["digest"] = delivery.digest("not the original authorization")
    if damage not in ("pin", "source"):
        pin = delivery.digest(packet)
    if damage in ("pin", "source", "write-capacity"):
        with pytest.raises(ValueError):
            observer.validate_packet(packet, packet_digest=pin)
    else:
        adapter = github_merge.GitHubMergeAdapter(case.configuration, case.policy, opener=case.opener)
        with pytest.raises(ValueError):
            metadata, operation = observer._retained(packet, adapter)
            observer._current(packet, adapter, metadata, operation)
    assert not Path(packet["output_bundle"]).exists()


def test_empty_partial_observation_reservation_cannot_be_reused(observer_case: tuple[MergeCase, Doc, Path]) -> None:
    _, packet, _ = observer_case
    Path(packet["output_bundle"]).mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        observer.observe(packet, packet_digest=delivery.digest(packet))


def test_selected_packet_file_is_private_canonical_and_independently_pinned(
    observer_case: tuple[MergeCase, Doc, Path],
) -> None:
    _, packet, path = observer_case
    proof = path.parent / "proof.json"
    proof.write_text(execution_store.canonical(packet))
    proof.chmod(0o600)
    assert observer.read_packet(proof, packet_digest=delivery.digest(packet)) == packet
    proof.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        observer.read_packet(proof, packet_digest=delivery.digest(packet))
