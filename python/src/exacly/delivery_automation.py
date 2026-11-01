"""Closed product-delivery documents; validation grants no authority to perform an effect.

These contracts are separate from verification delegation. A trusted operator selects the approval
pin and installed adapter outside candidate input. Matching that pin checks selected content, not a
signature or the independence of a human review. Durable accounting, live preconditions, concrete
stage validation and external invocation belong to subsequent adapters; no helper here invokes one.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

import jsonschema

from exacly import execution_store as store
from exacly import planning

Doc = dict[str, Any]
VERSION = "0.1.0"
PROFILE = "product-delivery-v1"
STAGES = ("plan", "merge", "publish", "deploy", "rollback")
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_NODES = 10000
MAX_DEPTH = 32
TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
SHA = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
DIGEST = planning.DIGEST
UNITS = {"type": "integer", "minimum": 0, "maximum": 2**63 - 1}
POSITIVE = {**UNITS, "minimum": 1}
REPOSITORY = planning.fields({"id": POSITIVE, "full_name": TEXT})
OBJECTIVE = planning.fields({"id": DIGEST, "original_goal": DIGEST})
ADAPTER = planning.fields({"id": TEXT, "configuration_digest": DIGEST})
JOURNAL = planning.fields({"path": TEXT, "id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}})
CONTRACT = planning.fields({key: DIGEST for key in ("model", "criteria", "bindings", "observer")})
BUDGET = planning.fields({key: UNITS for key in ("operations", "wall_time_ms", "reads", "rollbacks")})
ARTIFACT = planning.fields({"name": TEXT, "digest": DIGEST, "size": UNITS})
ARTIFACTS = {"type": "array", "minItems": 1, "maxItems": 64, "items": ARTIFACT}
SOURCE = planning.fields({"repository": REPOSITORY, "revision": SHA})
WORKFLOW = planning.fields({"repository": REPOSITORY, "id": TEXT, "revision": SHA, "contract_digest": DIGEST})
DESTINATION = planning.fields({"registry": TEXT, "package": TEXT})
MERGE_METHOD = {"enum": ["merge", "squash", "rebase"]}
SUBJECTS = {
    "plan": planning.fields({"source": SOURCE, "intent_digest": DIGEST}),
    "merge": planning.fields(
        {
            "repository": REPOSITORY,
            "head_repository": REPOSITORY,
            "pull_request": POSITIVE,
            "head_revision": SHA,
            "base_revision": SHA,
            "base_ref": TEXT,
            "method": MERGE_METHOD,
        }
    ),
    "publish": planning.fields(
        {"workflow": WORKFLOW, "source": SOURCE, "version": TEXT, "destination": DESTINATION, "artifacts": ARTIFACTS}
    ),
    "deploy": planning.fields(
        {
            "workflow": WORKFLOW,
            "application": TEXT,
            "environment": TEXT,
            "artifact": ARTIFACT,
            "expected_generation": TEXT,
            "strategy": TEXT,
        }
    ),
    "rollback": planning.fields(
        {
            "workflow": WORKFLOW,
            "application": TEXT,
            "environment": TEXT,
            "artifact": ARTIFACT,
            "observed_artifact": ARTIFACT,
            "expected_generation": TEXT,
            "strategy": TEXT,
            "conditions_digest": DIGEST,
        }
    ),
}


def _envelope(kind: str, properties: Doc) -> Doc:
    return planning.fields(
        {"schema_version": {"const": VERSION}, "profile": {"const": PROFILE}, "kind": {"const": kind}, **properties}
    )


REQUEST_SCHEMA = _envelope(
    "delivery-request",
    {
        "objective": OBJECTIVE,
        "operation_key": DIGEST,
        "stage": {"enum": list(STAGES)},
        "subject": {"type": "object"},
        "native_payload_digest": DIGEST,
    },
)
APPROVAL_SCHEMA = _envelope(
    "delivery-approval",
    {
        "issuer": TEXT,
        "approval_id": TEXT,
        "objective": OBJECTIVE,
        "stage": {"enum": list(STAGES)},
        "operation_key": DIGEST,
        "request_digest": DIGEST,
        "subject_digest": DIGEST,
        "resource_key": DIGEST,
        "policy_digest": DIGEST,
        "adapter": ADAPTER,
        "contract": CONTRACT,
        "journal": JOURNAL,
        "limits": BUDGET,
        "not_before": TEXT,
        "expires_at": TEXT,
    },
)
INTENT_SCHEMA = _envelope(
    "delivery-intent",
    {
        "id": DIGEST,
        "request": REQUEST_SCHEMA,
        "request_digest": DIGEST,
        "subject_digest": DIGEST,
        "resource_key": DIGEST,
        "authority_digest": DIGEST,
        "approval_id": TEXT,
        "policy_digest": DIGEST,
        "adapter": ADAPTER,
        "journal": JOURNAL,
        "cost": BUDGET,
    },
)
CLAIMS = {
    "implementation_verified": False,
    "operational_health_established": False,
    "later_stage_authorized": False,
    "fresh_observation": False,
    "self_authenticating": False,
}
RECEIPT_SCHEMA = _envelope(
    "delivery-receipt",
    {
        "id": DIGEST,
        "intent_id": DIGEST,
        "operation_key": DIGEST,
        "request_digest": DIGEST,
        "authority_digest": DIGEST,
        "stage": {"enum": list(STAGES)},
        "adapter": ADAPTER,
        "outcome": {"enum": ["uncertain", "rejected", "applied"]},
        "effect": {"type": ["object", "null"]},
        "provenance_digest": DIGEST,
        "reason": TEXT,
        "claims": {"const": CLAIMS},
    },
)
EFFECTS = {
    "plan": planning.fields({"proposal_id": DIGEST, "proposal_revision": POSITIVE}),
    "merge": planning.fields(
        {
            "repository": REPOSITORY,
            "pull_request": POSITIVE,
            "head_revision": SHA,
            "base_revision": SHA,
            "method": MERGE_METHOD,
            "commit": SHA,
            "remote_receipt_digest": DIGEST,
        }
    ),
    "publish": planning.fields(
        {
            "workflow": WORKFLOW,
            "source": SOURCE,
            "version": TEXT,
            "destination": DESTINATION,
            "artifacts": ARTIFACTS,
            "publisher_run": TEXT,
            "remote_receipt_digest": DIGEST,
        }
    ),
    "deploy": planning.fields(
        {
            "workflow": WORKFLOW,
            "application": TEXT,
            "environment": TEXT,
            "artifact": ARTIFACT,
            "generation": TEXT,
            "rollout_id": TEXT,
            "remote_receipt_digest": DIGEST,
        }
    ),
}
EFFECTS["rollback"] = EFFECTS["deploy"]


@dataclass(frozen=True)
class ApprovalPin:
    """Content and reviewer identity selected by trusted bootstrap, never by the proposed document."""

    issuer: str
    approval_id: str
    digest: str


class DeliveryAdapter(Protocol):
    """Installed trusted stage implementation; candidate data cannot load an adapter.

    The native payload must match its retained digest and receive concrete stage validation before
    effects. Inspection returns current provider facts and real conditional/idempotency guarantees.
    Invocation needs separately retained intent/accounting and authority, not these validation
    results. Reconciliation discovers outcomes without sending a replacement effect. All methods
    must enforce the supplied whole-call deadline, including nested requests and waiting.
    A journal retains canonical native payload bytes and supplies a newly decoded copy on every
    call. Validation must return a copied document matching ``native_payload_digest``; adapters
    must not depend on a mutable last-validation cache. Operator configuration has its own digest.
    """

    @property
    def adapter_id(self) -> str: ...

    @property
    def configuration_digest(self) -> str:
        """Recompute actual installed configuration; do not echo the approval's expected pin."""
        ...

    def validate_payload(self, request: Doc, payload: Doc) -> Doc: ...

    def inspect(self, request: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc: ...

    def invoke(self, intent: Doc, payload: Doc, authorization: Doc, *, deadline: store.Deadline) -> Doc: ...

    def reconcile(self, intent: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc: ...


def digest(value: Any) -> str:
    """Content identity only; a digest cannot approve itself or establish an external effect."""
    return planning.digest(value)


def _document(value: Any) -> Doc:
    try:
        copied = planning.plain(value)
        if not isinstance(copied, dict):
            raise ValueError("delivery documents require JSON objects")
        pending: list[tuple[Any, int]] = [(copied, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise ValueError("delivery document exceeds structural limits")
            if isinstance(item, dict):
                pending.extend((part, depth + 1) for pair in item.items() for part in pair)
            elif isinstance(item, list):
                pending.extend((part, depth + 1) for part in item)
            elif isinstance(item, str) and (
                not item.strip()
                or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in item)
            ):
                raise ValueError("delivery labels must be nonempty text without control characters")
        if len(planning.canonical(copied).encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("delivery document exceeds its byte limit")
        return copied
    except RecursionError as error:
        raise ValueError("delivery document is excessively nested") from error


def _schema(value: Any, schema: Doc) -> Doc:
    selected = _document(value)
    try:
        jsonschema.validate(selected, schema)
    except jsonschema.ValidationError as error:
        raise ValueError("invalid product-delivery document: " + error.message) from error
    return selected


def _timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timestamp lacks a timezone")
        return parsed.astimezone(dt.UTC)
    except (ValueError, OverflowError) as error:
        raise ValueError("delivery lifetime requires timezone-aware timestamps") from error


def _repository(value: Doc) -> None:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["full_name"]) is None:
        raise ValueError("delivery repository requires an owner/name identity")


def _artifact(value: Doc) -> None:
    name = value["name"]
    if name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("delivery artifact names must be literal filenames")


def _identity_fields(subject: Doc) -> None:
    for name in ("repository", "head_repository"):
        if name in subject:
            _repository(subject[name])
    for name in ("source", "workflow"):
        if name in subject:
            _repository(subject[name]["repository"])
    artifacts = subject.get("artifacts", [])
    if len({artifact["name"] for artifact in artifacts}) != len(artifacts):
        raise ValueError("delivery artifacts require unique names")
    for artifact in [*artifacts, *(subject[key] for key in ("artifact", "observed_artifact") if key in subject)]:
        _artifact(artifact)


def _subject(stage: str, value: Any) -> Doc:
    subject = _schema(value, SUBJECTS[stage])
    _identity_fields(subject)
    if stage == "merge":
        reference = subject["base_ref"]
        if (
            not reference.startswith("refs/heads/")
            or reference.endswith(("/", ".", ".lock"))
            or any(item in reference for item in ("..", "//", "@{", "\\", " ", "~", "^", ":", "?", "*", "["))
            or any(part.startswith(".") or part.endswith(".lock") for part in reference.split("/"))
        ):
            raise ValueError("delivery merge requires a literal branch ref")
    return subject


def validate_request(value: Any) -> Doc:
    """Validate exact identity declarations, not native payload semantics or live preconditions."""
    selected = _schema(value, REQUEST_SCHEMA)
    selected["subject"] = _subject(selected["stage"], selected["subject"])
    return selected


def resource_key(value: Any) -> str:
    """Stable declared conflict key, independent of operation aliases, revisions and policy changes.

    Concrete adapters must verify canonical native identities and reject aliases of the same actual
    destination. This helper cannot discover that two user-provided labels name one environment.
    Deployment and rollback deliberately share a conflict domain.
    """
    request = validate_request(value)
    stage, subject = request["stage"], request["subject"]
    if stage == "merge":
        resource = {
            "kind": "pull-request",
            "repository_id": subject["repository"]["id"],
            "number": subject["pull_request"],
        }
    elif stage == "publish":
        resource = {"kind": "publication", "destination": subject["destination"], "version": subject["version"]}
    elif stage in ("deploy", "rollback"):
        resource = {"kind": "environment", **{key: subject[key] for key in ("application", "environment")}}
    else:
        resource = {"kind": "planning", "original_goal": request["objective"]["original_goal"]}
    return digest(resource)


def _journal(journal: Doc) -> None:
    path = PurePosixPath(journal["path"])
    if (
        not path.is_absolute()
        or path.root != "/"
        or path.as_posix() != journal["path"]
        or path.name in ("", ".", "..")
        or ".." in path.parts
        or "\\" in journal["path"]
    ):
        raise ValueError("delivery journal requires a normalized absolute file path")
    identity = uuid.UUID(hex=journal["id"])
    if identity.version != 4 or identity.hex != journal["id"]:
        raise ValueError("delivery journal requires its initialized UUID")


def _approval(value: Any) -> Doc:
    selected = _schema(value, APPROVAL_SCHEMA)
    _journal(selected["journal"])
    limits = selected["limits"]
    if limits["operations"] < 1 or limits["wall_time_ms"] < 1:
        raise ValueError("delivery budgets require finite operation and time capacity")
    if _timestamp(selected["not_before"]) >= _timestamp(selected["expires_at"]):
        raise ValueError("delivery approval lifetime is empty")
    return selected


def validate_approval(value: Any, *, pin: ApprovalPin, now: dt.datetime) -> Doc:
    """Match externally selected content/reviewer pins and lifetime; do not mint a runtime grant.

    This pure check does not authenticate a signature, prove independent review, check revocation,
    reserve a budget, inspect storage or authorize adapter invocation.
    """
    selected = _approval(value)
    if not isinstance(pin, ApprovalPin):
        raise ValueError("delivery approval requires an independently selected ApprovalPin")
    if (selected["issuer"], selected["approval_id"], digest(selected)) != (pin.issuer, pin.approval_id, pin.digest):
        raise ValueError("delivery approval does not match the independently selected pin")
    if type(now) is not dt.datetime or now.utcoffset() is None:
        raise ValueError("delivery validation requires an explicit timezone-aware current time")
    if not _timestamp(selected["not_before"]) <= now.astimezone(dt.UTC) < _timestamp(selected["expires_at"]):
        raise ValueError("delivery approval is not current")
    return selected


def _identity(value: Doc) -> None:
    if value["id"] != digest({key: item for key, item in value.items() if key != "id"}):
        raise ValueError("delivery document content identity disagrees")


def _intent(value: Any) -> Doc:
    selected = _schema(value, INTENT_SCHEMA)
    _journal(selected["journal"])
    request = validate_request(selected["request"])
    if any(
        selected[key] != item
        for key, item in {
            "request_digest": digest(request),
            "subject_digest": digest(request["subject"]),
            "resource_key": resource_key(request),
        }.items()
    ):
        raise ValueError("delivery intent and request identities disagree")
    cost = selected["cost"]
    if cost["operations"] != 1 or cost["wall_time_ms"] < 1 or cost["rollbacks"] != int(request["stage"] == "rollback"):
        raise ValueError("delivery intent requires exact stage cost declarations")
    _identity(selected)
    return selected


def validate_intent(value: Any, *, approval: Any) -> Doc:
    """Check exact prepared content, including historical approvals.

    Before invocation, trusted code must separately repeat current ``validate_approval`` and native
    precondition/revocation checks and durably reserve the budget. This parser releases no grant.
    """
    selected, approved = _intent(value), _approval(approval)
    request = validate_request(selected["request"])
    expected = {
        "request_digest": digest(request),
        "subject_digest": digest(request["subject"]),
        "resource_key": resource_key(request),
        "authority_digest": digest(approved),
        "approval_id": approved["approval_id"],
        "policy_digest": approved["policy_digest"],
        "adapter": approved["adapter"],
        "journal": approved["journal"],
    }
    if any(selected[key] != item for key, item in expected.items()) or any(
        approved[key] != item
        for key, item in {
            "request_digest": expected["request_digest"],
            "subject_digest": expected["subject_digest"],
            "resource_key": expected["resource_key"],
            "objective": request["objective"],
            "stage": request["stage"],
            "operation_key": request["operation_key"],
        }.items()
    ):
        raise ValueError("delivery intent, request and approval identities disagree")
    cost = selected["cost"]
    if (
        cost["operations"] != 1
        or cost["wall_time_ms"] < 1
        or cost["rollbacks"] != int(request["stage"] == "rollback")
        or any(cost[key] > approved["limits"][key] for key in cost)
    ):
        raise ValueError("delivery intent exceeds its declared immutable budget scope")
    return selected


def validate_receipt(value: Any, *, intent: Any) -> Doc:
    """Check retained exact-stage facts; a successful replay is historical, never fresh health.

    Matching identities does not independently authenticate the provider receipt or establish that
    an effect happened. The concrete adapter must derive and verify those facts independently.
    Uncertain receipts can preserve unexpected actual effects; only applied effects must exactly
    match the approved subject. Neither form permits a retry or automatic compensation.
    """
    selected, prepared = _schema(value, RECEIPT_SCHEMA), _intent(intent)
    request = validate_request(prepared["request"])
    expected = {
        "intent_id": prepared["id"],
        "operation_key": request["operation_key"],
        "request_digest": digest(request),
        "authority_digest": prepared["authority_digest"],
        "stage": request["stage"],
        "adapter": prepared["adapter"],
    }
    if prepared["request_digest"] != expected["request_digest"] or any(
        selected[key] != item for key, item in expected.items()
    ):
        raise ValueError("delivery receipt does not belong to the retained exact intent")
    effect = selected["effect"]
    if selected["outcome"] == "applied" and effect is None:
        raise ValueError("applied delivery receipt requires actual stage effect identity")
    if selected["outcome"] == "rejected" and effect is not None:
        raise ValueError("rejected delivery receipt cannot claim an applied effect")
    if effect is not None:
        effect = _schema(effect, EFFECTS[request["stage"]])
        _identity_fields(effect)
        subject = request["subject"]
        matched = {
            "merge": ("repository", "pull_request", "head_revision", "base_revision", "method"),
            "publish": ("workflow", "source", "version", "destination", "artifacts"),
            "deploy": ("workflow", "application", "environment", "artifact"),
            "rollback": ("workflow", "application", "environment", "artifact"),
            "plan": (),
        }[request["stage"]]
        if selected["outcome"] == "applied" and any(effect[key] != subject[key] for key in matched):
            raise ValueError("delivery receipt effect differs from the approved stage subject")
    _identity(selected)
    return selected
