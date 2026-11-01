"""Controlled native adapter plus real private delivery storage and watchdogs."""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exacly import delivery_automation as contracts
from exacly import delivery_store
from exacly.delivery_engine import DeliveryEngine
from exacly.execution_store import Deadline
from test_delivery_contracts import NOW, approval, effect, pin, request

Doc = dict[str, Any]
CAPS = {
    "inspect": {"wall_time_ms": 3000, "read_calls": 2, "write_calls": 0},
    "invoke": {"wall_time_ms": 3000, "read_calls": 3, "write_calls": 2},
    "reconcile": {"wall_time_ms": 3000, "read_calls": 2, "write_calls": 0},
}


def read(path: Path) -> Doc:
    return dict(json.loads(path.read_text()))


def write(path: Path, value: Doc) -> None:
    temporary = path.with_name(path.name + "." + str(os.getpid()))
    with temporary.open("w") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class ControlledAdapter:
    adapter_id = "controlled-merge-v1"

    def __init__(self, remote: Path, journal: Path, journal_id: str, *, caps: Doc | None = None) -> None:
        self.remote, self.journal, self.journal_id = remote, journal, journal_id
        self._caps = copy.deepcopy(CAPS if caps is None else caps)
        self.config = "original configuration"
        self.key_id = "unchanged reviewed key"
        self.credential_descriptor = "first installation token descriptor"

    @property
    def configuration_digest(self) -> str:
        return contracts.digest(
            {"operation_profile": self.operation_profile_digest, "credential": self.credential_descriptor}
        )

    @property
    def operation_profile_digest(self) -> str:
        return contracts.digest({"config": self.config, "limits": self._caps, "key": self.key_id})

    @property
    def call_limits(self) -> Doc:
        return copy.deepcopy(self._caps)

    @property
    def approval_context(self) -> Doc:
        selected = approval(request())
        return {"policy_digest": selected["policy_digest"], "contract": selected["contract"]}

    def validate_payload(self, selected: Doc, payload: Doc) -> Doc:
        if (
            set(payload) != {"profile", "merge_revision", "operation_profile_digest"}
            or payload["profile"] != "controlled-merge-v1"
        ):
            raise ValueError("invalid controlled native payload")
        if contracts.digest(payload) != selected["native_payload_digest"]:
            raise ValueError("changed controlled native payload")
        if payload["operation_profile_digest"] != self.operation_profile_digest:
            raise ValueError("changed controlled operation profile")
        return copy.deepcopy(payload)

    def _proof(self, selected: Doc, payload: Doc, *, ready: bool, applied: bool) -> Doc:
        return {
            "kind": "controlled-proof-v1",
            "request_digest": contracts.digest(selected),
            "payload_digest": contracts.digest(payload),
            "ready": ready,
            "applied": applied,
        }

    def inspect(self, selected: Doc, payload: Doc, *, deadline: Deadline) -> Doc:
        value = read(self.remote)
        value["reads"] += 1
        write(self.remote, value)
        ready = value["mode"] != "blocked"
        if value["mode"] == "inspect_hang":
            time.sleep(20)
        return {
            "ready": ready,
            "reason": "Current controlled preconditions observed.",
            "provenance": self._proof(
                selected, payload, ready=False if value["mode"] == "ready_lie" else ready, applied=False
            ),
        }

    def invoke(self, intent: Doc, payload: Doc, authorization: Doc, *, deadline: Deadline) -> Doc:
        selected = intent["request"]
        saved = delivery_store.show(self.journal, self.journal_id, selected["operation_key"])["operation"]
        assert saved["phase"] == "may_have_started", "provider write preceded durable intent"
        assert saved["calls"][-1]["owner"] is not None, "effect preceded actual watchdog ownership"
        value = read(self.remote)
        if value["mode"] == "before":
            raise OSError("do-not-retain secret transport header")
        if value["mode"] == "hang":
            value["started"] = True
            write(self.remote, value)
            time.sleep(20)
        if value["mode"] == "wait":
            value["started"] = True
            write(self.remote, value)
            while not self.remote.with_suffix(".release").exists():
                time.sleep(0.01)
        if value["mode"] == "delayed":
            value["started"] = True
            write(self.remote, value)
            time.sleep(3)
        value["writes"] += 1
        value["applied"] = True
        value["operation"] = selected["operation_key"]
        write(self.remote, value)
        if value["mode"] == "after":
            raise OSError("lost acknowledgement with private token text")
        observed = effect(selected)
        if value["mode"] == "wrong_commit":
            observed["commit"] = "f" * 40
        if value["mode"] == "wrong_subject":
            observed["head_revision"] = "f" * 40
        proof = self._proof(selected, payload, ready=True, applied=value["mode"] != "effect_lie")
        if value["mode"] == "secret_proof":
            proof["unexpected_authorization"] = "do-not-retain-private-provider-credential"
        return {
            "outcome": "applied",
            "effect": observed,
            "reason": "Exact controlled merge applied.",
            "provenance": proof,
        }

    def reconcile(self, intent: Doc, payload: Doc, *, deadline: Deadline) -> Doc:
        selected = intent["request"]
        value = read(self.remote)
        value["reads"] += 1
        write(self.remote, value)
        applied = value["applied"] and value.get("operation") == selected["operation_key"]
        return {
            "outcome": "applied" if applied else "uncertain",
            "effect": effect(selected) if applied else None,
            "reason": "Exact controlled publication discovered." if applied else "Remote absence proves no outcome.",
            "provenance": self._proof(selected, payload, ready=True, applied=applied),
        }

    def validate_provenance(self, method: str, selected: Doc, payload: Doc, provenance: Doc) -> Doc:
        if set(provenance) != {"kind", "request_digest", "payload_digest", "ready", "applied"}:
            raise ValueError("unallowlisted controlled provenance")
        if provenance["kind"] != "controlled-proof-v1" or provenance["request_digest"] != contracts.digest(selected):
            raise ValueError("wrong controlled request proof")
        if provenance["payload_digest"] != contracts.digest(payload):
            raise ValueError("wrong controlled payload proof")
        if type(provenance["ready"]) is not bool or type(provenance["applied"]) is not bool:
            raise ValueError("invalid controlled native verdict")
        return copy.deepcopy(provenance)

    def validate_result(self, method: str, selected: Doc, payload: Doc, result: Doc) -> Doc:
        self.validate_provenance(method, selected, payload, result["provenance"])
        if method == "inspect":
            if result["ready"] != result["provenance"]["ready"]:
                raise ValueError("readiness differs from actual native proof")
        elif result["outcome"] == "applied" and (
            result["provenance"]["applied"] is not True or result["effect"]["commit"] != payload["merge_revision"]
        ):
            raise ValueError("claimed merge differs from actual native proof")
        return copy.deepcopy(result)


@dataclass
class Case:
    journal: Path
    journal_id: str
    remote: Path
    request: Doc
    payload: Doc
    approval: Doc
    adapter: ControlledAdapter
    engine: DeliveryEngine

    def apply(self) -> Doc:
        return self.engine.apply(self.request, self.payload, approval=self.approval, pin=pin(self.approval))

    def discover(self) -> Doc:
        return self.engine.reconcile(self.request["operation_key"], approval=self.approval, pin=pin(self.approval))


def make_case(tmp_path: Path, *, mode: str = "normal", caps: Doc | None = None, limits: Doc | None = None) -> Case:
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    journal = folder / "delivery.sqlite3"
    selected = request()
    total = {"operations": 5, "wall_time_ms": 200000, "reads": 100, "rollbacks": 0} if limits is None else limits
    initialized = delivery_store.initialize(
        journal,
        objective=selected["objective"],
        limits=total,
        native_limits={"wall_time_ms": 5000, "read_calls": 20, "write_calls": 2},
    )
    remote = tmp_path / "controlled-remote.json"
    write(remote, {"reads": 0, "writes": 0, "applied": False, "mode": mode})
    adapter = ControlledAdapter(remote, journal, initialized["id"], caps=caps)
    payload = {
        "profile": "controlled-merge-v1",
        "merge_revision": "e" * 40,
        "operation_profile_digest": adapter.operation_profile_digest,
    }
    selected["native_payload_digest"] = contracts.digest(payload)
    approved = approval(selected)
    approved["journal"] = {"path": str(journal), "id": initialized["id"]}
    approved["limits"] = copy.deepcopy(total)
    approved["adapter"] = {"id": adapter.adapter_id, "configuration_digest": adapter.configuration_digest}
    engine = DeliveryEngine(journal, journal_id=initialized["id"], adapter_factory=lambda: adapter, clock=lambda: NOW)
    return Case(journal, initialized["id"], remote, selected, payload, approved, adapter, engine)
