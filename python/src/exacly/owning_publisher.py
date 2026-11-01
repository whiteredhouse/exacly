"""A fixed exact-byte publisher workflow for one private, local POSIX registry.

Trusted bootstrap selects an independently approved configuration outside dispatch input. An
exclusive version directory is a durable reservation, and an empty ``published`` directory is the
terminal commit marker after all bytes and the authenticated owner receipt are synced. Partial
reservations are never repaired, retried or overwritten by this workflow. Discovery performs no
writes. The same-user host and local filesystem are trusted; this is not a hostile-host boundary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

from exacly import delivery_automation as delivery
from exacly import execution_store as store
from exacly import planning

Doc = dict[str, Any]
FIELDS = planning.fields
PROFILE = "local-owning-publisher-v1"
OWNER_PROFILE = "local-owning-publisher-configuration-v1"
VERIFICATION_PROFILE = "local-artifact-verification-v1"
FILE_LIMIT = 256 * 1024
ARTIFACT_LIMIT = 64 * 1024 * 1024
PURPOSES = {"publisher", "artifact-verifier"}
ROOT_SCHEMA = FIELDS(
    {
        "profile": {"enum": ["local-release-registry-v1", "local-artifact-store-v1"]},
        "id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
)
KEY = FIELDS({"path": delivery.TEXT, "id": delivery.DIGEST})
ROOT = FIELDS({"path": delivery.TEXT, "id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}})
INPUT = FIELDS({"artifact": delivery.ARTIFACT, "blob": delivery.DIGEST})
OWNER_SCHEMA = FIELDS(
    {
        "profile": {"const": OWNER_PROFILE},
        "publisher_id": delivery.TEXT,
        "workflow_code_digest": delivery.DIGEST,
        "scope": FIELDS(
            {"objective": delivery.OBJECTIVE, "operation_key": delivery.DIGEST, "subject": delivery.SUBJECTS["publish"]}
        ),
        "contract": delivery.CONTRACT,
        "registry": ROOT,
        "artifact_store": ROOT,
        "key": KEY,
        "verification_key": KEY,
        "verification_producer": delivery.WORKFLOW,
        "delivery_authority": FIELDS(
            {
                "issuer": delivery.TEXT,
                "approval_id": delivery.TEXT,
                "journal": delivery.JOURNAL,
                "limits": delivery.BUDGET,
                "policy_digest": delivery.DIGEST,
            }
        ),
        "inputs": {"type": "array", "minItems": 1, "maxItems": 64, "items": INPUT},
        "verification": FIELDS({"path": delivery.TEXT, "digest": delivery.DIGEST}),
        "not_before": delivery.TEXT,
        "expires_at": delivery.TEXT,
        "maximum_artifact_bytes": {"type": "integer", "minimum": 1, "maximum": ARTIFACT_LIMIT},
    }
)
VERIFICATION_BODY = FIELDS(
    {
        "profile": {"const": VERIFICATION_PROFILE},
        "key_id": delivery.DIGEST,
        "source": delivery.SOURCE,
        "artifacts": delivery.ARTIFACTS,
        "producer": delivery.WORKFLOW,
        "contract": delivery.CONTRACT,
        "result": {"const": "passed"},
        "issued_at": delivery.TEXT,
        "expires_at": delivery.TEXT,
    }
)
SIGNATURE = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
WINDOW = FIELDS({"not_before": delivery.TEXT, "expires_at": delivery.TEXT})
VERIFICATION_SCHEMA = FIELDS({**VERIFICATION_BODY["properties"], "signature": SIGNATURE})
RECEIPT_BODY = FIELDS(
    {
        "profile": {"const": PROFILE},
        "key_id": delivery.DIGEST,
        "publisher_id": delivery.TEXT,
        "publisher_run": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "intent_id": delivery.DIGEST,
        "request_digest": delivery.DIGEST,
        "payload_digest": delivery.DIGEST,
        "registry_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "subject": delivery.SUBJECTS["publish"],
        "verification_digest": delivery.DIGEST,
        "published_at": delivery.TEXT,
        "owner_window": WINDOW,
        "claims": {"const": delivery.CLAIMS},
    }
)
RECEIPT_SCHEMA = FIELDS({**RECEIPT_BODY["properties"], "signature": SIGNATURE})
PROVENANCE_SCHEMA = FIELDS(
    {
        "profile": {"const": PROFILE},
        "version_state": {"enum": ["unknown", "absent", "reserved", "published", "conflict"]},
        "reservation": {
            "anyOf": [
                FIELDS(
                    {"intent_id": delivery.DIGEST, "request_digest": delivery.DIGEST, "payload_digest": delivery.DIGEST}
                ),
                {"type": "null"},
            ]
        },
        "partial_artifacts": {"type": "array", "maxItems": 64, "items": delivery.ARTIFACT},
        "verification": {"anyOf": [VERIFICATION_SCHEMA, {"type": "null"}]},
        "receipt": {"anyOf": [RECEIPT_SCHEMA, {"type": "null"}]},
        "retrieved_artifacts": {"type": "array", "maxItems": 64, "items": delivery.ARTIFACT},
    }
)


def byte_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def workflow_code_digest() -> str:
    return byte_digest(Path(__file__).read_bytes())


def decode(data: bytes) -> Doc:
    store.require(len(data) <= FILE_LIMIT, "publisher document exceeds its byte bound")
    try:
        value = json.loads(data, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("publisher document is not valid unique JSON") from None
    return delivery._document(value)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
    )


def read_bytes(path: Path, limit: int, *, deadline: store.Deadline | None = None) -> bytes:
    """Read one bounded private regular single-link file, rejecting pathname/content races."""
    selected = store.location(path)
    descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = store.file_info(descriptor)
        store.require(before.st_size <= limit, "publisher input exceeds its byte bound")
        data = bytearray()
        while True:
            if deadline:
                deadline.check()
            part = os.read(descriptor, min(65536, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
            store.require(len(data) <= limit, "publisher input grew past its byte bound")
        store.require(
            _identity(before) == _identity(os.fstat(descriptor)) == _identity(selected.lstat()),
            "publisher input changed while being read",
        )
        return bytes(data)
    finally:
        os.close(descriptor)


def _write_new(path: Path, content: bytes) -> None:
    selected = store.location(path)
    descriptor = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            count = os.write(descriptor, content[offset:])
            store.require(count > 0, "publisher could not write complete immutable content")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize_root(path: Path, *, artifact_store: bool = False) -> str:
    """Create only the marker in an existing private empty owner-managed directory."""
    selected = store.location(path / "identity.json").parent
    store.require(not list(selected.iterdir()), "publisher storage initialization requires an empty directory")
    identity = uuid.uuid4().hex
    marker = {"profile": "local-artifact-store-v1" if artifact_store else "local-release-registry-v1", "id": identity}
    _write_new(selected / "identity.json", store.canonical(marker).encode())
    _sync_directory(selected)
    return identity


def _root(value: Doc, *, artifact_store: bool = False) -> Path:
    path = Path(value["path"])
    store.require(
        path.is_absolute() and path == path.absolute() and ".." not in path.parts,
        "publisher storage requires a pinned canonical absolute path",
    )
    marker = delivery._schema(decode(read_bytes(path / "identity.json", FILE_LIMIT)), ROOT_SCHEMA)
    store.require(
        marker
        == {"profile": "local-artifact-store-v1" if artifact_store else "local-release-registry-v1", "id": value["id"]},
        "publisher storage identity changed",
    )
    return path


def initialize_key(path: Path, *, purpose: str) -> str:
    """Initialize a distinct owner or verifier key; key creation grants no effect scope."""
    store.require(purpose in PURPOSES, "unsupported publication key purpose")
    data = ("EXACLY " + purpose.upper() + " KEY v1\n").encode() + os.urandom(32)
    with store.locked(path, write=True) as (selected, directory_fd):
        _write_new(selected, data)
        os.fsync(directory_fd)
    return byte_digest(data)


def _key(value: Doc, purpose: str) -> bytes:
    path = Path(value["path"])
    prefix = ("EXACLY " + purpose.upper() + " KEY v1\n").encode()
    data = read_bytes(path, len(prefix) + 32)
    store.require(
        len(data) == len(prefix) + 32
        and data.startswith(prefix)
        and hmac.compare_digest(byte_digest(data), value["id"]),
        "publication key purpose or identity changed",
    )
    return data[len(prefix) :]


def _signature(body: Doc, key: Doc, purpose: str) -> str:
    content = store.canonical(body).encode()
    return hmac.new(_key(key, purpose), ("exacly." + purpose + ".v1\0").encode() + content, hashlib.sha256).hexdigest()


def _signed(value: Doc, schema: Doc, key: Doc, purpose: str) -> Doc:
    selected = delivery._schema(value, schema)
    body = {name: part for name, part in selected.items() if name != "signature"}
    store.require(
        body["key_id"] == key["id"] and hmac.compare_digest(selected["signature"], _signature(body, key, purpose)),
        "publisher proof does not authenticate under the independently pinned purpose key",
    )
    return selected


def sign_verification(body: Doc, key: Doc) -> Doc:
    """Trusted verifier API; the publisher never calls this or changes verification meaning."""
    selected = delivery._schema(body, VERIFICATION_BODY)
    store.require(selected["key_id"] == key["id"], "verification key identity differs")
    return {**selected, "signature": _signature(selected, key, "artifact-verifier")}


def _instant(value: str) -> dt.datetime:
    try:
        selected = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        store.require(selected.utcoffset() is not None, "publisher time requires an explicit timezone")
        return selected.astimezone(dt.UTC)
    except (ValueError, OverflowError):
        raise ValueError("invalid publisher time") from None


def _current(configuration: Doc, *, deadline: store.Deadline | None = None) -> None:
    now = dt.datetime.now(dt.UTC)
    store.require(
        _instant(configuration["not_before"]) <= now < _instant(configuration["expires_at"]),
        "independent owning publisher scope is not current",
    )
    if deadline is not None:
        deadline.check()
        store.require(
            now + dt.timedelta(seconds=deadline.remaining() + 1) < _instant(configuration["expires_at"]),
            "independent owning publisher scope must outlive the bounded workflow call",
        )


def _verification_current(proof: Doc, deadline: store.Deadline) -> None:
    now = dt.datetime.now(dt.UTC)
    deadline.check()
    store.require(
        _instant(proof["issued_at"]) <= now
        and now + dt.timedelta(seconds=deadline.remaining() + 1) < _instant(proof["expires_at"]),
        "artifact verification must remain applicable throughout the bounded workflow call",
    )


def load_configuration(path: Path, digest: str) -> Doc:
    raw = read_bytes(path, FILE_LIMIT)
    store.require(byte_digest(raw) == digest, "independent owning publisher configuration bytes changed")
    selected = delivery._schema(decode(raw), OWNER_SCHEMA)
    store.require(selected["workflow_code_digest"] == workflow_code_digest(), "approved owning workflow code changed")
    subject = selected["scope"]["subject"]
    store.require(
        subject["workflow"]["contract_digest"] == selected["workflow_code_digest"],
        "approved workflow does not identify the exact installed publisher contract",
    )
    store.require(
        subject["destination"]["registry"] == "local-release-registry:" + selected["registry"]["id"],
        "release destination differs from canonical registry identity",
    )
    store.require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", subject["destination"]["package"]) is not None,
        "publisher package name is unsafe",
    )
    for component in [subject["version"], *(item["name"] for item in subject["artifacts"])]:
        store.require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", component) is not None,
            "publisher version or artifact name is unsafe",
        )
    names = [item["name"] for item in subject["artifacts"]]
    store.require(
        len(names) == len(set(names))
        and not set(names) & {"claim.json", "receipt.json", "verification.json", "published"},
        "publisher artifact names collide with each other or workflow metadata",
    )
    store.require(
        [item["artifact"] for item in selected["inputs"]] == subject["artifacts"],
        "owner inventory differs from independently approved release artifacts",
    )
    store.require(
        sum(item["artifact"]["size"] for item in selected["inputs"]) <= selected["maximum_artifact_bytes"],
        "approved artifacts exceed owning workflow byte limits",
    )
    store.require(
        selected["key"]["id"] != selected["verification_key"]["id"],
        "publisher and verifier require distinct purpose keys",
    )
    return selected


def _verification(configuration: Doc, *, deadline: store.Deadline) -> Doc:
    selected = configuration["verification"]
    raw = read_bytes(Path(selected["path"]), FILE_LIMIT, deadline=deadline)
    store.require(byte_digest(raw) == selected["digest"], "approved verification receipt bytes changed")
    proof = _signed(decode(raw), VERIFICATION_SCHEMA, configuration["verification_key"], "artifact-verifier")
    store.require(
        raw == store.canonical(proof).encode(), "approved verification receipt requires canonical exact bytes"
    )
    subject = configuration["scope"]["subject"]
    now = dt.datetime.now(dt.UTC)
    store.require(
        proof["source"] == subject["source"]
        and proof["artifacts"] == subject["artifacts"]
        and proof["producer"] == configuration["verification_producer"]
        and proof["contract"] == configuration["contract"]
        and _instant(proof["issued_at"]) <= now < _instant(proof["expires_at"]),
        "artifact verification does not cover current exact bytes, source and approved producer",
    )
    _verification_current(proof, deadline)
    return proof


def _inputs(configuration: Doc, *, deadline: store.Deadline) -> list[tuple[Doc, bytes]]:
    root = _root(configuration["artifact_store"], artifact_store=True)
    result: list[tuple[Doc, bytes]] = []
    for item in configuration["inputs"]:
        expected = item["artifact"]
        store.require(item["blob"] == expected["digest"], "publisher cannot substitute artifact bytes")
        data = read_bytes(root / item["blob"].removeprefix("sha256:"), expected["size"], deadline=deadline)
        store.require(
            len(data) == expected["size"] and byte_digest(data) == expected["digest"],
            "prebuilt artifact bytes differ from independently approved identity",
        )
        result.append((expected, data))
    return result


def _version(configuration: Doc) -> Path:
    subject = configuration["scope"]["subject"]
    return version_path(configuration["registry"], subject["destination"]["package"], subject["version"])


def version_path(registry: Doc, package: str, version: str) -> Path:
    """Resolve an unambiguous canonical resource; human labels never become pathname components."""
    identity = delivery.digest({"package": package, "version": version}).removeprefix("sha256:")
    return _root(registry) / identity


def _proof(
    *,
    state: str,
    verification: Doc | None = None,
    receipt: Doc | None = None,
    artifacts: list[Doc] | None = None,
    reservation: Doc | None = None,
    partial: list[Doc] | None = None,
) -> Doc:
    return {
        "profile": PROFILE,
        "version_state": state,
        "verification": verification,
        "receipt": receipt,
        "retrieved_artifacts": artifacts or [],
        "reservation": reservation,
        "partial_artifacts": partial or [],
    }


def uncertain(reason: str, *, proof: Doc | None = None) -> Doc:
    return {
        "outcome": "uncertain",
        "effect": None,
        "reason": reason,
        "provenance": proof if proof is not None else _proof(state="unknown"),
    }


def inspect(configuration: Doc, request: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
    _current(configuration, deadline=deadline)
    _key(configuration["key"], "publisher")
    verification = _verification(configuration, deadline=deadline)
    _inputs(configuration, deadline=deadline)
    state = "reserved" if os.path.lexists(_version(configuration)) else "absent"
    return {
        "ready": state == "absent",
        "reason": "Exact approved prebuilt artifacts are ready."
        if state == "absent"
        else "Immutable version is already reserved; discover it without retry.",
        "provenance": _proof(state=state, verification=verification),
    }


def effect(request: Doc, receipt: Doc) -> Doc:
    return {
        **request["subject"],
        "publisher_run": receipt["publisher_run"],
        "remote_receipt_digest": delivery.digest(receipt),
    }


def validate_provenance(request: Doc, payload: Doc, value: Doc, configuration: Doc) -> Doc:
    selected = delivery._schema(value, PROVENANCE_SCHEMA)
    verification, receipt = selected["verification"], selected["receipt"]
    if verification is not None:
        _signed(verification, VERIFICATION_SCHEMA, configuration["verification_key"], "artifact-verifier")
        store.require(
            byte_digest(store.canonical(verification).encode()) == payload["verification_digest"],
            "retained verification differs from approved exact receipt bytes",
        )
        store.require(
            verification["source"] == request["subject"]["source"]
            and verification["artifacts"] == request["subject"]["artifacts"]
            and verification["producer"] == configuration["verification_producer"]
            and verification["contract"] == configuration["contract"],
            "retained verification applies to different artifact/source/producer",
        )
    if receipt is not None:
        _signed(receipt, RECEIPT_SCHEMA, configuration["key"], "publisher")
        store.require(
            receipt["subject"] == request["subject"]
            and receipt["request_digest"] == delivery.digest(request)
            and receipt["payload_digest"] == delivery.digest(payload)
            and receipt["registry_id"] == configuration["registry"]["id"]
            and receipt["publisher_id"] == configuration["publisher_id"]
            and receipt["verification_digest"] == payload["verification_digest"],
            "owner publication receipt applies to another exact operation",
        )
        store.require(
            selected["version_state"] == "published"
            and selected["retrieved_artifacts"] == request["subject"]["artifacts"],
            "owner receipt alone does not establish retained artifact content",
        )
        store.require(verification is not None, "publication lacks its retained verification window")
        assert verification is not None
        store.require(
            receipt["owner_window"] == {key: configuration[key] for key in ("not_before", "expires_at")},
            "publication receipt differs from the original owning publisher window",
        )
        validate_publication_time(receipt, verification)
    else:
        store.require(
            not selected["retrieved_artifacts"] and selected["version_state"] != "published",
            "publication proof lacks its authenticated owner receipt",
        )
    store.require(
        selected["version_state"] not in ("unknown", "absent")
        or (selected["reservation"] is None and not selected["partial_artifacts"]),
        "unobserved versions cannot claim retained partial effects",
    )
    store.require(
        selected["version_state"] != "published" or not selected["partial_artifacts"],
        "terminal publication cannot substitute partial inventory for actual complete content",
    )
    return selected


def validate_publication_time(receipt: Doc, verification: Doc) -> None:
    """Check historical applicability after callers authenticate the receipt and verification bytes.

    Expired current grants do not invalidate an original publication. A receipt may only assert a
    past instant within its retained original owner scope and verification applicability windows.
    """
    window = delivery._schema(receipt["owner_window"], WINDOW)
    published = _instant(receipt["published_at"])
    store.require(published <= dt.datetime.now(dt.UTC), "publication timestamp is in the future")
    store.require(
        _instant(window["not_before"]) <= published < _instant(window["expires_at"]),
        "publication timestamp is outside its original owning publisher window",
    )
    store.require(
        _instant(verification["issued_at"]) <= published < _instant(verification["expires_at"]),
        "publication lacks verification applicable at the actual publication time",
    )


def discover(configuration: Doc, intent: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
    request = intent["request"]
    path = _version(configuration)
    deadline.check()
    if not os.path.lexists(path):
        return uncertain(
            "No version is visible; absence does not authorize another publication.", proof=_proof(state="absent")
        )
    # Enforce safe private directory ancestry before inspecting any publisher-created paths.
    store.location(path / "receipt.json")
    marker = path / "published"
    if not os.path.lexists(marker):
        reservation = None
        if os.path.lexists(path / "claim.json"):
            try:
                reservation = delivery._schema(
                    decode(read_bytes(path / "claim.json", FILE_LIMIT, deadline=deadline)),
                    FIELDS(
                        {
                            "intent_id": delivery.DIGEST,
                            "request_digest": delivery.DIGEST,
                            "payload_digest": delivery.DIGEST,
                        }
                    ),
                )
            except ValueError:
                # A killed owner may have left only a prefix of its create-only claim.
                reservation = None
        partial = []
        for artifact in request["subject"]["artifacts"]:
            item = path / artifact["name"]
            if os.path.lexists(item):
                data = read_bytes(item, artifact["size"], deadline=deadline)
                partial.append({"name": artifact["name"], "size": len(data), "digest": byte_digest(data)})
        return uncertain(
            "Version is reserved or partial; discovery cannot finish or replace it.",
            proof=_proof(state="reserved", reservation=reservation, partial=partial),
        )
    marker_info = marker.lstat()
    store.require(
        stat.S_ISDIR(marker_info.st_mode)
        and marker_info.st_uid == os.geteuid()
        and stat.S_IMODE(marker_info.st_mode) == 0o700
        and not list(marker.iterdir()),
        "publisher terminal marker is not a private immutable empty directory",
    )
    receipt = _signed(
        decode(read_bytes(path / "receipt.json", FILE_LIMIT, deadline=deadline)),
        RECEIPT_SCHEMA,
        configuration["key"],
        "publisher",
    )
    if receipt["intent_id"] != intent["id"] or receipt["request_digest"] != delivery.digest(request):
        return {
            "outcome": "rejected",
            "effect": None,
            "reason": "Immutable version belongs to a different operation.",
            "provenance": _proof(state="conflict"),
        }
    found = []
    for artifact in request["subject"]["artifacts"]:
        data = read_bytes(path / artifact["name"], artifact["size"], deadline=deadline)
        found.append({"name": artifact["name"], "size": len(data), "digest": byte_digest(data)})
    verification = decode(read_bytes(path / "verification.json", FILE_LIMIT, deadline=deadline))
    proof = _proof(state="published", verification=verification, receipt=receipt, artifacts=found)
    validate_provenance(request, payload, proof, configuration)
    return {
        "outcome": "applied",
        "effect": effect(request, receipt),
        "reason": "Authenticated immutable version and exact retained bytes were discovered.",
        "provenance": proof,
    }


def read_published_artifact(
    registry: Doc,
    package: str,
    version: str,
    artifact: Doc,
    source: Doc,
    key: Doc,
    workflow: Doc,
    *,
    deadline: store.Deadline,
    expected_verification_contract: Doc | None = None,
    expected_verification_producer: Doc | None = None,
) -> bytes:
    """Read an exact published artifact for an independently configured later-stage consumer.

    Authenticate the owner receipt, selected source/workflow/version and every retained artifact.
    No current publisher grant is needed to read historical content. This does not authenticate a
    public registry or establish deployment or operational health. The owner receipt authenticates
    retained verification bytes through the trusted publisher delegation; without a verifier key,
    this consumer does not independently reauthenticate that verifier. Optional separately selected
    expectations bind the artifact acceptance meaning without loading a current publisher grant.
    """
    delivery._schema(registry, ROOT)
    delivery._schema(artifact, delivery.ARTIFACT)
    delivery._schema(source, delivery.SOURCE)
    delivery._schema(workflow, delivery.WORKFLOW)
    delivery._schema(key, KEY)
    path = version_path(registry, package, version)
    store.location(path / "receipt.json")
    marker = path / "published"
    info = marker.lstat()
    store.require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
        and not list(marker.iterdir()),
        "published artifact has no valid owning-workflow terminal marker",
    )
    receipt = _signed(
        decode(read_bytes(path / "receipt.json", FILE_LIMIT, deadline=deadline)), RECEIPT_SCHEMA, key, "publisher"
    )
    subject = receipt["subject"]
    store.require(
        receipt["registry_id"] == registry["id"]
        and subject["destination"] == {"registry": "local-release-registry:" + registry["id"], "package": package}
        and subject["version"] == version
        and subject["source"] == source
        and subject["workflow"] == workflow
        and artifact in subject["artifacts"],
        "published artifact receipt differs from independently selected source or destination",
    )
    verification_raw = read_bytes(path / "verification.json", FILE_LIMIT, deadline=deadline)
    verification = delivery._schema(decode(verification_raw), VERIFICATION_SCHEMA)
    store.require(
        byte_digest(verification_raw) == receipt["verification_digest"]
        and verification_raw == store.canonical(verification).encode(),
        "retained verification bytes differ from the authenticated publisher receipt",
    )
    store.require(
        verification["source"] == subject["source"] and verification["artifacts"] == subject["artifacts"],
        "retained verification applies to different publication artifacts or source",
    )
    if expected_verification_contract is not None:
        expected = delivery._schema(expected_verification_contract, delivery.CONTRACT)
        store.require(
            verification["contract"] == expected, "publication verification contract differs from selected meaning"
        )
    if expected_verification_producer is not None:
        expected = delivery._schema(expected_verification_producer, delivery.WORKFLOW)
        store.require(
            verification["producer"] == expected, "publication verification producer differs from selected producer"
        )
    validate_publication_time(receipt, verification)
    result: bytes | None = None
    total = 0
    for item in subject["artifacts"]:
        store.require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", item["name"]) is not None,
            "published artifact name is unsafe",
        )
        total += item["size"]
        store.require(total <= ARTIFACT_LIMIT, "published artifact inventory exceeds consumer bound")
        data = read_bytes(path / item["name"], item["size"], deadline=deadline)
        store.require(
            len(data) == item["size"] and byte_digest(data) == item["digest"],
            "actual published bytes differ from authenticated inventory",
        )
        if item == artifact:
            result = data
    store.require(result is not None, "selected artifact is not in the owning publisher inventory")
    assert result is not None
    return result


def _delivery_current(configuration: Doc, intent: Doc, authorization: Doc, *, deadline: store.Deadline) -> None:
    delivery.validate_intent(intent, approval=authorization)
    _current(configuration, deadline=deadline)
    selected = configuration["delivery_authority"]
    store.require(
        all(authorization[key] == value for key, value in selected.items())
        and authorization["contract"] == configuration["contract"]
        and authorization["stage"] == "publish"
        and _instant(authorization["not_before"]) <= dt.datetime.now(dt.UTC) < _instant(authorization["expires_at"]),
        "delivery publication authority differs from independent owner scope or is not current",
    )
    store.require(
        dt.datetime.now(dt.UTC) + dt.timedelta(seconds=deadline.remaining() + 1)
        < _instant(authorization["expires_at"]),
        "delivery publication authority must outlive the bounded workflow call",
    )


def publish(
    configuration: Doc,
    intent: Doc,
    payload: Doc,
    authorization: Doc,
    *,
    adapter_configuration: Doc,
    policy: Doc,
    deadline: store.Deadline,
) -> Doc:
    """Apply a separately predelegated one-version owner configuration outside dispatch input.

    Caller issuer/ID/journal/limit matching is structural. It does not check an external ApprovalPin,
    durable engine reservation or revocation. The trusted local adapter caller must enforce those
    boundaries before dispatch. This workflow is not a remote untrusted-owner handoff protocol.
    """
    from exacly import release_publication as publication

    request = delivery.validate_request(intent["request"])
    # The owner independently approves exact scope and prerequisites; the caller's grant is additional.
    store.require(
        configuration["scope"] == {key: request[key] for key in ("objective", "operation_key", "subject")},
        "dispatch input is outside independently approved owning publisher scope",
    )
    _delivery_current(configuration, intent, authorization, deadline=deadline)
    publication.validate_payload(request, payload, adapter_configuration, policy)
    store.require(
        publication._owner(adapter_configuration) == configuration,
        "dispatch adapter selected a different trusted owner configuration",
    )
    native = delivery._schema(payload, publication.PAYLOAD_SCHEMA)
    store.require(
        delivery.digest(native) == request["native_payload_digest"]
        and native["inputs"] == configuration["inputs"]
        and native["verification_digest"] == configuration["verification"]["digest"],
        "dispatch artifact or verification input differs from independently approved owner scope",
    )
    verification = _verification(configuration, deadline=deadline)
    inputs = _inputs(configuration, deadline=deadline)
    path = _version(configuration)
    with store.locked(path, write=True, deadline=deadline) as (_, registry_fd):
        _delivery_current(configuration, intent, authorization, deadline=deadline)
        store.require(
            intent["adapter"]
            == {"id": PROFILE, "configuration_digest": publication.configuration_digest(adapter_configuration, policy)},
            "publisher code or policy changed before version reservation",
        )
        _key(configuration["key"], "publisher")
        # Fingerprinting may be expensive. Recheck actual UTC after it and before the first write.
        _delivery_current(configuration, intent, authorization, deadline=deadline)
        _verification_current(verification, deadline)
        deadline.check()
        if os.path.lexists(path):
            return discover(configuration, intent, native, deadline=deadline)
        os.mkdir(path, 0o700)
        os.fsync(registry_fd)
        claim = {
            "intent_id": intent["id"],
            "request_digest": delivery.digest(request),
            "payload_digest": delivery.digest(native),
        }
        _delivery_current(configuration, intent, authorization, deadline=deadline)
        _write_new(path / "claim.json", store.canonical(claim).encode())
        _sync_directory(path)
        for artifact, data in inputs:
            deadline.check()
            _delivery_current(configuration, intent, authorization, deadline=deadline)
            _write_new(path / artifact["name"], data)
        body = {
            "profile": PROFILE,
            "key_id": configuration["key"]["id"],
            "publisher_id": configuration["publisher_id"],
            "publisher_run": uuid.uuid4().hex,
            "intent_id": intent["id"],
            "request_digest": delivery.digest(request),
            "payload_digest": delivery.digest(native),
            "registry_id": configuration["registry"]["id"],
            "subject": request["subject"],
            "verification_digest": native["verification_digest"],
            "published_at": dt.datetime.now(dt.UTC).isoformat(),
            "owner_window": {key: configuration[key] for key in ("not_before", "expires_at")},
            "claims": delivery.CLAIMS,
        }
        delivery._schema(body, RECEIPT_BODY)
        receipt = {**body, "signature": _signature(body, configuration["key"], "publisher")}
        _delivery_current(configuration, intent, authorization, deadline=deadline)
        _verification_current(verification, deadline)
        _write_new(path / "verification.json", store.canonical(verification).encode())
        _delivery_current(configuration, intent, authorization, deadline=deadline)
        _write_new(path / "receipt.json", store.canonical(receipt).encode())
        _sync_directory(path)
        deadline.check()
        _delivery_current(configuration, intent, authorization, deadline=deadline)
        _verification_current(verification, deadline)
        os.mkdir(path / "published", 0o700)
        _sync_directory(path)
        os.fsync(registry_fd)
        return discover(configuration, intent, native, deadline=deadline)


def main() -> int:
    """Run the trusted local owner entry point; its private config is the one-version predelegation.

    This command does not verify a caller ApprovalPin, engine reservation or revocation service.
    Candidate code must not be able to invoke this host command or read its private configuration.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--configuration-digest", required=True)
    arguments = parser.parse_args()
    try:
        configuration = load_configuration(arguments.configuration, arguments.configuration_digest)
        envelope = delivery._schema(
            decode(sys.stdin.buffer.read(FILE_LIMIT + 1)),
            FIELDS(
                {
                    "intent": delivery.INTENT_SCHEMA,
                    "payload": {"type": "object"},
                    "authorization": delivery.APPROVAL_SCHEMA,
                    "adapter_configuration": {"type": "object"},
                    "policy": {"type": "object"},
                }
            ),
        )
        result = publish(
            configuration,
            envelope["intent"],
            envelope["payload"],
            envelope["authorization"],
            adapter_configuration=envelope["adapter_configuration"],
            policy=envelope["policy"],
            deadline=store.Deadline(120),
        )
        sys.stdout.write(store.canonical(result))
        return 0
    except (OSError, ValueError):
        # Do not print arbitrary input, file content, key material or provider exception strings.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
