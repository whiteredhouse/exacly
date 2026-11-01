"""Separately approved, durable product-delivery effects through trusted installed adapters.

Only the merge stage is supported by this engine slice. Preview is local and advisory. Native
effects require the real Linux watchdog, fresh current approval, authoritative journal reservations
and the adapter's verified provider preconditions. Historical receipts confer no new authority.
"""

from __future__ import annotations

import copy
import datetime as dt
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from exacly import controller_runtime, delivery_runtime, planning
from exacly import delivery_automation as contracts
from exacly import delivery_store as journal_store
from exacly import execution_store as store

Doc = dict[str, Any]
SUPPORTED_STAGES = {"merge"}
APPROVAL_CONTEXT = planning.fields({"policy_digest": contracts.DIGEST, "contract": contracts.CONTRACT})
READ_RECOVERY_SCHEMA = journal_store.READ_RECOVERY_SCHEMA
validate_read_recovery = journal_store.validate_read_recovery


class EngineAdapter(contracts.DeliveryAdapter, Protocol):
    """Additional trusted local contracts; no candidate-selected provider loader exists."""

    @property
    def call_limits(self) -> Doc:
        """Actual immutable per-phase limits, included in the installed configuration digest."""
        ...

    @property
    def approval_context(self) -> Doc:
        """Recompute actual selected policy and protected contract, never echo approval input."""
        ...

    @property
    def operation_profile_digest(self) -> str:
        """Actual stable operation scope; only independently reviewed credential descriptors may rotate."""
        ...

    def validate_provenance(self, method: str, request: Doc, payload: Doc, provenance: Doc) -> Doc:
        """Purely validate a copied, allowlisted native proof; no HTTP bodies or credentials."""
        ...

    def validate_result(self, method: str, request: Doc, payload: Doc, result: Doc) -> Doc:
        """Purely bind ready/outcome/effect to the native proof, including exact merge commit."""
        ...


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _copy(value: Any) -> Doc:
    return journal_store.copied(value)


def _metadata(history: Doc) -> Doc:
    return {
        "schema_version": history["schema_version"],
        "profile": history["profile"],
        "id": history["id"],
        "path": history["journal"],
        "objective": history["objective"],
        "limits": history["limits"],
        "native_limits": history["native_limits"],
    }


class DeliveryEngine:
    """One pinned local journal and a host-selected lazy adapter factory.

    Exact terminal replay precedes factory creation and needs no credentials, policy files or fresh
    authority. New effects always use the actual UTC default clock; an injected test clock is trusted
    Python bootstrap, never request data or a CLI argument.
    """

    def __init__(
        self,
        path: Path,
        *,
        journal_id: str,
        adapter_factory: Callable[[], EngineAdapter],
        clock: Callable[[], dt.datetime] | None = None,
        lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.path, self.journal_id = path, journal_id
        self._factory, self._clock, self._lock_timeout = adapter_factory, clock or _now, lock_timeout
        store.Deadline(lock_timeout)
        with journal_store.opened(path, journal_id, lock_timeout=lock_timeout) as journal:
            self._metadata = copy.deepcopy(journal.metadata)

    def _history(self, key: str | None = None) -> Doc:
        return journal_store.show(self.path, self.journal_id, key, lock_timeout=self._lock_timeout)

    def _scope(self, request: Doc) -> None:
        store.require(request["stage"] in SUPPORTED_STAGES, "this engine profile supports only merge delivery")
        store.require(
            request["objective"] == self._metadata["objective"], "delivery objective differs from initialized scope"
        )

    def _adapter(self, request: Doc, payload: Doc) -> tuple[EngineAdapter, Doc, Doc]:
        adapter = self._factory()
        validated = _copy(adapter.validate_payload(_copy(request), _copy(payload)))
        store.require(
            contracts.digest(validated) == contracts.digest(payload) == request["native_payload_digest"],
            "native payload validation changed its pinned content",
        )
        store.require(
            payload.get("operation_profile_digest") == adapter.operation_profile_digest,
            "native operation profile differs from its retained scope",
        )
        limits = _copy(adapter.call_limits)
        store.require(set(limits) == set(journal_store.METHODS), "native adapter call phases are incomplete")
        for method, value in limits.items():
            selected = journal_store.validate_native_limits(value)
            store.require(
                all(selected[key] <= self._metadata["native_limits"][key] for key in selected),
                "native adapter exceeds original objective call bounds",
            )
            store.require(method == "invoke" or selected["write_calls"] == 0, "read-only adapter phase permits writes")
        context = journal_store._schema(adapter.approval_context, APPROVAL_CONTEXT)
        return adapter, limits, context

    def _approval(
        self, value: Doc, pin: contracts.ApprovalPin, request: Doc, adapter: EngineAdapter, context: Doc
    ) -> Doc:
        approved = contracts.validate_approval(value, pin=pin, now=self._clock())
        store.require(
            approved["objective"] == self._metadata["objective"]
            and approved["journal"] == {"path": self._metadata["path"], "id": self.journal_id}
            and approved["limits"] == self._metadata["limits"]
            and approved["request_digest"] == contracts.digest(request)
            and approved["subject_digest"] == contracts.digest(request["subject"])
            and approved["operation_key"] == request["operation_key"]
            and approved["resource_key"] == contracts.resource_key(request)
            and approved["stage"] == request["stage"]
            and approved["adapter"] == {"id": adapter.adapter_id, "configuration_digest": adapter.configuration_digest}
            and approved["policy_digest"] == context["policy_digest"]
            and approved["contract"] == context["contract"],
            "current delivery approval does not cover actual policy, configuration and exact request",
        )
        return approved

    def _intent(self, request: Doc, approved: Doc, bounds: Doc) -> Doc:
        value = {
            "schema_version": contracts.VERSION,
            "profile": contracts.PROFILE,
            "kind": "delivery-intent",
            "request": _copy(request),
            "request_digest": contracts.digest(request),
            "subject_digest": contracts.digest(request["subject"]),
            "resource_key": contracts.resource_key(request),
            "authority_digest": contracts.digest(approved),
            "approval_id": approved["approval_id"],
            "policy_digest": approved["policy_digest"],
            "adapter": _copy(approved["adapter"]),
            "journal": _copy(approved["journal"]),
            "cost": {
                "operations": 1,
                "wall_time_ms": bounds["wall_time_ms"],
                "reads": bounds["read_calls"],
                "rollbacks": 0,
            },
        }
        value["id"] = contracts.digest(value)
        return contracts.validate_intent(value, approval=approved)

    def _report(
        self,
        request: Doc,
        status: str,
        reason: str,
        *,
        operation: Doc | None = None,
        historical: bool = False,
        live_checked: bool = False,
        packet: Doc | None = None,
    ) -> Doc:
        return {
            "schema_version": contracts.VERSION,
            "profile": contracts.PROFILE,
            "kind": "delivery-report",
            "status": status,
            "reason": journal_store._text(reason),
            "historical": historical,
            "live_checked": live_checked,
            "effect_authorized": False,
            "journal": {"path": self._metadata["path"], "id": self.journal_id},
            "operation_key": request["operation_key"],
            "request_digest": contracts.digest(request),
            "intent": copy.deepcopy(operation["intent"]) if operation else None,
            "receipt": copy.deepcopy(operation["receipt"]) if operation else None,
            "packet": copy.deepcopy(packet),
            "claims": copy.deepcopy(contracts.CLAIMS),
        }

    def _exact(self, operation: Doc, request: Doc, payload: Doc) -> None:
        store.require(
            operation["intent"]["request"] == request and operation["payload"] == payload,
            "operation key is already retained for another exact request or payload",
        )

    def _sync_cleanup(self, key: str) -> None:
        """Import a small immutable watchdog fact; do not let deadline children write SQLite."""
        with journal_store.opened(self.path, self.journal_id, write=True, lock_timeout=self._lock_timeout) as journal:
            operation = journal.read()["operations"].get(key)
            store.require(operation is not None, "unknown delivery cleanup operation")
            for call in operation["calls"]:
                if call["owner"] is None or call["cleanup"] is not None:
                    continue
                proof = delivery_runtime.read_cleanup(self._metadata, call["id"])
                if proof is None:
                    continue
                store.require(proof["owner"] == call["owner"]["process"], "cleanup receipt belongs to another owner")
                cleaned = bool(proof["cleaned"])

                def import_fact(item: Doc, cleaned: bool = cleaned) -> None:
                    item["cleanup"] = cleaned

                journal.update_call(key, call["id"], import_fact)

    def preview(
        self, request: Doc, payload: Doc, *, approval: Doc | None = None, pin: contracts.ApprovalPin | None = None
    ) -> Doc:
        selected, native = contracts.validate_request(request), _copy(payload)
        self._scope(selected)
        adapter, limits, context = self._adapter(selected, native)
        packet = {
            "request": selected,
            "native_payload_digest": contracts.digest(native),
            "resource_key": contracts.resource_key(selected),
            "adapter": {"id": adapter.adapter_id, "configuration_digest": adapter.configuration_digest},
            "approval_context": context,
            "call_limits": limits,
            "limits": self._metadata["limits"],
            "native_limits": self._metadata["native_limits"],
            "live_preconditions_checked": False,
        }
        store.require(
            (approval is None) == (pin is None), "approval document and external pin must be supplied together"
        )
        if approval is None or pin is None:
            return self._report(
                selected, "approval_required", "Review and separately pin exact delivery authority.", packet=packet
            )
        approved = self._approval(approval, pin, selected, adapter, context)
        packet["intent"] = self._intent(selected, approved, limits["invoke"])
        history = self._history()
        status = "blocked" if history["revoked"] else "preview"
        reason = (
            "Delivery objective is revoked."
            if history["revoked"]
            else "Local proposal only; provider preconditions remain unobserved."
        )
        return self._report(selected, status, reason, packet=packet)

    def apply(self, request: Doc, payload: Doc, *, approval: Doc, pin: contracts.ApprovalPin) -> Doc:
        store.require(
            isinstance(approval, dict) and approval.get("kind") != "delivery-read-recovery",
            "read-only recovery cannot authorize or invoke delivery apply",
        )
        selected, native = contracts.validate_request(request), _copy(payload)
        self._scope(selected)
        key = selected["operation_key"]
        saved = self._history(key)["operation"]
        if saved is not None:
            self._exact(saved, selected, native)
            if saved["phase"] in ("applied", "rejected", "may_have_started", "uncertain"):
                return self._report(
                    selected,
                    saved["phase"] if saved["phase"] != "may_have_started" else "uncertain",
                    "Retained outcome only; uncertain effects require discover-only reconciliation.",
                    operation=saved,
                    historical=True,
                )
        with delivery_runtime.invocation(self._metadata, lock_timeout=self._lock_timeout):
            saved = self._history(key)["operation"]
            if saved is not None:
                self._sync_cleanup(key)
                saved = self._history(key)["operation"]
                self._exact(saved, selected, native)
                if saved["phase"] != "prepared":
                    return self._report(
                        selected,
                        saved["phase"] if saved["phase"] != "may_have_started" else "uncertain",
                        "Another invocation retained this operation; no replacement was sent.",
                        operation=saved,
                        historical=True,
                    )
            adapter, limits, context = self._adapter(selected, native)
            approved = self._approval(approval, pin, selected, adapter, context)
            intended = self._intent(selected, approved, limits["invoke"])
            with journal_store.opened(
                self.path, self.journal_id, write=True, lock_timeout=self._lock_timeout
            ) as journal:
                saved = journal.prepare(intended, native, approved, pin, self._clock())
            inspected, saved = self._call(adapter, "inspect", saved, native, approved, pin, limits["inspect"])
            if not inspected["ready"]:
                return self._report(selected, "prepared", inspected["reason"], operation=saved, live_checked=True)
            # Scope/configuration are checked again immediately before the durably gated effect.
            context = journal_store._schema(adapter.approval_context, APPROVAL_CONTEXT)
            approved = self._approval(approval, pin, selected, adapter, context)
            _outcome, saved = self._call(adapter, "invoke", saved, native, approved, pin, limits["invoke"])
            return self._report(selected, saved["phase"], saved["reason"], operation=saved, live_checked=True)

    def reconcile(self, operation_key: str, *, approval: Doc, pin: contracts.ApprovalPin) -> Doc:
        history = self._history(operation_key)
        saved = history["operation"]
        store.require(saved is not None, "unknown delivery operation")
        selected, native = saved["intent"]["request"], saved["payload"]
        if saved["phase"] in ("applied", "rejected"):
            return self._report(
                selected,
                saved["phase"],
                "Historical receipt; no fresh provider observation.",
                operation=saved,
                historical=True,
            )
        store.require(
            saved["phase"] in ("may_have_started", "uncertain"),
            "prepared operation has no external effect to reconcile",
        )
        with delivery_runtime.invocation(self._metadata, lock_timeout=self._lock_timeout):
            self._sync_cleanup(operation_key)
            history = self._history(operation_key)
            saved = history["operation"]
            if saved["phase"] in ("applied", "rejected"):
                return self._report(
                    selected,
                    saved["phase"],
                    "Historical receipt; no fresh provider observation.",
                    operation=saved,
                    historical=True,
                )
            adapter, limits, context = self._adapter(selected, native)
            store.require(
                adapter.adapter_id == saved["intent"]["adapter"]["id"],
                "read reconciliation changes the original installed adapter identity",
            )
            if approval.get("kind") == "delivery-read-recovery":
                approved = validate_read_recovery(approval, pin=pin, now=self._clock())
                journal_store.recovery_scope(approved, saved, self._metadata, limits["reconcile"])
                store.require(
                    approved["new_configuration_digest"] == adapter.configuration_digest
                    and approved["operation_profile_digest"] == adapter.operation_profile_digest
                    and approved["policy_digest"] == context["policy_digest"]
                    and approved["contract"] == context["contract"],
                    "read recovery changes actual configuration, policy or operation profile",
                )
            else:
                store.require(not history["revoked"], "withdrawn scope needs separate read-only recovery")
                approved = self._approval(approval, pin, selected, adapter, context)
                original = saved["approval"]
                ignored = {"issuer", "approval_id", "not_before", "expires_at"}
                store.require(
                    {key: value for key, value in approved.items() if key not in ignored}
                    == {key: value for key, value in original.items() if key not in ignored},
                    "recovery approval changes the original exact scope",
                )
            _outcome, saved = self._call(adapter, "reconcile", saved, native, approved, pin, limits["reconcile"])
            return self._report(selected, saved["phase"], saved["reason"], operation=saved, live_checked=True)

    def _receipt(self, intent: Doc, result: Doc) -> Doc:
        value = {
            "schema_version": contracts.VERSION,
            "profile": contracts.PROFILE,
            "kind": "delivery-receipt",
            "intent_id": intent["id"],
            "operation_key": intent["request"]["operation_key"],
            "request_digest": intent["request_digest"],
            "authority_digest": intent["authority_digest"],
            "stage": intent["request"]["stage"],
            "adapter": _copy(intent["adapter"]),
            "outcome": result["outcome"],
            "effect": copy.deepcopy(result["effect"]),
            "provenance_digest": contracts.digest(result["provenance"]),
            "reason": result["reason"],
            "claims": copy.deepcopy(contracts.CLAIMS),
        }
        value["id"] = contracts.digest(value)
        return contracts.validate_receipt(value, intent=intent)

    def _validate_result(self, adapter: EngineAdapter, method: str, request: Doc, payload: Doc, raw: Any) -> Doc:
        selected = journal_store._schema(
            raw,
            journal_store.INSPECTION if method == "inspect" else journal_store.RESULT,
            limit=journal_store.MAX_PROVENANCE_BYTES,
        )
        journal_store._text(selected["reason"])
        proof = _copy(
            adapter.validate_provenance(method, _copy(request), _copy(payload), _copy(selected["provenance"]))
        )
        store.require(
            contracts.digest(proof) == contracts.digest(selected["provenance"]), "native provenance was rewritten"
        )
        validated = _copy(adapter.validate_result(method, _copy(request), _copy(payload), _copy(selected)))
        store.require(contracts.digest(validated) == contracts.digest(selected), "native result was rewritten")
        return selected

    def _boundary(self, method: str, intent: Doc, call_id: str, raw: Any = None) -> Doc:
        provenance = {
            "kind": "delivery-engine-boundary-v1",
            "intent_id": intent["id"],
            "call_id": call_id,
            "reason_code": "native-outcome-unresolved",
        }
        if raw is not None:
            with suppress(ValueError, TypeError, RecursionError):
                provenance["unvalidated_result_digest"] = contracts.digest(raw)
        reason = (
            "Native result or cleanup is unresolved; retain the reservation and reconcile without another invocation."
        )
        if method == "inspect":
            return {"ready": False, "reason": reason, "provenance": provenance}
        effect = None
        if isinstance(raw, dict) and isinstance(raw.get("effect"), dict):
            candidate = {"outcome": "uncertain", "effect": raw["effect"], "reason": reason, "provenance": provenance}
            try:
                self._receipt(intent, candidate)
                effect = copy.deepcopy(raw["effect"])
            except (ValueError, TypeError, RecursionError):
                pass
        return {"outcome": "uncertain", "effect": effect, "reason": reason, "provenance": provenance}

    def _call(
        self,
        adapter: EngineAdapter,
        method: str,
        operation: Doc,
        payload: Doc,
        approval: Doc,
        pin: contracts.ApprovalPin,
        bounds: Doc,
    ) -> tuple[Doc, Doc]:
        intent, request = operation["intent"], operation["intent"]["request"]
        key = request["operation_key"]
        with journal_store.opened(self.path, self.journal_id, write=True, lock_timeout=self._lock_timeout) as journal:
            call = journal.reserve_call(key, method, bounds, approval, pin, self._clock())
        # Retain the complete conservative slot; a shorter current grant can only reduce
        # this call's actual release window. This cannot cancel an already sent request.
        remaining_authority = (contracts._timestamp(approval["expires_at"]) - self._clock()).total_seconds()
        duration = max(0.0, min(int(bounds["wall_time_ms"]) / 1000, remaining_authority))
        expires = time.monotonic() + duration

        def remaining() -> float:
            return min(self._lock_timeout, max(0.0, expires - time.monotonic()))

        def owner(value: Doc) -> None:
            with journal_store.opened(self.path, self.journal_id, write=True, lock_timeout=remaining()) as journal:
                journal.update_call(key, call["id"], lambda item: item.update(owner=_copy(value)))

        def cleanup(cleaned: bool) -> None:
            delivery_runtime.retain_cleanup(self._metadata, call["id"], cleaned)

        def native_call() -> Doc:
            reading = method == "reconcile" and approval.get("kind") == "delivery-read-recovery"
            current = (
                validate_read_recovery(approval, pin=pin, now=self._clock())
                if reading
                else contracts.validate_approval(approval, pin=pin, now=self._clock())
            )
            actual_configuration = (
                current["new_configuration_digest"] if reading else intent["adapter"]["configuration_digest"]
            )
            store.require(
                adapter.configuration_digest == actual_configuration,
                "native adapter configuration changed before release",
            )
            store.require(
                adapter.operation_profile_digest == payload.get("operation_profile_digest"),
                "native operation profile changed before release",
            )
            current_payload = _copy(adapter.validate_payload(_copy(request), _copy(payload)))
            store.require(
                contracts.digest(current_payload) == request["native_payload_digest"], "retained native payload changed"
            )
            deadline = store.Deadline(max(0.0, expires - time.monotonic()))
            if method == "inspect":
                result = adapter.inspect(_copy(request), current_payload, deadline=deadline)
            elif method == "invoke":
                result = adapter.invoke(_copy(intent), current_payload, _copy(current), deadline=deadline)
            else:
                result = adapter.reconcile(_copy(intent), current_payload, deadline=deadline)
            # Stage validators are trusted but still run inside the same real whole-call
            # boundary as provider IO and preparation. No late validator may extend that call.
            try:
                validated_result = self._validate_result(adapter, method, request, payload, result)
                if method != "inspect":
                    # Stage proof and common subject identity are independent obligations. A
                    # trusted adapter bug must still become durable uncertainty after release.
                    self._receipt(intent, validated_result)
            except Exception:
                validated_result = self._boundary(method, intent, call["id"], result)
            return {"status": "returned", "result": validated_result}

        raw: Any = None
        try:
            bounded = controller_runtime.bounded_native_call(native_call, duration, on_owner=owner, on_cleanup=cleanup)
            store.require(bounded.get("status") == "returned", "native watchdog did not return a result")
            raw = bounded.get("result")
            result = journal_store._schema(
                raw,
                journal_store.INSPECTION if method == "inspect" else journal_store.RESULT,
                limit=journal_store.MAX_PROVENANCE_BYTES,
            )
        except Exception:
            result = self._boundary(method, intent, call["id"], raw)
        self._sync_cleanup(key)
        with journal_store.opened(self.path, self.journal_id, write=True, lock_timeout=self._lock_timeout) as journal:
            current = journal.read()["operations"][key]
            recorded = next(item for item in current["calls"] if item["id"] == call["id"])
            if recorded["cleanup"] is not True:
                result = self._boundary(method, intent, call["id"], raw)
            receipt = None if method == "inspect" else self._receipt(intent, result)
            retained = journal.finish_call(key, call["id"], result, receipt)
        return result, retained
