"""One separately approved read-only acceptance observation of a retained exact merge.

This is an acceptance observer, not the delivery engine or its original effect budget. An externally
selected packet grants one bounded observation. A private create-only reservation consumes that
grant before the real Linux watchdog starts; absent or partial results never permit reuse. The
original delivery journal is read-only. Production always selects the installed GitHub adapter.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from exacly import automatic_merge, controller_runtime, delivery_key, github_merge, planning
from exacly import delivery_automation as contracts
from exacly import delivery_store as journal
from exacly import execution_store as store

Doc = dict[str, Any]
SOURCE = "github-atomic-merge-default-https-v1"
MAX_PACKET = 1024 * 1024
PIN = planning.fields({"issuer": contracts.TEXT, "approval_id": contracts.TEXT, "digest": contracts.DIGEST})
PACKET = planning.fields(
    {
        "schema_version": {"const": "0.1.0"},
        "kind": {"const": "delivery-merge-live-observation"},
        "source": {"const": SOURCE},
        "observation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "output_bundle": contracts.TEXT,
        "maximum_calls": {"type": "integer", "const": 1},
        "limits": automatic_merge.LIMIT,
        "journal": contracts.JOURNAL,
        "operation_key": contracts.DIGEST,
        "intent_id": contracts.DIGEST,
        "receipt_id": contracts.DIGEST,
        "original_approval_pin": PIN,
        "original_configuration": automatic_merge.CONFIG_SCHEMA,
        "configuration": automatic_merge.CONFIG_SCHEMA,
        "policy": automatic_merge.POLICY_SCHEMA,
        "read_approval": journal.READ_RECOVERY_SCHEMA,
        "read_approval_pin": PIN,
    }
)


def _pin(value: Doc) -> contracts.ApprovalPin:
    return contracts.ApprovalPin(value["issuer"], value["approval_id"], value["digest"])


def validate_packet(value: Any, *, packet_digest: str) -> Doc:
    """The pin is independently selected by the operator; never infer it from supplied JSON."""
    store.require(
        isinstance(packet_digest, str) and store.DIGEST.fullmatch(packet_digest),
        "independently selected live observation packet digest required",
    )
    selected = journal._schema(value, PACKET, limit=MAX_PACKET)
    store.require(contracts.digest(selected) == packet_digest, "live observation packet differs from selected pin")
    output = Path(selected["output_bundle"])
    store.require(
        output.is_absolute() and str(output) == selected["output_bundle"] and ".." not in output.parts,
        "live observation bundle requires a canonical absolute path",
    )
    store.require(selected["limits"]["write_calls"] == 0, "live acceptance never grants provider writes")
    return selected


def read_packet(path: Path, *, packet_digest: str) -> Doc:
    """Read private bounded canonical JSON without accepting a credential or provider loader."""
    absolute = store.location(path)
    descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = store.file_info(descriptor)
        store.require(info.st_size <= MAX_PACKET, "live observation packet exceeds its byte bound")
        content = os.read(descriptor, MAX_PACKET + 1)
        store.require(len(content) <= MAX_PACKET, "live observation packet exceeds its byte bound")
        value = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        selected = validate_packet(value, packet_digest=packet_digest)
        store.require(store.canonical(selected).encode() == content, "live observation packet is not canonical")
        return selected
    finally:
        os.close(descriptor)


def _retained(packet: Doc, adapter: github_merge.GitHubMergeAdapter) -> tuple[Doc, Doc]:
    """Authenticate original authority, exact result, and retained enforcement facts before reading GitHub."""
    with journal.opened(Path(packet["journal"]["path"]), packet["journal"]["id"]) as retained:
        history = retained.read()
    metadata = history["metadata"]
    operation = history["operations"].get(packet["operation_key"])
    store.require(operation is not None and operation["phase"] == "applied", "approved merge is not retained applied")
    intent, payload, approval = operation["intent"], operation["payload"], operation["approval"]
    store.require(
        intent["id"] == packet["intent_id"] and operation["receipt"]["id"] == packet["receipt_id"],
        "live observation selects another historical intent or receipt",
    )
    store.require(
        operation["pin"] == packet["original_approval_pin"], "original merge authority is not independently selected"
    )
    original = github_merge.GitHubMergeAdapter(packet["original_configuration"], packet["policy"])
    original.validate_payload(intent["request"], payload)
    store.require(
        intent["adapter"] == {"id": original.adapter_id, "configuration_digest": original.configuration_digest}
        and approval["policy_digest"] == original.approval_context["policy_digest"]
        and approval["contract"] == original.approval_context["contract"],
        "original merge enforcement profile differs from retained authority",
    )
    store.require(
        adapter.operation_profile_digest == original.operation_profile_digest == payload["operation_profile_digest"],
        "live observer changes original operation semantics",
    )
    invoked = [call for call in operation["calls"] if call["method"] == "invoke"]
    store.require(len(invoked) == 1 and invoked[0]["result"] is not None, "original native merge proof is unavailable")
    native = invoked[0]["result"]
    original.validate_result("invoke", intent["request"], payload, native)
    facts = native["provenance"]["facts"].get("preflight")
    automatic_merge.validate_ready_facts(intent["request"], payload, original.configuration, original.policy, facts)
    # Applied reconciliation alone has no retained preflight claim. The invocation above must
    # retain the actual checks/reviews/policy/source facts that preceded publication.
    result = next(call["result"] for call in reversed(operation["calls"]) if call["receipt"] == operation["receipt"])
    store.require(result["outcome"] == "applied", "retained merge receipt has no exact applied native result")
    automatic_merge._validate_observed(
        intent["request"],
        payload,
        original.configuration,
        original.policy,
        result["provenance"]["facts"],
        result["provenance"]["marker_object"],
    )
    store.require(
        result["effect"]["remote_receipt_digest"] == contracts.digest(result["provenance"]["facts"]),
        "retained effect is not tied to its native provider facts",
    )
    signed = automatic_merge.marker(
        intent["request"], payload, original.configuration, original.policy, lock_timeout=store.DEFAULT_LOCK_TIMEOUT
    )
    marker = result["provenance"]["facts"].get("marker")
    store.require(
        marker == {key: value for key, value in signed.items() if key != "message"},
        "retained merge lacks the independently selected delivery-key signature",
    )
    delivery_key.validate_key(Path(original.configuration["key"]["path"]), original.configuration["key"]["id"])
    return metadata, operation


def _current(packet: Doc, adapter: github_merge.GitHubMergeAdapter, metadata: Doc, operation: Doc) -> Doc:
    approved = journal.validate_read_recovery(
        packet["read_approval"], pin=_pin(packet["read_approval_pin"]), now=dt.datetime.now(dt.UTC)
    )
    bounds = adapter.call_limits["reconcile"]
    store.require(bounds == packet["limits"] and bounds["write_calls"] == 0, "observer read ceilings changed")
    journal.recovery_scope(approved, operation, metadata, bounds)
    store.require(
        approved["new_configuration_digest"] == adapter.configuration_digest
        and approved["policy_digest"] == adapter.approval_context["policy_digest"]
        and approved["contract"] == adapter.approval_context["contract"]
        and approved["operation_profile_digest"] == adapter.operation_profile_digest,
        "current read authority differs from actual selected observer profile",
    )
    # Recheck after local fingerprinting; an earlier clock read must not extend this grant.
    return journal.validate_read_recovery(approved, pin=_pin(packet["read_approval_pin"]), now=dt.datetime.now(dt.UTC))


def _write(directory: int, name: str, value: Doc) -> None:
    content = store.canonical(value).encode()
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        store.file_info(descriptor)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            store.require(written > 0, "live observation evidence write was incomplete")
            offset += written
        os.fsync(descriptor)
        os.fsync(directory)
    finally:
        os.close(descriptor)


def _reserve(packet: Doc, packet_digest: str) -> int:
    path = store.location(Path(packet["output_bundle"]))
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory = -1
    try:
        # Even a crash leaving an empty directory consumes this selected packet's single call.
        os.mkdir(path.name, 0o700, dir_fd=parent)
        directory = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.fchmod(directory, 0o700)
        info = os.fstat(directory)
        store.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(), "unsafe observation reservation")
        os.fsync(parent)
        _write(
            directory,
            "reservation.json",
            {
                "kind": "delivery-merge-observer-reservation",
                "packet_digest": packet_digest,
                "observation_id": packet["observation_id"],
                "maximum_calls": 1,
                "limits": packet["limits"],
                "original_effect_budget_charged": False,
            },
        )
        return directory
    except BaseException:
        if directory >= 0:
            os.close(directory)
        raise
    finally:
        os.close(parent)


def observe(value: Any, *, packet_digest: str) -> Doc:
    """Consume one independent read grant using the actual fixed transport and real watchdog.

    This deliberately has no injected provider/factory parameter. Tests may monkeypatch trusted
    Python bootstrap; packet data cannot select a fake host, endpoint or implementation.
    """
    packet = validate_packet(value, packet_digest=packet_digest)
    directory = _reserve(packet, packet_digest)
    duration = packet["limits"]["wall_time_ms"] / 1000
    expires = time.monotonic() + duration

    def native() -> Doc:
        adapter = github_merge.GitHubMergeAdapter(packet["configuration"], packet["policy"])
        metadata, operation = _retained(packet, adapter)
        approved = _current(packet, adapter, metadata, operation)
        remaining = min(
            expires - time.monotonic(),
            (contracts._timestamp(approved["expires_at"]) - dt.datetime.now(dt.UTC)).total_seconds(),
        )
        store.require(remaining > 0, "live observation authority expired before provider reads")
        result = adapter.reconcile(operation["intent"], operation["payload"], deadline=store.Deadline(remaining))
        validated = adapter.validate_result("reconcile", operation["intent"]["request"], operation["payload"], result)
        store.require(validated == result, "native observer rewrote its result")
        compared = set(operation["receipt"]["effect"]) - {"remote_receipt_digest"}
        accepted = result["outcome"] == "applied" and all(
            result["effect"].get(key) == operation["receipt"]["effect"][key] for key in compared
        )
        return {"status": "returned", "result": result, "accepted": accepted}

    try:
        bounded = controller_runtime.bounded_native_call(
            native,
            duration,
            on_owner=lambda owner: _write(directory, "owner.json", owner),
            on_cleanup=lambda cleaned: _write(directory, "cleanup.json", {"cleaned": cleaned}),
        )
        descriptor = os.open("cleanup.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            store.file_info(descriptor)
            cleaned = os.read(descriptor, 128)
        finally:
            os.close(descriptor)
        store.require(cleaned == b'{"cleaned":true}', "observer cleanup is unresolved; reservation remains consumed")
        store.require(bounded.get("status") == "returned", "observer outcome unresolved; reservation remains consumed")
        result = journal.copied(bounded.get("result"), limit=journal.MAX_PROVENANCE_BYTES)
        store.require(type(bounded.get("accepted")) is bool, "native observer omitted its checked verdict")
        _write(directory, "result.json", result)
        receipt = {
            "schema_version": "0.1.0",
            "kind": "delivery-merge-live-observation-receipt",
            "source": SOURCE,
            "packet_digest": packet_digest,
            "observation_id": packet["observation_id"],
            "intent_id": packet["intent_id"],
            "original_receipt_id": packet["receipt_id"],
            "read_approval_digest": contracts.digest(packet["read_approval"]),
            "limits": packet["limits"],
            "result_digest": contracts.digest(result),
            "accepted": bounded["accepted"],
            "original_effect_budget_charged": False,
            "provider_writes": 0,
        }
        receipt["id"] = contracts.digest(receipt)
        _write(directory, "receipt.json", receipt)
        return receipt
    finally:
        os.close(directory)
