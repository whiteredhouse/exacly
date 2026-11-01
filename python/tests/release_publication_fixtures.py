"""Independent local publisher/verifier bootstrap for controlled acceptance tests only."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from pathlib import Path
from typing import Any

from exacly import delivery_automation as delivery
from exacly import execution_store as store
from exacly import owning_publisher as owner
from exacly import release_publication as publication

Doc = dict[str, Any]


def retained(path: Path, value: Doc | bytes) -> str:
    raw = value if isinstance(value, bytes) else store.canonical(value).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return owner.byte_digest(raw)


def make_case(root: Path, *, data: bytes = b"approved prebuilt artifact\n") -> Doc:
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    for label in ("registry", "artifacts", "keys", "operator"):
        (root / label).mkdir(mode=0o700)
    registry = {"path": str(root / "registry"), "id": owner.initialize_root(root / "registry")}
    artifact_store = {
        "path": str(root / "artifacts"),
        "id": owner.initialize_root(root / "artifacts", artifact_store=True),
    }
    publisher_key = {
        "path": str(root / "keys" / "publisher.key"),
        "id": owner.initialize_key(root / "keys" / "publisher.key", purpose="publisher"),
    }
    verifier_key = {
        "path": str(root / "keys" / "verifier.key"),
        "id": owner.initialize_key(root / "keys" / "verifier.key", purpose="artifact-verifier"),
    }
    artifact: Doc = {"name": "package.whl", "digest": owner.byte_digest(data), "size": len(data)}
    retained(root / "artifacts" / artifact["digest"].removeprefix("sha256:"), data)
    repo = {"id": 713, "full_name": "approved/example"}
    source = {"repository": repo, "revision": "a" * 40}
    workflow = {
        "repository": repo,
        "id": "local-owning-publisher",
        "revision": "b" * 40,
        "contract_digest": owner.workflow_code_digest(),
    }
    producer = {
        "repository": repo,
        "id": "independent-artifact-verifier",
        "revision": "c" * 40,
        "contract_digest": delivery.digest("verifier-contract"),
    }
    contract = {key: delivery.digest(key) for key in ("model", "criteria", "bindings", "observer")}
    now = dt.datetime.now(dt.UTC)
    before, expires = (now - dt.timedelta(minutes=1)).isoformat(), (now + dt.timedelta(minutes=10)).isoformat()
    verification = owner.sign_verification(
        {
            "profile": owner.VERIFICATION_PROFILE,
            "key_id": verifier_key["id"],
            "source": source,
            "artifacts": [artifact],
            "producer": producer,
            "contract": contract,
            "result": "passed",
            "issued_at": before,
            "expires_at": expires,
        },
        verifier_key,
    )
    verification_digest = retained(root / "operator" / "verification.json", verification)
    objective = {"id": delivery.digest("delivery-objective"), "original_goal": delivery.digest("original-goal")}
    operation_key = delivery.digest("publish-version")
    subject = {
        "workflow": workflow,
        "source": source,
        "version": "1.2.3",
        "destination": {"registry": "local-release-registry:" + registry["id"], "package": "approved-package"},
        "artifacts": [artifact],
    }
    policy = {"profile": publication.PROFILE, "contract": contract}
    authority = {
        "issuer": "independent-owner",
        "approval_id": "publication-approval",
        "journal": {"path": str(root / "operator" / "delivery.sqlite"), "id": uuid.uuid4().hex},
        "limits": {"operations": 1, "wall_time_ms": 120000, "reads": 256, "rollbacks": 0},
        "policy_digest": delivery.digest(policy),
    }
    owner_config = {
        "profile": owner.OWNER_PROFILE,
        "publisher_id": "approved-local-publisher",
        "workflow_code_digest": owner.workflow_code_digest(),
        "scope": {"objective": objective, "operation_key": operation_key, "subject": subject},
        "contract": contract,
        "registry": registry,
        "artifact_store": artifact_store,
        "key": publisher_key,
        "verification_key": verifier_key,
        "verification_producer": producer,
        "delivery_authority": authority,
        "inputs": [{"artifact": artifact, "blob": artifact["digest"]}],
        "verification": {"path": str(root / "operator" / "verification.json"), "digest": verification_digest},
        "not_before": before,
        "expires_at": expires,
        "maximum_artifact_bytes": 64 * 1024 * 1024,
    }
    owner_digest = retained(root / "operator" / "publisher.json", owner_config)
    config = {
        "profile": publication.PROFILE,
        "owner_configuration": {"path": str(root / "operator" / "publisher.json"), "digest": owner_digest},
        "call_limits": {
            method: {"wall_time_ms": 120000, "read_calls": 1, "write_calls": int(method == "invoke")}
            for method in ("inspect", "invoke", "reconcile")
        },
    }
    adapter = publication.ReleasePublicationAdapter(config, policy)
    native = {
        "profile": publication.PROFILE,
        "operation_profile_digest": adapter.operation_profile_digest,
        "inputs": owner_config["inputs"],
        "verification_digest": verification_digest,
    }
    request = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-request",
        "objective": objective,
        "operation_key": operation_key,
        "stage": "publish",
        "subject": subject,
        "native_payload_digest": delivery.digest(native),
    }
    approval = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-approval",
        **authority,
        "objective": objective,
        "stage": "publish",
        "operation_key": operation_key,
        "request_digest": delivery.digest(request),
        "subject_digest": delivery.digest(subject),
        "resource_key": delivery.resource_key(request),
        "adapter": {"id": publication.PROFILE, "configuration_digest": adapter.configuration_digest},
        "contract": contract,
        "not_before": before,
        "expires_at": expires,
    }
    intent = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-intent",
        "request": request,
        "request_digest": delivery.digest(request),
        "subject_digest": delivery.digest(subject),
        "resource_key": delivery.resource_key(request),
        "authority_digest": delivery.digest(approval),
        "approval_id": approval["approval_id"],
        "policy_digest": approval["policy_digest"],
        "adapter": approval["adapter"],
        "journal": approval["journal"],
        "cost": {"operations": 1, "wall_time_ms": 120000, "reads": 256, "rollbacks": 0},
    }
    intent["id"] = delivery.digest(intent)
    delivery.validate_intent(intent, approval=approval)
    return {
        "adapter": adapter,
        "configuration": config,
        "owner": owner_config,
        "policy": policy,
        "request": request,
        "payload": native,
        "approval": approval,
        "intent": intent,
        "artifact_bytes": data,
        "root": root,
        "verification": verification,
    }
