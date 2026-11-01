"""Exact-byte release publication through a separately configured local owning workflow.

This first concrete profile is a private filesystem registry in one coordination domain. It is
not a PyPI, GitHub Releases or general workflow-dispatch adapter. Its receipts establish immutable
publication only; verification remains a separately authenticated prerequisite.
"""

from __future__ import annotations

import copy
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from exacly import delivery_automation as delivery
from exacly import delivery_profile, planning
from exacly import execution_store as store

Doc = dict[str, Any]
PROFILE = "local-owning-publisher-v1"
FIELDS = planning.fields
TEXT = delivery.TEXT
DIGEST = delivery.DIGEST
LIMIT = FIELDS(
    {
        "wall_time_ms": {"type": "integer", "minimum": 1, "maximum": 120000},
        "read_calls": {"type": "integer", "minimum": 1, "maximum": 256},
        "write_calls": {"type": "integer", "minimum": 0, "maximum": 1},
    }
)
CONFIG_SCHEMA = FIELDS(
    {
        "profile": {"const": PROFILE},
        "owner_configuration": FIELDS({"path": TEXT, "digest": DIGEST}),
        "call_limits": FIELDS({name: LIMIT for name in ("inspect", "invoke", "reconcile")}),
    }
)
POLICY_SCHEMA = FIELDS({"profile": {"const": PROFILE}, "contract": delivery.CONTRACT})
INPUT = FIELDS({"artifact": delivery.ARTIFACT, "blob": DIGEST})
PAYLOAD_SCHEMA = FIELDS(
    {
        "profile": {"const": PROFILE},
        "operation_profile_digest": DIGEST,
        "inputs": {"type": "array", "minItems": 1, "maxItems": 64, "items": INPUT},
        "verification_digest": DIGEST,
    }
)
RESULT_SCHEMA = FIELDS(
    {
        "outcome": {"enum": ["applied", "uncertain", "rejected"]},
        "effect": {"type": ["object", "null"]},
        "reason": TEXT,
        "provenance": {"type": "object"},
    }
)


def validate_configuration(value: Any) -> Doc:
    selected = delivery._schema(value, CONFIG_SCHEMA)
    path = Path(selected["owner_configuration"]["path"])
    store.require(
        path.is_absolute() and ".." not in path.parts, "owner configuration requires a canonical absolute path"
    )
    for phase, limit in selected["call_limits"].items():
        store.require(
            limit["write_calls"] == (1 if phase == "invoke" else 0), "publication phase has unsafe write bounds"
        )
    return selected


def validate_policy(value: Any) -> Doc:
    return delivery._schema(value, POLICY_SCHEMA)


def _runtime_profile() -> Doc:
    return delivery_profile.implementation(
        required=(
            "release_publication.py",
            "owning_publisher.py",
            "delivery_engine.py",
            "delivery_store.py",
            "delivery_runtime.py",
            "delivery_io.py",
            "cli.py",
        )
    )


def _owner(configuration: Doc) -> Doc:
    from exacly import owning_publisher as owner

    selected = configuration["owner_configuration"]
    return owner.load_configuration(Path(selected["path"]), selected["digest"])


def configuration_digest(configuration: Any, policy: Any) -> str:
    selected, rule = validate_configuration(configuration), validate_policy(policy)
    owner = _owner(selected)
    return delivery.digest({"configuration": selected, "owner": owner, "policy": rule, "runtime": _runtime_profile()})


def validate_payload(request: Any, payload: Any, configuration: Any, policy: Any) -> Doc:
    selected = delivery.validate_request(request)
    native = delivery._schema(payload, PAYLOAD_SCHEMA)
    config, rule = validate_configuration(configuration), validate_policy(policy)
    owner = _owner(config)
    store.require(selected["stage"] == "publish", "publication requires its own stage authority")
    store.require(delivery.digest(native) == selected["native_payload_digest"], "publication payload identity changed")
    store.require(
        native["operation_profile_digest"] == configuration_digest(config, rule), "publication profile changed"
    )
    store.require(owner["contract"] == rule["contract"], "publisher contract differs from reviewed policy")
    store.require(
        owner["scope"] == {key: selected[key] for key in ("objective", "operation_key", "subject")},
        "owning publisher does not independently authorize the exact release scope",
    )
    store.require(
        owner["inputs"] == native["inputs"], "artifact input inventory differs from owning publisher approval"
    )
    store.require(owner["verification"]["digest"] == native["verification_digest"], "verification identity changed")
    store.require(
        [item["artifact"] for item in native["inputs"]] == selected["subject"]["artifacts"],
        "publication does not select the exact approved artifact inventory",
    )
    names = [item["artifact"]["name"] for item in native["inputs"]]
    store.require(len(set(names)) == len(names), "publication artifact names must be unique")
    for item in native["inputs"]:
        store.require(
            item["blob"] == item["artifact"]["digest"], "prebuilt artifact blob must identify its exact bytes"
        )
    version = selected["subject"]["version"]
    store.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version) is not None, "unsafe immutable version")
    for name in names:
        store.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", name) is not None, "unsafe artifact name")
    return native


def _bind_owner_receipt(intent: Doc, result: Doc) -> Doc:
    """Bind authenticated native facts to the explicit retained intent without discarding foreign facts."""
    receipt = result["provenance"]["receipt"]
    if receipt is not None and receipt["intent_id"] != intent["id"]:
        return {
            "outcome": "uncertain",
            "effect": None,
            "reason": "Publisher receipt belongs to another retained intent; reconcile without resending.",
            "provenance": copy.deepcopy(result["provenance"]),
        }
    return result


class ReleasePublicationAdapter:
    """Fixed installed owning workflow; candidate input cannot select an executable or config file.

    One invocation is a logical workflow call, bounded internally by the approved artifact inventory
    and byte limit. The engine must durably reserve intent and fence this subprocess before invoking
    it. Lost replies, partial versions and missing receipts are discovery-only uncertain outcomes.
    """

    def __init__(self, configuration: Doc, policy: Doc) -> None:
        self.configuration, self.policy = validate_configuration(configuration), validate_policy(policy)

    @property
    def adapter_id(self) -> str:
        return PROFILE

    @property
    def configuration_digest(self) -> str:
        return configuration_digest(self.configuration, self.policy)

    @property
    def operation_profile_digest(self) -> str:
        # This profile has no credential-rotation exception. Recovery requires the retained runtime.
        return self.configuration_digest

    @property
    def call_limits(self) -> Doc:
        return copy.deepcopy(self.configuration["call_limits"])

    @property
    def approval_context(self) -> Doc:
        return {"policy_digest": delivery.digest(self.policy), "contract": copy.deepcopy(self.policy["contract"])}

    def validate_payload(self, request: Doc, payload: Doc) -> Doc:
        return validate_payload(request, payload, self.configuration, self.policy)

    def inspect(self, request: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
        from exacly import owning_publisher as owner

        selected = self.validate_payload(request, payload)
        deadline.check()
        return owner.inspect(_owner(self.configuration), request, selected, deadline=deadline)

    def invoke(self, intent: Doc, payload: Doc, authorization: Doc, *, deadline: store.Deadline) -> Doc:
        from exacly import owning_publisher as owner

        request = intent["request"]
        native = self.validate_payload(request, payload)
        # Recompute after all preflight work; a changed runtime/configuration cannot retain the grant.
        store.require(
            intent["adapter"] == {"id": self.adapter_id, "configuration_digest": self.configuration_digest},
            "publisher configuration changed before workflow invocation",
        )
        selected = self.configuration["owner_configuration"]
        envelope = {
            "intent": intent,
            "payload": native,
            "authorization": authorization,
            "adapter_configuration": self.configuration,
            "policy": self.policy,
        }
        deadline.check()
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "exacly.owning_publisher",
                "--configuration",
                selected["path"],
                "--configuration-digest",
                selected["digest"],
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            output, _ = process.communicate(store.canonical(envelope).encode(), timeout=deadline.remaining())
        except BaseException:
            process.kill()
            process.wait()
            raise
        if process.returncode != 0 or len(output) > delivery.MAX_DOCUMENT_BYTES:
            return owner.uncertain("Owning publisher reply was unavailable; discover the retained version.")
        try:
            result = self.validate_result("invoke", request, native, owner.decode(output))
            return _bind_owner_receipt(intent, result)
        except ValueError:
            return owner.uncertain("Owning publisher reply could not be authenticated; discover the retained version.")

    def reconcile(self, intent: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
        from exacly import owning_publisher as owner

        native = self.validate_payload(intent["request"], payload)
        result = self.validate_result(
            "reconcile",
            intent["request"],
            native,
            owner.discover(_owner(self.configuration), intent, native, deadline=deadline),
        )
        return _bind_owner_receipt(intent, result)

    def validate_provenance(self, method: str, request: Doc, payload: Doc, provenance: Doc) -> Doc:
        from exacly import owning_publisher as owner

        store.require(method in ("inspect", "invoke", "reconcile"), "unknown publication phase")
        return owner.validate_provenance(request, payload, provenance, _owner(self.configuration))

    def validate_result(self, method: str, request: Doc, payload: Doc, result: Doc) -> Doc:
        from exacly import owning_publisher as owner

        self.validate_payload(request, payload)
        if method == "inspect":
            selected = delivery._schema(
                result, FIELDS({"ready": {"type": "boolean"}, "reason": TEXT, "provenance": {"type": "object"}})
            )
        else:
            selected = delivery._schema(result, RESULT_SCHEMA)
        proof = self.validate_provenance(method, request, payload, selected["provenance"])
        if selected.get("ready") is True:
            store.require(
                proof["verification"] is not None and proof["version_state"] == "absent",
                "publication readiness lacks current authenticated prerequisites",
            )
        if selected.get("outcome") == "applied":
            store.require(
                proof["receipt"] is not None and proof["version_state"] == "published",
                "publication success lacks authenticated retained version",
            )
            store.require(
                selected["effect"] == owner.effect(request, proof["receipt"]),
                "publication effect differs from exact owning publisher receipt",
            )
        elif method != "inspect":
            store.require(selected["effect"] is None, "unapplied publication cannot claim an artifact effect")
        return selected
