"""Explicit delivery command inputs and the fixed installed merge-adapter bootstrap.

This module loads no provider by name from JSON. Configuration and policy are captured lazily so
local history and exact terminal replay do not require a token, a key or the original config files.
Operational time comes from the engine's trusted UTC clock, never a command-line date override.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from exacly import delivery_automation as contracts

if TYPE_CHECKING:
    from exacly.github_merge import GitHubMergeAdapter

Doc = dict[str, Any]
TOKEN_ENVIRONMENT = "EXACLY_DELIVERY_MERGE_TOKEN"
SUCCESS = {"initialized", "inspected", "preview", "applied"}
BLOCKED = {"missing", "approval_required", "blocked", "prepared", "rejected", "uncertain"}


def read_document(path: Path) -> Doc:
    """Read one bounded regular strict-JSON object; reject duplicates and nonfinite values."""
    if os.name != "posix" or any(not hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK")):
        raise ValueError("delivery inputs require supported local POSIX file primitives")

    def unique(pairs: list[tuple[str, Any]]) -> Doc:
        result: Doc = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("delivery JSON contains duplicate keys")
            result[key] = value
        return result

    def nonfinite(value: str) -> Any:
        raise ValueError("delivery JSON contains nonfinite values")

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("delivery input must be a regular file")
        encoded = stream.read(contracts.MAX_DOCUMENT_BYTES + 1)
    if len(encoded) > contracts.MAX_DOCUMENT_BYTES:
        raise ValueError("delivery input exceeds its permitted byte limit")
    try:
        document = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
        return contracts._document(document)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("delivery input is not bounded UTF-8 JSON") from error


def merge_adapter(configuration: Doc, policy: Doc) -> GitHubMergeAdapter:
    """Construct only the installed GitHub merge profile; Python tests may inject a fixture here."""
    from exacly.github_merge import GitHubMergeAdapter

    return GitHubMergeAdapter(configuration, policy)


def _pin(args: argparse.Namespace) -> tuple[Doc | None, contracts.ApprovalPin | None]:
    values = (args.approval, args.approved_approval_digest, args.issuer, args.approval_id)
    if any(value is not None for value in values) and not all(value is not None for value in values):
        raise ValueError("--approval requires --approved-approval-digest, --issuer and --approval-id together")
    if args.apply and args.approval is None:
        raise ValueError("--apply requires a separately approved delivery approval and its external pin")
    if args.approval is None:
        return None, None
    return read_document(args.approval), contracts.ApprovalPin(
        issuer=args.issuer, approval_id=args.approval_id, digest=args.approved_approval_digest
    )


def run(args: argparse.Namespace) -> Doc:
    """Dispatch a fixed native operation without acquiring implicit authority or initializing reads."""
    options = {} if args.lock_timeout is None else {"lock_timeout": args.lock_timeout}
    if args.delivery_command == "key-init":
        from exacly import delivery_key

        key_path = args.key.absolute()
        identity = delivery_key.initialize(key_path, **options)
        return {
            "schema_version": "0.1.0",
            "profile": "product-delivery-key-v1",
            "status": "initialized",
            "key": {"path": str(key_path), "id": identity},
            "claims": dict(contracts.CLAIMS),
        }
    if args.delivery_command in {"init", "show", "revoke"}:
        from exacly import delivery_store
    if args.delivery_command == "init":
        return delivery_store.initialize(
            args.journal,
            objective=read_document(args.objective),
            limits=read_document(args.limits),
            native_limits=read_document(args.native_limits),
            **options,
        )
    if args.delivery_command == "show":
        return delivery_store.show(args.journal, args.journal_id, operation_key=args.operation_key, **options)
    if args.delivery_command == "revoke":
        return delivery_store.revoke(args.journal, args.journal_id, reason=args.reason, **options)
    approval, pin = _pin(args)
    request: Doc | None = None
    payload: Doc | None = None
    if args.delivery_command == "merge":
        request = contracts.validate_request(read_document(args.request))
        if request["stage"] != "merge":
            raise ValueError("delivery merge requires an exact merge-stage request")
        payload = read_document(args.payload)
    elif args.delivery_command != "reconcile":
        raise ValueError("unsupported installed delivery command")
    if args.delivery_command == "reconcile" and (approval is None or pin is None):
        raise ValueError("reconciliation requires a current, separately pinned read approval")
    from exacly.delivery_engine import DeliveryEngine

    def adapter_factory() -> GitHubMergeAdapter:
        return merge_adapter(read_document(args.config), read_document(args.policy))

    engine = DeliveryEngine(args.journal, journal_id=args.journal_id, adapter_factory=adapter_factory, **options)
    if args.delivery_command == "reconcile":
        assert approval is not None and pin is not None
        return engine.reconcile(args.operation_key, approval=approval, pin=pin)
    assert request is not None and payload is not None
    if args.apply:
        assert approval is not None and pin is not None
        return engine.apply(request, payload, approval=approval, pin=pin)
    return engine.preview(request, payload, approval=approval, pin=pin)


def exit_status(report: Doc) -> int:
    """Unknown or malformed native result statuses cannot become successful invocations."""
    status = report.get("status")
    if not isinstance(status, str):
        raise ValueError("delivery returned an invalid native report status")
    if status in SUCCESS:
        return 0
    if status in BLOCKED:
        return 1
    raise ValueError("delivery returned an unsupported native report status")
