"""Durable bounded local verification coordination through native effect authorities.

The controller retains phase intent before handing an exact request to an independently selected
standing authorizer. Component journals decide whether effects occurred; only native completion
and subsequent native observation establish completed verification. One invocation-file ownership
lock serializes schedules while short journal transactions permit cancellation during an effect.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from exacly import (
    admission,
    completion,
    completion_store,
    controller_store,
    dispatch,
    dispatch_store,
    executions,
    executor,
    handoff,
    handoff_store,
    output_association,
    planning,
    reconciliation,
    remote_publication,
    standing_authorization,
    state_writer,
    worker_integration,
)
from exacly import execution_store as store
from exacly.applicability import Target
from exacly.controller_runtime import bounded_native_call as bounded_native_call
from exacly.core import is_state

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported deployment requires local POSIX locks.
    fcntl = None  # type: ignore[assignment]

Doc = dict[str, Any]
VERSION = "0.1.0"
COMPONENT_FIELDS = {
    "schema_version",
    "profile",
    "required_members",
    "controller",
    "configuration_path",
    "configuration_digest",
    "date",
    "state",
    "ledger",
    "ledger_anchor",
    "initial_binding_id",
    "executor_policy",
    "handoff_policy",
    "handoff",
    "remote",
    "association",
    "producer",
    "native_stores",
    "costs",
}
COMPONENT_OPTIONAL = {
    "authority_preflight",
    "baseline",
    "dispatch_policy",
    "control_bounds_ms",
    "negatives",
    "producer_timeout_ms",
    "publisher",
}
NEGATIVE_CASES = ("unavailable-producer", "wrong-revision", "wrong-attempt", "changed-criteria", "changed-verifier")
PHASES = ("dispatch", "execute", "handoff", "publish", "producer", "associate", "complete", "reobserve")
ADAPTERS = {
    name: f"native-{adapter}-v1"
    for name, adapter in (
        ("dispatch", "dispatch"),
        ("execute", "worker"),
        ("handoff", "handoff"),
        ("publish", "publication"),
        ("associate", "association"),
        ("complete", "completion"),
    )
}


class Authorizer(Protocol):
    def identity(self) -> Doc: ...
    def check_available(self) -> Doc: ...
    def execute(self, kind: str, request: Doc, *, effect_id: str) -> Doc: ...
    def reconcile(self, kind: str, request: Doc, *, effect_id: str) -> Doc: ...
    def call_metered(self, purpose: str, request: Doc, *, operation_id: str) -> Doc: ...
    def request_cancel(self, kind: str, request: Doc, *, effect_id: str) -> Doc: ...


class Workflow(Protocol):
    contract: Doc

    def assess(self, correlations: Doc) -> Doc: ...
    def proposal(self, phase: str, correlations: Doc) -> Doc: ...
    def accept(self, phase: str, request: Doc, receipt: Doc, correlations: Doc) -> Doc: ...
    def producer_request(self, correlations: Doc) -> Doc: ...
    def producer(self, correlations: Doc) -> Doc: ...
    def reobserve(self, correlations: Doc) -> Doc: ...
    def cancel(self, correlations: Doc) -> None: ...


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _timestamp(value: Any) -> dt.datetime:
    store.require(isinstance(value, str), "workflow timestamps require timezone-qualified UTC text")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise store.StoreError("invalid workflow timestamp") from None
    store.require(parsed.utcoffset() is not None, "workflow timestamps require a timezone")
    return parsed.astimezone(dt.UTC)


def host_identity(*, run_id: str, job_id: str) -> Doc:
    """Pin the actual boot and trusted coordinator run/job; a new runner cannot resume this scope."""
    store.require(bool(run_id) and bool(job_id), "explicit trusted coordinator run and job identity are required")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return {"boot_id": boot, "run_id": run_id, "job_id": job_id}


def validate_contract(value: Any) -> Doc:
    expected = {
        "schema_version",
        "scope",
        "objective_id",
        "original_goal",
        "input_target",
        "goal",
        "contract",
        "stores",
        "authorizer",
        "host",
        "deadline_at",
        "poll_seconds",
        "max_poll_seconds",
        "initial_binding_id",
        "plan_digest",
    }
    store.require(isinstance(value, dict) and set(value) == expected, "invalid workflow contract fields")
    selected = dict(json.loads(store.canonical(value)))
    store.require(
        selected["schema_version"] == VERSION and selected["scope"] == "local-machine",
        "only versioned local-machine workflow coordination is supported",
    )
    for key in ("objective_id", "original_goal"):
        store.require(
            isinstance(selected[key], str) and store.DIGEST.fullmatch(selected[key]), "invalid workflow objective"
        )
    store.require(
        selected["initial_binding_id"] is None
        or (isinstance(selected["initial_binding_id"], str) and store.DIGEST.fullmatch(selected["initial_binding_id"])),
        "invalid original workflow binding",
    )
    store.require(
        isinstance(selected["plan_digest"], str) and store.DIGEST.fullmatch(selected["plan_digest"]),
        "invalid original workflow plan identity",
    )
    reconciliation.source_target(Target(selected["input_target"]))
    store.require(
        set(selected["goal"]) == {"record", "dimension"} and selected["goal"]["dimension"] == "verification",
        "workflow requires one selected verification goal",
    )
    store.require(
        set(selected["contract"]) == {"model", "criteria", "bindings", "readiness", "trust", "observer"}
        and all(isinstance(item, str) and store.DIGEST.fullmatch(item) for item in selected["contract"].values()),
        "invalid pinned workflow contracts",
    )
    store.require(
        isinstance(selected["stores"], dict) and selected["stores"], "workflow requires canonical component stores"
    )
    for entry in selected["stores"].values():
        store.require(
            isinstance(entry, dict) and set(entry) == {"path", "id"} and isinstance(entry["id"], str) and entry["id"],
            "invalid workflow component store identity",
        )
        path = Path(entry["path"])
        store.require(
            path.is_absolute() and str(path) == entry["path"] and path.resolve() == path,
            "workflow component stores must be canonical absolute paths",
        )
    store.require(
        isinstance(selected["host"], dict)
        and set(selected["host"]) == {"boot_id", "run_id", "job_id"}
        and all(isinstance(item, str) and item for item in selected["host"].values()),
        "invalid workflow host identity",
    )
    _timestamp(selected["deadline_at"])
    store.require(
        all(
            type(selected[key]) in (int, float) and 0 < selected[key] <= 300
            for key in ("poll_seconds", "max_poll_seconds")
        )
        and selected["poll_seconds"] <= selected["max_poll_seconds"],
        "workflow polling must be finite and bounded",
    )
    authority = selected["authorizer"]
    store.require(
        isinstance(authority, dict)
        and authority.get("objective") == {"id": selected["objective_id"], "original_goal": selected["original_goal"]},
        "workflow objective differs from independently selected authority",
    )
    return selected


def initialize(journal: Path, contract: Doc, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    selected = validate_contract(contract)
    absolute = store.location(journal)
    own_store = selected["stores"].get("controller")
    if own_store is not None:
        store.require(
            own_store["path"] == str(absolute), "controller path differs from its approved coordination identity"
        )
    for name, entry in selected["stores"].items():
        if name == "controller":
            continue
        # Holding invocation ownership never holds any component's directory flock.
        store.require(
            absolute.parent != Path(entry["path"]).parent,
            "controller journal requires a separate coordination directory",
        )
    initial_binding = selected["initial_binding_id"]
    event: Doc = {
        "status": "active",
        "phase": "execute" if initial_binding else "dispatch",
        "pending": None,
        "correlations": {"binding_id": initial_binding, "original_binding_id": initial_binding}
        if initial_binding
        else {},
        "reason": "Initialized bounded verification workflow.",
        "next_at": None,
        "poll_count": 0,
    }
    result = controller_store.initialize(
        journal,
        selected,
        event,
        lock_timeout=lock_timeout,
        journal_id=own_store["id"] if own_store is not None else None,
    )
    # Ownership file is initialized explicitly, alongside durable workflow identity.
    ownership = Path(str(absolute) + ".invocation")
    descriptor = os.open(ownership, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, (result["journal_id"] + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    with store.locked(absolute, write=True, lock_timeout=lock_timeout) as (_, parent_fd):
        os.fsync(parent_fd)
    return result


def show(journal: Path, journal_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    with controller_store.opened(journal, journal_id, write=False, lock_timeout=lock_timeout) as retained:
        return retained.read()


@contextmanager
def _invocation(journal: Path, journal_id: str) -> Iterator[bool]:
    store.require(fcntl is not None, "controller ownership requires POSIX local locks")
    absolute = store.location(journal)
    path = Path(str(absolute) + ".invocation")
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = store.file_info(descriptor)
        store.require(
            os.pread(descriptor, 128, 0) == (journal_id + "\n").encode(), "controller invocation identity changed"
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        current = path.lstat()
        store.require(
            (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), "controller ownership file changed"
        )
        yield True
    finally:
        os.close(descriptor)


def _append(journal: Path, journal_id: str, event: Doc) -> Doc:
    with controller_store.opened(journal, journal_id, write=True) as retained:
        # Cancellation/pause may have been requested while a native effect was in flight.
        latest = retained.read()["event"]
        event = {**event, "supervisors": latest.get("supervisors", [])}
        if (
            isinstance(event.get("pending"), dict)
            and isinstance(latest.get("pending"), dict)
            and event["pending"]["effect_id"] == latest["pending"]["effect_id"]
        ):
            event = {**event, "pending": {**event["pending"], **latest["pending"]}}
        if latest["status"] in {"cancelled", "paused"} and event["status"] not in {"cancelled", "paused"}:
            event = {**event, "status": latest["status"], "reason": latest["reason"]}
        return retained.append(event)


def _release(journal: Path, journal_id: str, event: Doc) -> Doc:
    """Linearize release of only the retained exact delegated request against cancellation."""
    with controller_store.opened(journal, journal_id, write=True) as retained:
        latest = retained.read()["event"]
        if latest["status"] in {"paused", "cancelled"}:
            return retained.read()
        store.require(latest["pending"] == event["pending"], "pending controller request changed before release")
        return retained.append(
            {
                **latest,
                "pending": {**latest["pending"], "released": True},
                "reason": "Exact delegated request released; outcome remains unestablished.",
            }
        )


def _waiting(event: Doc, contract: Doc, current: dt.datetime, reason: str) -> Doc:
    count = event["poll_count"] + 1
    interval = min(contract["max_poll_seconds"], contract["poll_seconds"] * 2 ** min(count - 1, 20))
    return {
        **event,
        "status": "recovering" if event["pending"] else "waiting",
        "poll_count": count,
        "next_at": min(current + dt.timedelta(seconds=interval), _timestamp(contract["deadline_at"])).isoformat(),
        "reason": reason,
    }


def pause(journal: Path, journal_id: str, *, reason: str) -> Doc:
    store.require(isinstance(reason, str) and reason.strip(), "pause requires an actionable reason")
    with controller_store.opened(journal, journal_id, write=True) as retained:
        event = retained.read()["event"]
        store.require(
            event["status"] not in {"completed", "superseded", "cancelled"}, "terminal workflow cannot be paused"
        )
        return retained.append({**event, "status": "paused", "reason": reason})


def cancel(
    journal: Path,
    journal_id: str,
    *,
    reason: str,
    workflow: Workflow | None = None,
    authority: Authorizer | None = None,
) -> Doc:
    store.require(isinstance(reason, str) and reason.strip(), "cancellation requires a reason")
    with controller_store.opened(journal, journal_id, write=True) as retained:
        event = retained.read()["event"]
        store.require(event["status"] not in {"completed", "superseded"}, "completed workflow cannot be cancelled")
        result = retained.append({**event, "status": "cancelled", "reason": reason})
    if workflow is not None:
        store.require(workflow.contract == result["identity"]["contract"], "cancellation runtime contract changed")
    pending = result["event"]["pending"]
    if authority is not None and pending is not None:
        store.require(
            authority.identity() == result["identity"]["contract"]["authorizer"],
            "cancellation authorizer identity changed",
        )
        requested = authority.request_cancel(pending["kind"], pending["request"], effect_id=pending["effect_id"])
        result = _append(journal, journal_id, {**result["event"], "cancellation": requested})
    return result


def resume(journal: Path, journal_id: str) -> Doc:
    """Explicitly resume a paused contract; changed contracts still require independent replanning."""
    with controller_store.opened(journal, journal_id, write=True) as retained:
        event = retained.read()["event"]
        store.require(event["status"] == "paused", "only paused workflow can resume")
        return retained.append(
            {
                **event,
                "status": "recovering" if event["pending"] else "active",
                "next_at": None,
                "reason": "Explicit resume; current authority will be rechecked.",
            }
        )


def step(
    journal: Path,
    journal_id: str,
    *,
    workflow: Workflow,
    authority: Authorizer,
    clock: Callable[[], dt.datetime] = now,
    recovery_only: bool = False,
) -> Doc:
    """Advance at most one native effect; duplicate schedules share one invocation and durable intent."""
    with _invocation(journal, journal_id) as owned:
        if not owned:
            return {"status": "owned", "reason": "Another invocation owns this workflow; no new effect started."}
        retained = show(journal, journal_id)
        contract, event = retained["identity"]["contract"], retained["event"]
        store.require(
            validate_contract(workflow.contract) == contract and authority.identity() == contract["authorizer"],
            "workflow or independently selected authorizer contract changed",
        )
        store.require(
            Path("/proc/sys/kernel/random/boot_id").read_text().strip() == contract["host"]["boot_id"],
            "workflow belongs to another coordinator boot; new-runner recovery is unsupported",
        )
        if event["status"] in {"completed", "superseded"} or (
            event["status"] in {"paused", "cancelled"} and (not recovery_only or event["pending"] is None)
        ):
            return retained
        current = clock().astimezone(dt.UTC)
        if not recovery_only and event["next_at"] is not None and current < _timestamp(event["next_at"]):
            return retained
        if current >= _timestamp(contract["deadline_at"]) and event["pending"] is None:
            return _append(
                journal,
                journal_id,
                {
                    **event,
                    "status": "paused",
                    "reason": "Workflow deadline exhausted; inspect retained native history.",
                },
            )
        if event["phase"] == "reobserve":
            try:
                observed = workflow.reobserve(event["correlations"])
            except (ValueError, OSError) as error:
                return _append(
                    journal,
                    journal_id,
                    {**event, "status": "paused", "reason": f"Native reobservation unavailable: {error}"[:1000]},
                )
            status = "completed" if observed["status"] == "satisfied" else "paused"
            return _append(
                journal, journal_id, {**event, "status": status, "reason": observed["reason"], "observation": observed}
            )
        if event["pending"] is None:
            if recovery_only:
                return retained
            available = authority.check_available()
            if not available["available"]:
                return _append(journal, journal_id, {**event, "status": "paused", "reason": available["reason"]})
            try:
                assessed = workflow.assess(event["correlations"])
            except (ValueError, OSError) as error:
                return _append(
                    journal,
                    journal_id,
                    {**event, "status": "paused", "reason": f"Native applicability unavailable: {error}"[:1000]},
                )
            event = {**event, "assessment": assessed}
            if assessed["status"] != "eligible":
                return _append(
                    journal,
                    journal_id,
                    {
                        **event,
                        "status": "superseded" if assessed["status"] == "satisfied" else "paused",
                        "reason": assessed["reason"],
                    },
                )
            if event["phase"] == "producer":
                observation = event.get("observation")
                if observation is None:
                    request = workflow.producer_request(event["correlations"])
                    observation = {
                        "request": request,
                        "operation_id": planning.digest(
                            {
                                "workflow": retained["identity"]["workflow_id"],
                                "head": retained["head"],
                                "request": request,
                            }
                        ),
                    }
                    recorded = _append(
                        journal,
                        journal_id,
                        {
                            **event,
                            "observation": observation,
                            "reason": "Exact producer read intent retained before objective-wide metering.",
                        },
                    )
                    if recorded["event"]["status"] in {"paused", "cancelled"}:
                        return recorded
                    event = recorded["event"]
                current = clock().astimezone(dt.UTC)
                if current >= _timestamp(contract["deadline_at"]):
                    return _append(
                        journal,
                        journal_id,
                        {
                            **event,
                            "status": "paused",
                            "reason": "Workflow deadline exhausted before metered producer release.",
                        },
                    )
                metered = authority.call_metered(
                    "producer-observe", observation["request"], operation_id=observation["operation_id"]
                )
                current = clock().astimezone(dt.UTC)
                if metered["status"] != "completed":
                    updated = {**event, "observation": None, "last_observation": metered}
                    if metered["status"] == "uncertain" and current < _timestamp(contract["deadline_at"]):
                        return _append(journal, journal_id, _waiting(updated, contract, current, metered["reason"]))
                    return _append(journal, journal_id, {**updated, "status": "paused", "reason": metered["reason"]})
                observed = metered["receipt"]["result"]
                event = {**event, "observation": None, "last_observation": metered}
                correlations = {**event["correlations"], **observed.get("correlations", {})}
                updated = {**event, "correlations": correlations}
                if observed["status"] == "succeeded":
                    return _append(
                        journal,
                        journal_id,
                        {
                            **updated,
                            "phase": "associate",
                            "status": "active",
                            "next_at": None,
                            "poll_count": 0,
                            "reason": observed["reason"],
                        },
                    )
                if observed["status"] == "waiting":
                    return _append(journal, journal_id, _waiting(updated, contract, current, observed["reason"]))
                return _append(journal, journal_id, {**updated, "status": "paused", "reason": observed["reason"]})
            try:
                proposal = workflow.proposal(event["phase"], event["correlations"])
            except (ValueError, OSError) as error:
                return _append(
                    journal,
                    journal_id,
                    {**event, "status": "paused", "reason": f"Native phase proposal unavailable: {error}"[:1000]},
                )
            proposal = {
                **proposal,
                "controller": {
                    "journal": str(journal),
                    "journal_id": journal_id,
                    "workflow_id": retained["identity"]["workflow_id"],
                },
            }
            effect_id = planning.digest(
                {"workflow": retained["identity"]["workflow_id"], "phase": event["phase"], "proposal": proposal}
            )
            event = {
                **event,
                "status": "active",
                "pending": {"kind": event["phase"], "request": proposal, "effect_id": effect_id},
                "reason": "Exact phase intent retained before requesting delegated native authority.",
            }
            recorded = _append(journal, journal_id, event)
            if recorded["event"]["status"] in {"paused", "cancelled"}:
                return recorded
            released = _release(journal, journal_id, event)
            if released["event"]["status"] in {"paused", "cancelled"}:
                return released
            event = released["event"]
            current = clock().astimezone(dt.UTC)
            if current >= _timestamp(contract["deadline_at"]):
                return _append(
                    journal,
                    journal_id,
                    {**event, "status": "paused", "reason": "Workflow deadline exhausted before delegated invocation."},
                )
            result = authority.execute(event["phase"], proposal, effect_id=effect_id)
        else:
            pending = event["pending"]
            result = authority.reconcile(pending["kind"], pending["request"], effect_id=pending["effect_id"])
            current = clock().astimezone(dt.UTC)
            if result["status"] in {"not_started", "recoverable"}:
                if recovery_only:
                    return _append(
                        journal,
                        journal_id,
                        {
                            **event,
                            "last_authorization": result,
                            "reason": "Native unsent/prepared phase retained; explicit current authority is required.",
                        },
                    )
                latest = show(journal, journal_id)["event"]
                if latest["status"] in {"paused", "cancelled"}:
                    return show(journal, journal_id)
                available, assessed = authority.check_available(), workflow.assess(event["correlations"])
                current = clock().astimezone(dt.UTC)
                if (
                    current >= _timestamp(contract["deadline_at"])
                    or not available["available"]
                    or assessed["status"] != "eligible"
                ):
                    return _append(
                        journal,
                        journal_id,
                        {
                            **event,
                            "status": "paused",
                            "reason": "Unsent phase retained; applicability, deadline or authority blocks execution.",
                        },
                    )
                released = _release(journal, journal_id, event)
                if released["event"]["status"] in {"paused", "cancelled"}:
                    return released
                event = released["event"]
                current = clock().astimezone(dt.UTC)
                if current >= _timestamp(contract["deadline_at"]):
                    return _append(
                        journal,
                        journal_id,
                        {
                            **event,
                            "status": "paused",
                            "reason": "Workflow deadline exhausted before recovered invocation release.",
                        },
                    )
                result = authority.execute(pending["kind"], pending["request"], effect_id=pending["effect_id"])
        current = clock().astimezone(dt.UTC)
        pending = event["pending"]
        if result["status"] == "completed":
            correlations = workflow.accept(event["phase"], pending["request"], result["receipt"], event["correlations"])
            correlations = {
                **correlations,
                "authorizations": {**correlations.get("authorizations", {}), event["phase"]: result["reservation"]},
            }
            next_phase = PHASES[PHASES.index(event["phase"]) + 1]
            return _append(
                journal,
                journal_id,
                {
                    **event,
                    "status": "active",
                    "phase": next_phase,
                    "pending": None,
                    "correlations": correlations,
                    "next_at": None,
                    "poll_count": 0,
                    "reason": result["reason"],
                },
            )
        if result["status"] == "uncertain":
            if current >= _timestamp(contract["deadline_at"]):
                return _append(
                    journal,
                    journal_id,
                    {
                        **event,
                        "status": "paused",
                        "last_authorization": result,
                        "reason": "Deadline exhausted; preserve uncertain reservation and recover explicitly.",
                    },
                )
            return _append(
                journal,
                journal_id,
                _waiting({**event, "last_authorization": result}, contract, current, result["reason"]),
            )
        return _append(
            journal, journal_id, {**event, "status": "paused", "last_authorization": result, "reason": result["reason"]}
        )


@dataclass(frozen=True)
class NativeServices:
    """Trusted operator-selected objects. No policy value imports or selects a provider."""

    source: admission.Source
    transport: remote_publication.RemoteTransport
    prepared: worker_integration.PreparedWorker
    guard: Any
    selector: Any
    dispatch_client: dispatch.IssueAdapter | None = None
    boundary: Callable[[Callable[[], Doc], float], Doc] = bounded_native_call
    clock: Callable[[], dt.datetime] = now


def validate_components(value: Doc) -> Doc:
    store.require(
        isinstance(value, dict)
        and set(value) >= COMPONENT_FIELDS
        and set(value) <= COMPONENT_FIELDS | COMPONENT_OPTIONAL,
        "unknown or missing native component fields; composition and additional targets are unsupported",
    )
    selected = dict(json.loads(store.canonical(value)))
    store.require(
        selected["schema_version"] == VERSION
        and selected["profile"] == "single-producer-local-v1"
        and selected["required_members"] == ["producer"],
        "first native profile supports exactly one required producer in one local coordination domain",
    )
    publisher = selected.get("publisher")
    if publisher is not None:
        expected = {
            "environment",
            "present",
            "scope_verified",
            "app_id",
            "installation_id",
            "expires_at",
            "issuer_evidence",
            "issuer_server_verified",
        }
        store.require(
            isinstance(publisher, dict)
            and set(publisher) == expected
            and publisher["environment"] == "EXACLY_REQ039_PUBLISH_TOKEN"
            and type(publisher["present"]) is bool
            and type(publisher["scope_verified"]) is bool
            and publisher["issuer_evidence"] == "trusted-workflow-mint-metadata"
            and publisher["issuer_server_verified"] is False,
            "publisher descriptor requires its closed approved mint identity and credential selector",
        )
        for field in ("app_id", "installation_id"):
            store.require(
                publisher[field] is None or (type(publisher[field]) is int and publisher[field] > 0),
                "publisher IDs must be explicit positive integers or unavailable",
            )
        store.require(
            publisher["app_id"] != 15368, "GitHub Actions job-token identity cannot publish producer-triggering changes"
        )
        if publisher["expires_at"] is not None:
            _timestamp(publisher["expires_at"])
    preflight = selected.get("authority_preflight")
    if preflight is not None:
        store.require(
            isinstance(preflight, dict)
            and set(preflight)
            == {
                "schema_version",
                "adapter_id",
                "repository",
                "coordinator",
                "producer_policy_digest",
                "resource_guard_digest",
                "publisher",
                "approval_channel",
                "wall_time_ms",
                "max_producer_reads",
            }
            and preflight["schema_version"] == VERSION
            and preflight["adapter_id"] == "req039-authority-preflight-v1"
            and publisher is not None
            and preflight["publisher"] == publisher
            and type(preflight["wall_time_ms"]) is int
            and preflight["wall_time_ms"] == 45000
            and type(preflight["max_producer_reads"]) is int
            and preflight["max_producer_reads"] == 20,
            "authority preflight requires its closed finite approved runtime contract",
        )
        repository, coordinator, channel = (
            preflight["repository"],
            preflight["coordinator"],
            preflight["approval_channel"],
        )
        store.require(
            isinstance(repository, dict)
            and set(repository) == {"id", "full_name"}
            and type(repository["id"]) is int
            and repository["id"] > 0
            and isinstance(repository["full_name"], str)
            and len(repository["full_name"].split("/")) == 2
            and isinstance(coordinator, dict)
            and set(coordinator)
            == {
                "run_id",
                "run_attempt",
                "job_id",
                "boot_id",
                "nonce",
                "workflow_id",
                "revision",
            }
            and all(
                isinstance(coordinator[key], str) and coordinator[key]
                for key in ("run_id", "run_attempt", "job_id", "boot_id", "nonce", "revision")
            )
            and type(coordinator["workflow_id"]) is int
            and coordinator["workflow_id"] > 0
            and len(coordinator["nonce"]) == 32
            and all(char in "0123456789abcdef" for char in coordinator["nonce"])
            and len(coordinator["revision"]) == 40
            and all(char in "0123456789abcdef" for char in coordinator["revision"])
            and isinstance(channel, dict)
            and set(channel) == {"repository", "issue", "user_id"}
            and channel["repository"] == repository["full_name"]
            and type(channel["issue"]) is int
            and channel["issue"] > 0
            and type(channel["user_id"]) is int
            and channel["user_id"] > 0
            and all(
                isinstance(preflight[key], str) and store.DIGEST.fullmatch(preflight[key])
                for key in ("producer_policy_digest", "resource_guard_digest")
            ),
            "authority preflight identity, approval channel or producer/resource pin is invalid",
        )
    negative = selected.get("negatives")
    if negative is not None:
        store.require(
            isinstance(negative, dict)
            and set(negative)
            == {
                "schema_version",
                "producer_contract_digest",
                "target",
                "plan_digest",
                "action_id",
                "readiness_digest",
                "trust_digest",
                "cases",
                "unavailable_run_id",
                "wrong_attempt",
                "criteria_model_digest",
                "verifier_digest",
                "wall_time_ms",
            }
            and negative["schema_version"] == VERSION
            and negative["cases"] == list(NEGATIVE_CASES)
            and type(negative["unavailable_run_id"]) is int
            and negative["unavailable_run_id"] > 0
            and type(negative["wrong_attempt"]) is int
            and negative["wrong_attempt"] == 2
            and type(negative["wall_time_ms"]) is int
            and 0 < negative["wall_time_ms"] <= 300000
            and all(
                isinstance(negative[key], str) and store.DIGEST.fullmatch(negative[key])
                for key in (
                    "producer_contract_digest",
                    "plan_digest",
                    "action_id",
                    "readiness_digest",
                    "trust_digest",
                    "criteria_model_digest",
                    "verifier_digest",
                )
            ),
            "negative admission requires its closed approved cases, mismatching expectations and bounded read contract",
        )
        reconciliation.source_target(Target(negative["target"]))
    return selected


class NativeWorkflow:
    """Concrete one-producer path through the installed native verification APIs.

    Components are fixed operator configuration containing executor/handoff policies, exact local
    handoff metadata, remote request fields, initialized association storage and producer contract.
    The approved plan remains the original input even after output association. Trusted services
    supply physical execution, transport and live evidence; candidate data selects none of them.
    """

    def __init__(
        self,
        *,
        contract: Doc,
        documents: Mapping[str, Any],
        plan: Doc,
        readiness_policy: Doc,
        trust_policy: Doc,
        components: Doc,
        services: NativeServices,
    ) -> None:
        self.contract = validate_contract(contract)
        self.documents = planning.plain(dict(documents))
        self.plan = planning.plain(plan)
        self.readiness = planning.plain(readiness_policy)
        self.trust = admission.validate_policy(trust_policy)
        self.components = validate_components(planning.plain(components))
        self.services = services
        actions = [action for action in self.plan["actions"] if action["record"] == self.contract["goal"]["record"]]
        store.require(len(actions) == 1, "native workflow requires one selected original planned action")
        self.action = actions[0]
        self.date = dt.date.fromisoformat(components["date"])
        self.state_path = Path(components["state"])
        self.ledger = Path(components["ledger"])
        self._check_config()

    @property
    def configuration_digest(self) -> str:
        """Actual selected bytes; identity alone grants nothing without the independent delegation pin."""
        actual = planning.digest(
            {key: value for key, value in self.components.items() if key != "configuration_digest"}
        )
        store.require(
            actual == self.components["configuration_digest"] == self.contract["stores"]["configuration"]["id"],
            "actual native configuration differs from the pinned workflow",
        )
        config = {name: document for name, document in self.documents.items() if not is_state(document)}
        store.require(
            admission.config_digest(config) == self.contract["contract"]["model"]
            and planning.digest(self.readiness) == self.contract["contract"]["readiness"]
            and planning.digest(self.trust) == self.contract["contract"]["trust"]
            and self.trust["observer"]["digest"] == self.contract["contract"]["observer"]
            and self.plan["digest"] == self.contract["plan_digest"]
            and planning.digest({key: value for key, value in self.plan.items() if key != "digest"})
            == self.plan["digest"]
            and self.action["criteria_digest"] == self.contract["contract"]["criteria"]
            and self.action["bindings_digest"] == self.contract["contract"]["bindings"],
            "native configuration uses changed approved model, plan or policy snapshots",
        )
        return actual

    def _check_anchor(self) -> None:
        anchor = controller_store.check_ledger_anchor(self.components["ledger_anchor"])
        store.require(
            anchor["ledger"] == str(self.ledger)
            and self.components["native_stores"]["complete"] == {"path": str(self.ledger), "id": anchor["digest"]},
            "native ledger anchor differs from the approved canonical completion store",
        )

    def _check_config(self) -> None:
        validate_components(self.components)
        _ = self.configuration_digest
        store.require(
            self.components["initial_binding_id"] == self.contract["initial_binding_id"],
            "original registered-task selection differs from the approved controller contract",
        )
        requirements = self.readiness["requirements"]
        store.require(
            len(requirements) == 1
            and requirements[0]["dimensions"] == ["verification"]
            and requirements[0]["record"].removeprefix(self.plan["namespace"] + ":") == self.action["record"],
            "first native profile supports exactly one selected verification prerequisite, not aggregate mapping",
        )
        publisher = self.components.get("publisher")
        if publisher is not None:
            store.require(
                publisher["present"]
                and publisher["scope_verified"]
                and publisher["app_id"] is not None
                and publisher["installation_id"] is not None
                and publisher["expires_at"] is not None
                and self.services.clock().astimezone(dt.UTC) < _timestamp(publisher["expires_at"]),
                "approved publisher identity or credential lifetime is unavailable",
            )
        preflight = self.components.get("authority_preflight")
        if preflight is not None:
            store.require(
                preflight["repository"] == self.trust["repository"]
                and {key: preflight["coordinator"][key] for key in ("boot_id", "run_id", "job_id")}
                == self.contract["host"]
                and preflight["producer_policy_digest"] == planning.digest(self.trust)
                and preflight["resource_guard_digest"] == self.services.guard.digest,
                "runtime preflight changes the approved host, repository, producer or physical resource guard",
            )
        if "negatives" in self.components:
            NativeNegativeReader(self).inspect(
                {"contract": self.components["negatives"], "case": NEGATIVE_CASES[0], "correlations": {}}
            )
        self._check_anchor()
        model = reconciliation.approved_model(
            self.documents, self.plan, self.action, self.contract["input_target"], self.date
        )
        expected = {
            "model": self.plan["inputs"]["config"]["digest"],
            "criteria": self.action["criteria_digest"],
            "bindings": self.action["bindings_digest"],
            "readiness": planning.digest(self.readiness),
            "trust": planning.digest(self.trust),
            "observer": self.trust["observer"]["digest"],
        }
        store.require(
            expected == self.contract["contract"]
            and self.plan["target"] == self.contract["input_target"]
            and self.plan["digest"] == self.contract["plan_digest"],
            "native workflow approved model, criteria, policies or input target changed",
        )
        store.require(model.graph.namespace == self.plan["namespace"], "native model namespace changed")
        store.require(
            self.components["configuration_digest"]
            == planning.digest({key: value for key, value in self.components.items() if key != "configuration_digest"}),
            "native component configuration identity changed",
        )
        coordination = self.components["controller"]
        store.require(
            self.contract["stores"]["controller"]
            == {"path": coordination["journal"], "id": coordination["journal_id"]},
            "native workflow selects another canonical controller store",
        )
        store.require(
            self.components["configuration_digest"] == self.contract["stores"]["configuration"]["id"],
            "native components differ from approved workflow configuration",
        )
        store.require(
            str(self.ledger) == self.contract["stores"]["ledger"]["path"]
            and str(self.state_path) == self.contract["stores"]["state"]["path"],
            "native current state or binding ledger changed",
        )
        for kind, entry in self.components["native_stores"].items():
            if kind in {"dispatch", "execute", "handoff"}:
                policy_name = {
                    "dispatch": "dispatch_policy",
                    "execute": "executor_policy",
                    "handoff": "handoff_policy",
                }[kind]
                selected_storage = self.components[policy_name]["storage"]
                actual = {"path": selected_storage["journal"], "id": selected_storage["journal_id"]}
            elif kind == "publish":
                actual = {"path": self.components["remote"]["journal"], "id": self.components["remote"]["journal_id"]}
            elif kind == "associate":
                actual = {
                    "path": self.components["association"]["journal"],
                    "id": self.components["association"]["journal_id"],
                }
            elif kind == "complete":
                actual = {"path": str(self.ledger), "id": self.components["ledger_anchor"]["digest"]}
            else:
                raise store.StoreError("unsupported native component scope")
            store.require(
                entry == actual and entry == self.contract["stores"][kind],
                "native effect store differs from actual approved component storage",
            )
        store.require(
            self.components["remote"]["ref"] == "refs/heads/" + self.trust["branch"],
            "published destination branch differs from the approved producer push branch",
        )
        store.require(
            str(self.trust["repository"]["id"]) == self.components["remote"]["repository_id"],
            "native remote publication repository differs from the approved producer repository",
        )
        if "dispatch_policy" in self.components:
            store.require(
                self.components["dispatch_policy"]["repository"] == self.trust["repository"],
                "native dispatch repository differs from this approved single-consumer contract",
            )
        store.require(
            self.components["producer"]["policy_digest"] == planning.digest(self.trust),
            "producer selection and completion must use the same independently approved trust policy",
        )
        store.require(
            self.services.prepared.identity["model_digest"] == self.trust["model"]["digest"],
            "prepared worker model differs from approved verifier model",
        )

    def effect_registry(self) -> dict[str, NativeEffect]:
        return {kind: NativeEffect(self, kind) for kind in ADAPTERS}

    def _read_permitted(self) -> None:
        scope = self.components["controller"]
        event = show(Path(scope["journal"]), scope["journal_id"])["event"]
        store.require(
            event["status"] not in {"paused", "cancelled", "completed", "superseded"},
            "controller no longer permits new observation requests",
        )

    def _state(self) -> Doc:
        snapshot = state_writer.read(self.state_path)
        store.require(snapshot.document is not None, "native workflow current state is absent")
        assert snapshot.document is not None
        return snapshot.document

    def assess(self, correlations: Doc) -> Doc:
        self._check_config()
        state = self._state()
        target = Target(correlations["target"] if "associate" in correlations else self.contract["input_target"])
        model = reconciliation.approved_model(self.documents, self.plan, self.action, target.data, self.date)
        current = {**self.documents, "__current_state__": state}
        fresh = planning.create(current, self.readiness, target, self.date)
        check = [item for item in fresh["checks"] if item["record"] == self.action["record"]]
        store.require(len(check) == 1 and not model.report.errors, "native goal is no longer uniquely applicable")
        classification = check[0]["classification"]
        selection = {"classification": classification, "plan_digest": fresh["digest"]}
        if classification == "satisfied":
            return {
                **selection,
                "status": "satisfied",
                "reason": "Fresh native assessment already satisfies this exact goal.",
            }
        if classification in {"blocked_configuration", "blocked_model"} or fresh["status"] == "blocked":
            return {
                **selection,
                "status": "blocked",
                "reason": "Native planner requires approved configuration or model escalation.",
            }
        store.require(
            classification in {"collect_evidence", "investigate_failure"},
            "native planner selected an unsupported work classification",
        )
        if classification != self.action["kind"]:
            return {
                **selection,
                "status": "blocked",
                "reason": "Native planner changed the selected work to "
                + classification
                + "; review a new plan and recipe before execution. "
                "Missing evidence does not establish missing implementation.",
            }
        binding = correlations.get("binding_id", self.components.get("initial_binding_id"))
        if binding:
            matched, reason, _ = executions.match(
                self.ledger,
                binding,
                plan_doc=self.plan,
                action_id=self.action["id"],
                state_doc=state,
                readiness_policy=self.readiness,
                trust_policy=self.trust,
                output_target=target,
                date=self.date,
            )
            if not matched:
                return {"status": "blocked", "reason": reason}
        reason = check[0]["reason"]
        if classification == "collect_evidence":
            reason += (
                " The approved evidence recipe collects verification; "
                "missing evidence does not establish missing implementation."
            )
        return {**selection, "status": "eligible", "reason": reason}

    def _binding(self, correlations: Doc) -> str:
        value = correlations.get("binding_id", self.components.get("initial_binding_id"))
        store.require(
            isinstance(value, str) and store.DIGEST.fullmatch(value), "native workflow has no registered binding"
        )
        assert isinstance(value, str)
        return value

    def _native_request(self, phase: str, correlations: Doc) -> Doc:
        common = {
            "plan": self.plan,
            "action_id": self.action["id"],
            "readiness_policy": self.readiness,
            "trust_policy": self.trust,
            "target": self.contract["input_target"],
            "date": self.date.isoformat(),
        }
        if phase == "dispatch":
            return {**common, "dispatch_policy": self.components["dispatch_policy"]}
        if phase == "execute":
            return {
                **common,
                "executor_policy": self.components["executor_policy"],
                "binding_id": self._binding(correlations),
                "inputs": {
                    key: base64.b64encode(value).decode() for key, value in self.services.prepared.inputs.items()
                },
                "worker_identity": self.services.prepared.identity,
                "resource_guard": self.services.guard.identity,
            }
        if phase == "handoff":
            execution = correlations["execute"]
            return {
                "schema_version": VERSION,
                "policy": self.components["handoff_policy"],
                "execution_id": execution["execution_id"],
                "attempt": execution["attempt"],
                "base_revision": self.contract["input_target"]["revision"],
                **self.components["handoff"],
            }
        if phase == "publish":
            return {**self.components["remote"], "handoff_id": correlations["handoff"]["handoff_id"]}
        if phase == "associate":
            receipt = correlations["handoff"]
            return {
                "storage": self.components["association"],
                "handoff_id": receipt["handoff_id"],
                "handoff_receipt_digest": receipt["digest"],
                "binding_id": self._binding(correlations),
            }
        if phase == "complete":
            selection = correlations["producer_selection"]
            return {
                "plan": self.plan,
                "action_id": self.action["id"],
                "readiness_policy": self.readiness,
                "trust_policy": self.trust,
                "output_target": correlations["target"],
                "run_id": selection["run_id"],
                "attempt": selection["attempt"],
                "date": self.date.isoformat(),
                "ledger": str(self.ledger),
                "binding_id": self._binding(correlations),
                "state_path": str(self.state_path),
            }
        raise store.StoreError("unsupported native verification phase")

    def _dispatch(self, document: Doc) -> dispatch.Request:
        return dispatch.Request(
            document["plan"],
            document["action_id"],
            document["readiness_policy"],
            document["trust_policy"],
            document["dispatch_policy"],
            Target(document["target"]),
            self.date,
        )

    def _executor(self, document: Doc) -> executor.Request:
        return executor.Request(
            document["plan"],
            document["action_id"],
            document["readiness_policy"],
            document["trust_policy"],
            document["executor_policy"],
            Target(document["target"]),
            self.date,
            document["binding_id"],
            {key: base64.b64decode(value, validate=True) for key, value in document["inputs"].items()},
        )

    def _remote(self, document: Doc) -> remote_publication.Request:
        return remote_publication.request_from_document(document)

    def _association(self, document: Doc) -> output_association.Request:
        return output_association.Request(
            document["storage"],
            document["handoff_id"],
            document["handoff_receipt_digest"],
            document["binding_id"],
            self.documents,
            self.plan,
            self.action["id"],
            self.readiness,
            self.trust,
            self.date,
        )

    def _grant(self, phase: str, document: Doc) -> Doc:
        if phase == "dispatch":
            report = dispatch.preview(self.documents, self.state_path, self._dispatch(document))
        elif phase == "execute":
            preview = worker_integration.preview
            report = preview(
                self.documents,
                self.state_path,
                self._executor(document),
                prepared=self.services.prepared,
                guard=self.services.guard,
            )
        elif phase == "handoff":
            report = handoff.preview(handoff.request_from_document(document))
        elif phase == "publish":
            report = remote_publication.preview(self._remote(document))
        elif phase == "associate":
            return {"association_id": output_association.identity(self._association(document))}
        elif phase == "complete":
            native_request = completion._inputs(
                document["plan"],
                document["action_id"],
                document["readiness_policy"],
                document["trust_policy"],
                Target(document["output_target"]),
                document["run_id"],
                document["attempt"],
                self.date,
                document["binding_id"],
                Path(document["state_path"]),
            )[-1]
            return {"invocation": "native-completion-v1", "request_digest": planning.digest(native_request)}
        else:
            raise store.StoreError("unsupported native grant")
        store.require(
            report["status"] == "ready" and isinstance(report["authorization_template"], dict),
            "native phase has no fresh exact effect authorization; inspect and recover its component journal",
        )
        return dict(report["authorization_template"])

    def proposal(self, phase: str, correlations: Doc) -> Doc:
        document = self._native_request(phase, correlations)
        return {
            "phase": phase,
            "correlations": copy.deepcopy(correlations),
            "native_request": document,
            "authorization": self._grant(phase, document),
            "controller": {**self.components["controller"], "workflow_id": planning.digest(self.contract)},
        }

    def accept(self, phase: str, request: Doc, receipt: Doc, correlations: Doc) -> Doc:
        # Re-read the native result; an authorizer response or worker report alone is insufficient.
        observed = self.effect_registry()[phase]._recover(request, request["authorization"], terminal_only=True)
        store.require(
            observed["status"] == "succeeded" and observed["receipt"] == receipt,
            "native retained receipt differs from the controller phase outcome",
        )
        result = {**correlations, phase: copy.deepcopy(receipt)}
        if phase == "dispatch":
            result["binding_id"] = result["original_binding_id"] = receipt["binding_id"]
        elif phase == "handoff":
            result["target"] = receipt["target"]
            # Until association the active native binding still selects the original input.
        elif phase == "associate":
            result["binding_id"] = receipt["binding"]
        return result

    def current_publication(self, correlations: Doc) -> Doc:
        selected = self.components["native_stores"]["publish"]
        publication = remote_publication.lookup(
            Path(selected["path"]), selected["id"], correlations["publish"]["operation_id"]
        )
        store.require(
            publication is not None and publication == correlations["publish"] and publication["status"] == "published",
            "native publication provenance is missing or differs",
        )
        assert publication is not None
        observed = self.services.transport.inspect(publication["intent"], timeout=30)
        store.require(
            observed.get("repository_id") == self.components["remote"]["repository_id"]
            and observed.get("ref") == self.components["remote"]["ref"]
            and observed.get("revision") == correlations["handoff"]["commit"]
            and observed.get("operation_id") == publication["operation_id"]
            and observed.get("commit_present") is True,
            "remote destination drifted from the exact approved publication; preserve it and replan",
        )
        return observed

    def reader_registry(self) -> dict[str, Any]:
        readers: dict[str, Any] = {"producer-observe": NativeProducerReader(self)}
        if "baseline" in self.components:
            readers["baseline-admission"] = NativeBaselineReader(self)
        if "negatives" in self.components:
            readers["negative-admission"] = NativeNegativeReader(self)
        return readers

    def negative_request(self, correlations: Doc, *, case: str) -> Doc:
        selected = self.components["negatives"]
        store.require(case in selected["cases"], "negative admission case lacks explicit approval")
        return {"contract": copy.deepcopy(selected), "case": case, "correlations": copy.deepcopy(correlations)}

    def baseline_request(self, correlations: Doc, *, run_id: int, attempt: int = 1) -> Doc:
        selected = self.components["baseline"]
        store.require(
            selected["run_id"] == run_id and selected["attempt"] == attempt,
            "baseline admission requires its exact independently approved run and attempt",
        )
        return {"contract": copy.deepcopy(selected), "correlations": copy.deepcopy(correlations)}

    def producer_request(self, correlations: Doc) -> Doc:
        return {
            "contract_digest": planning.digest(self.components["producer"]),
            "correlations": copy.deepcopy(correlations),
        }

    def producer(self, correlations: Doc) -> Doc:
        published = remote_publication.show(
            Path(self.components["remote"]["journal"]),
            self.components["remote"]["journal_id"],
            correlations["publish"]["operation_id"],
        )
        store.require(
            published["status"] == "published" and published == correlations["publish"],
            "producer source differs from retained exact remote publication",
        )
        contract, selector = self.components["producer"], self.services.selector
        selection = correlations.get("producer_selection")
        if selection is None:
            selected = selector.select(self.services.source, contract, correlations["handoff"]["commit"], timeout=30)
            if selected["status"] == "pending":
                return {"status": "waiting", "reason": selected["reason"]}
            if selected["status"] != "selected":
                return {"status": "blocked", "reason": selected["reason"]}
            # Persist exact selection before polling its terminal outcome on the next schedule.
            return {
                "status": "waiting",
                "reason": "Exact approved producer selection retained before polling.",
                "correlations": {"producer_selection": selected["selection"]},
            }
        polled = selector.poll(self.services.source, contract, selection, timeout=30)
        return {
            "status": "succeeded"
            if polled["status"] == "succeeded"
            else "waiting"
            if polled["status"] == "waiting"
            else "blocked",
            "reason": polled["reason"],
            "correlations": {"producer_result": polled},
        }

    def reobserve(self, correlations: Doc) -> Doc:
        receipt = correlations.get("complete")
        store.require(
            isinstance(receipt, dict) and receipt.get("status") == "completed",
            "only native completion permits final reobservation",
        )
        assert isinstance(receipt, dict)
        native = completion.show(self.ledger, receipt["id"])
        store.require(native["receipt"] == receipt, "native completion receipt is unavailable or changed")
        fresh = planning.create(
            {**self.documents, "__current_state__": self._state()},
            self.readiness,
            Target(correlations["target"]),
            self.date,
        )
        check = [item for item in fresh["checks"] if item["record"] == self.action["record"]]
        satisfied = len(check) == 1 and check[0]["result"] == "satisfied"
        return {
            "status": "satisfied" if satisfied else "blocked",
            "plan_digest": fresh["digest"],
            "reason": "Native completion and fresh current-state assessment satisfy the selected output goal."
            if satisfied
            else "Current-state reobservation does not satisfy the completed output; inspect drift.",
        }

    def cancel(self, correlations: Doc) -> None:
        raise store.StoreError(
            "Use controller.cancel with its independently selected authorizer for metered native cancellation."
        )


class NativeEffect:
    def __init__(self, workflow: NativeWorkflow, kind: str) -> None:
        self.workflow, self.kind, self.adapter_id = workflow, kind, ADAPTERS[kind]

    @property
    def native_configuration_digest(self) -> str:
        return self.workflow.configuration_digest

    def bound_ms(self, method: str, request: Doc) -> int:
        store.require(method in {"inspect", "invoke", "recover", "resume", "cancel"}, "unsupported native method bound")
        configured = self.workflow.components.get("control_bounds_ms", {}).get(self.kind, {})
        bound = configured.get(method, self.workflow.components["costs"][self.kind]["wall_time_ms"])
        store.require(
            type(bound) is int and 0 < bound <= 300000, "native method requires an approved total wall ceiling"
        )
        return int(bound)

    def inspect(self, request: Doc) -> Doc:
        return self.workflow.services.boundary(lambda: self._inspect(request), self.bound_ms("inspect", request) / 1000)

    def _scope(self, request: Doc) -> Doc:
        expected = {**self.workflow.components["controller"], "workflow_id": planning.digest(self.workflow.contract)}
        store.require(
            request.get("controller") == expected, "native request selects another initialized controller store"
        )
        return expected

    def _inspect(self, request: Doc) -> Doc:
        self._scope(request)
        workflow = self.workflow
        workflow._read_permitted()
        store.require(request["phase"] == self.kind, "native adapter phase changed")
        workflow._check_config()
        costs = workflow.components["costs"][self.kind]
        store.require(0 < costs["wall_time_ms"] <= 300000, "native phase cost must bound its total physical deadline")
        if self.kind == "execute":
            store.require(
                costs["wall_time_ms"] >= workflow.components["executor_policy"]["timeout_seconds"] * 1000,
                "native worker wall reservation is smaller than its execution deadline",
            )
        assessed = workflow.assess(request["correlations"])
        store.require(assessed["status"] == "eligible", assessed["reason"])
        document = workflow._native_request(self.kind, request["correlations"])
        store.require(
            document == request["native_request"], "native request differs from current approved configuration"
        )
        remote_precondition = (
            workflow.current_publication(request["correlations"]) if self.kind in {"associate", "complete"} else None
        )
        grant = workflow._grant(self.kind, document)
        store.require(grant == request["authorization"], "native exact grant or preconditions changed")
        store_identity = workflow.components["native_stores"][self.kind]
        operation = grant.get(
            "dispatch_id",
            grant.get(
                "execution_id",
                grant.get(
                    "handoff_id", grant.get("operation_id", grant.get("association_id", grant.get("request_digest")))
                ),
            ),
        )
        source_paths = (
            [item["path"] for item in workflow.services.prepared.identity["files"]] if self.kind == "execute" else []
        )
        write_paths = sorted(document["mappings"].values()) if self.kind == "handoff" else []
        repository = (
            document["dispatch_policy"]["repository"] if self.kind == "dispatch" else workflow.trust["repository"]
        )
        destination = (
            {"repository_id": int(document["repository_id"]), "ref": document["ref"]}
            if self.kind == "publish"
            else None
        )
        return {
            "schema_version": VERSION,
            "objective_id": workflow.contract["objective_id"],
            "original_goal": workflow.contract["original_goal"],
            "action_id": workflow.action["id"],
            "contract": workflow.contract["contract"],
            "repository": repository,
            "base_revision": workflow.contract["input_target"]["revision"],
            "source_paths": source_paths,
            "write_paths": write_paths,
            "destination": destination,
            "native": {"store": store_identity, "operation_id": operation},
            "capabilities": ["intent-before-effects-v1", self.adapter_id],
            "cost": workflow.components["costs"][self.kind],
            "preconditions": {
                "state_digest": planning.digest(workflow._state()),
                "native_request_digest": planning.digest(document),
                "remote_publication": remote_precondition,
                "native_configuration_digest": self.native_configuration_digest,
            },
            "authorization": grant,
        }

    def _outcome(self, report: Doc) -> Doc:
        status = report["status"]
        successful = {
            "dispatch": "registered",
            "execute": "succeeded",
            "handoff": "published",
            "publish": "published",
            "associate": "associated",
            "complete": "completed",
        }[self.kind]
        if status == successful:
            if self.kind == "dispatch":
                receipt = report.get("receipt")
                if receipt is None:
                    receipt = report["record"]["events"][-1]["data"]["receipt"]
            else:
                receipt = report if self.kind == "publish" else report["receipt"]
            return {"status": "succeeded", "receipt": receipt}
        if status in {"failed", "cancelled", "timed_out", "output_invalid", "output_limit", "interrupted"}:
            return {
                "status": "cancelled" if status == "cancelled" else "failed",
                "receipt": report.get("receipt") or report,
            }
        # Authentic failed admission/readiness is a terminal unsuccessful invocation, never completion.
        if self.kind == "complete" and status in {"pending", "stale", "aborted"}:
            return {
                "status": "uncertain",
                "reason": "Native completion is pending; inspect admission/binding before a separately bounded retry.",
            }
        return {"status": "uncertain", "reason": report.get("reason", f"Native {self.kind} remains {status}.")}

    def _release_native(self, request: Doc) -> None:
        scope = self._scope(request)
        store.require(
            isinstance(scope, dict) and set(scope) == {"journal", "journal_id", "workflow_id"},
            "native automated effect requires its exact durable controller correlation",
        )
        with controller_store.opened(Path(scope["journal"]), scope["journal_id"], write=True) as retained:
            value = retained.read()
            store.require(
                value["identity"]["contract"] == self.workflow.contract
                and value["identity"]["workflow_id"] == scope["workflow_id"],
                "native effect controller contract changed",
            )
            event = value["event"]
            store.require(
                event["status"] not in {"paused", "cancelled"},
                "controller cancellation or pause vetoed native invocation; reconcile the retained reservation",
            )
            pending = event["pending"]
            assert isinstance(pending, dict)
            store.require(
                isinstance(pending, dict)
                and pending["kind"] == self.kind
                and pending["request"] == request
                and pending.get("released") is True,
                "native effect does not match the released controller phase",
            )
            retained.append(
                {
                    **event,
                    "pending": {**pending, "native_released": True},
                    "reason": "Exact native request handed off; cancellation preserves any uncertain outcome.",
                }
            )

    def _deadline(self, method: str, request: Doc) -> float:
        reserved_ms = self.workflow.components["costs"][self.kind]["wall_time_ms"]
        enforced_ms = self.bound_ms(method, request)
        store.require(reserved_ms >= enforced_ms, "native reservation is smaller than its enforced whole-call ceiling")
        reserved = enforced_ms / 1000
        remaining = (_timestamp(self.workflow.contract["deadline_at"]) - self.workflow.services.clock()).total_seconds()
        store.require(0 < reserved <= 300 and remaining > 0, "native phase has no remaining bounded wall authority")
        return min(reserved, remaining)

    def _supervised(self, method: str, request: Doc, operation: Callable[[], Doc], timeout: float) -> Doc:
        if self.workflow.services.boundary is not bounded_native_call:
            return self.workflow.services.boundary(operation, timeout)  # Explicit trusted controlled test backend.
        identifier = planning.digest({"method": method, "request": request, "nonce": os.urandom(16).hex()})
        scope = request["controller"]

        def owner(identity: Doc) -> None:
            with controller_store.opened(Path(scope["journal"]), scope["journal_id"], write=True) as journal:
                value = journal.read()
                store.require(
                    value["identity"]["contract"] == self.workflow.contract,
                    "native supervisor belongs to another workflow contract",
                )
                event = value["event"]
                store.require(event["status"] not in {"cancelled", "paused"}, "controller vetoed supervisor release")
                supervisors = event.get("supervisors", [])
                journal.append(
                    {
                        **event,
                        "supervisors": [
                            *supervisors,
                            {
                                "id": identifier,
                                "method": method,
                                "request_digest": planning.digest(request),
                                "identity": identity,
                                "phase": "prepared",
                            },
                        ],
                    }
                )

        def cleaned(complete: bool) -> None:
            with controller_store.opened(Path(scope["journal"]), scope["journal_id"], write=True) as journal:
                event = journal.read()["event"]
                supervisors = event.get("supervisors", [])
                if not any(item["id"] == identifier for item in supervisors):
                    return  # The parent never durably released this gated invocation.
                updated = [
                    {**item, "phase": "terminated" if complete else "unresolved"} if item["id"] == identifier else item
                    for item in supervisors
                ]
                journal.append({**event, "supervisors": updated})

        return bounded_native_call(operation, timeout, on_owner=owner, on_cleanup=cleaned)

    def _supervisors_clear(self, request: Doc) -> bool:
        if self.workflow.services.boundary is not bounded_native_call:
            return True
        scope = request["controller"]
        event = show(Path(scope["journal"]), scope["journal_id"])["event"]
        return all(
            item["phase"] == "terminated"
            for item in event.get("supervisors", [])
            if item["request_digest"] == planning.digest(request)
        )

    def invoke(self, request: Doc, authorization: Doc) -> Doc:
        return self._supervised(
            "invoke", request, lambda: self._invoke(request, authorization), self._deadline("invoke", request)
        )

    def _invoke(self, request: Doc, authorization: Doc) -> Doc:
        self._release_native(request)
        workflow, document = self.workflow, request["native_request"]
        workflow._check_anchor()
        store.require(authorization == request["authorization"], "native invocation authorization changed")
        if self.kind == "dispatch":
            store.require(workflow.services.dispatch_client is not None, "trusted dispatch adapter is unavailable")
            assert workflow.services.dispatch_client is not None
            report = dispatch.apply(
                Path(workflow.components["dispatch_policy"]["storage"]["journal"]),
                workflow.ledger,
                workflow.documents,
                workflow.state_path,
                workflow._dispatch(document),
                authorization=authorization,
                client=workflow.services.dispatch_client,
            )
        elif self.kind == "execute":
            apply = worker_integration.apply
            policy = workflow.components["executor_policy"]
            report = apply(
                Path(policy["storage"]["journal"]),
                workflow.ledger,
                workflow.documents,
                workflow.state_path,
                workflow._executor(document),
                authorization=authorization,
                prepared=workflow.services.prepared,
                guard=workflow.services.guard,
            )
        elif self.kind == "handoff":
            report = handoff.apply(handoff.request_from_document(document), authorization=authorization)
        elif self.kind == "publish":
            report = remote_publication.apply(
                workflow._remote(document), authorization=authorization, transport=workflow.services.transport
            )
        elif self.kind == "associate":
            report = output_association.apply(workflow._association(document))
        else:
            arguments = {
                **document,
                "output_target": Target(document["output_target"]),
                "date": workflow.date,
                "ledger": Path(document["ledger"]),
                "state_path": Path(document["state_path"]),
            }
            arguments["plan_doc"] = arguments.pop("plan")
            report = completion.complete(workflow.services.source, **arguments)
        return self._outcome(report)

    def _absent(self, request: Doc, authorization: Doc) -> Doc:
        native = self.workflow.components["native_stores"][self.kind]
        operation = authorization.get(
            "dispatch_id",
            authorization.get(
                "execution_id",
                authorization.get(
                    "handoff_id",
                    authorization.get(
                        "operation_id", authorization.get("association_id", authorization.get("request_digest"))
                    ),
                ),
            ),
        )
        return {
            "status": "not_started",
            "proof": {
                "native": {"store": native, "operation_id": operation},
                "authorization_digest": planning.digest(authorization),
            },
        }

    def recover(self, request: Doc, authorization: Doc) -> Doc:
        """Consult pinned native history under a separately metered finite recovery deadline."""
        timeout = self.bound_ms("recover", request) / 1000
        return self.workflow.services.boundary(lambda: self._recover(request, authorization), timeout)

    def _recover(
        self, request: Doc, authorization: Doc, *, terminal_only: bool = False, supervision_checked: bool = False
    ) -> Doc:
        self._scope(request)
        if not supervision_checked and not self._supervisors_clear(request):
            return {
                "status": "uncertain",
                "reason": "Native supervisor cleanup is unresolved; no absence proof or replacement is authorized.",
            }
        workflow, document = self.workflow, request["native_request"]
        workflow._check_anchor()
        store.require(authorization == request["authorization"], "native recovery authorization changed")
        if self.kind == "dispatch":
            native = workflow.components["native_stores"]["dispatch"]
            with dispatch_store.opened(Path(native["path"]), write=False, deadline=store.Deadline()) as journal:
                store.require(journal.journal_id == native["id"], "native dispatch journal instance changed")
                dispatch_record = journal.get(authorization["dispatch_id"])
                if dispatch_record is None:
                    return self._absent(request, authorization)
                dispatch._validate_history(dispatch_record, workflow._dispatch(document))
                report = {"status": dispatch_record["phase"], "record": dispatch_record}
            if report["status"] != "registered":
                return self._recoverable(request, authorization)
        elif self.kind == "execute":
            from exacly import executor_store

            policy = workflow.components["executor_policy"]
            with executor_store.opened(Path(policy["storage"]["journal"]), write=False) as journal:
                store.require(
                    journal.journal_id == policy["storage"]["journal_id"], "native executor store instance changed"
                )
                retained = journal.get(authorization["execution_id"])
                if retained is None or not retained["attempts"]:
                    return self._absent(request, authorization)
            backends = worker_integration.guarded_backends(workflow.services.guard)
            if terminal_only:
                return self._outcome(
                    executor.show(Path(policy["storage"]["journal"]), authorization["execution_id"], backends=backends)
                )
            report = executor.recover(
                Path(policy["storage"]["journal"]), authorization["execution_id"], backends=backends
            )
        elif self.kind == "handoff":
            native = workflow.components["native_stores"]["handoff"]
            with handoff_store.opened(Path(native["path"]), write=False, deadline=store.Deadline()) as journal:
                store.require(journal.journal_id == native["id"], "native handoff journal instance changed")
                retained_handoff = handoff._checked(journal, authorization["handoff_id"])
                if retained_handoff is None:
                    return self._absent(request, authorization)
                report = handoff._view("show", retained_handoff)
            if report["status"] == "missing":
                return self._absent(request, authorization)
            if report["status"] != "published":
                return self._recoverable(request, authorization)
        elif self.kind == "publish":
            native = workflow.components["native_stores"]["publish"]
            retained_remote = remote_publication.lookup(
                Path(native["path"]), native["id"], authorization["operation_id"]
            )
            if retained_remote is None:
                return self._absent(request, authorization)
            if terminal_only:
                return self._outcome(
                    remote_publication.show(Path(native["path"]), native["id"], authorization["operation_id"])
                )
            report = remote_publication.recover(
                Path(native["path"]), native["id"], authorization["operation_id"], transport=workflow.services.transport
            )
        elif self.kind == "associate":
            report = output_association.show(document["storage"], authorization["association_id"])
            if report["status"] == "missing":
                return self._absent(request, authorization)
            if report["status"] != "associated":
                return self._recoverable(request, authorization)
        else:
            with (
                store.locked(workflow.ledger, write=False) as (absolute, _),
                store.database(absolute, create=False) as (connection, _created),
            ):
                original_id = request["correlations"].get(
                    "original_binding_id", workflow.components.get("initial_binding_id")
                )
                store.require(isinstance(original_id, str), "completion requires its original native dispatch binding")
                original = completion_store.envelope(connection, original_id)
                store.require(
                    original["binding"]["id"] == original_id, "native completion ledger original identity changed"
                )
                completion_store.envelope(connection, document["binding_id"])
                receipt = completion_store.find_completed(
                    connection, authorization["request_digest"], binding_id=document["binding_id"]
                )
                pending = completion_store.current(connection, binding_id=document["binding_id"])
            if receipt is not None:
                return {"status": "succeeded", "receipt": receipt}
            if pending is None:
                return self._absent(request, authorization)
            if terminal_only:
                return {"status": "uncertain", "reason": "Native completion has no matching terminal receipt."}
            report = completion.recover(workflow.ledger, pending["intent"]["id"], state_path=workflow.state_path)
        return self._outcome(report)

    def cancel(self, request: Doc, authorization: Doc) -> Doc:
        store.require(self.kind == "execute", "this native effect has no supported in-flight cancellation")

        def request_native() -> Doc:
            workflow = self.workflow
            self._scope(request)
            workflow._check_anchor()
            store.require(authorization == request["authorization"], "native cancellation grant changed")
            policy = workflow.components["executor_policy"]
            from exacly import executor_store

            with executor_store.opened(Path(policy["storage"]["journal"]), write=False) as journal:
                store.require(
                    journal.journal_id == policy["storage"]["journal_id"], "native cancellation journal changed"
                )
                if journal.get(authorization["execution_id"]) is None:
                    return {
                        "status": "uncertain",
                        "reason": "Native intent is absent; retain controller veto and supervision uncertainty.",
                    }
            report = executor.cancel(
                Path(policy["storage"]["journal"]),
                authorization["execution_id"],
                backends=worker_integration.guarded_backends(workflow.services.guard),
            )
            return self._outcome(report)

        return self.workflow.services.boundary(request_native, self.bound_ms("cancel", request) / 1000)

    def _recoverable(self, request: Doc, authorization: Doc) -> Doc:
        return {**self._absent(request, authorization), "status": "recoverable"}

    def resume(self, request: Doc, authorization: Doc) -> Doc:
        """Finish an exact native intent within separately charged recovery and wall authority."""
        return self._supervised(
            "resume", request, lambda: self._resume(request, authorization), self._deadline("resume", request)
        )

    def _resume(self, request: Doc, authorization: Doc) -> Doc:
        workflow, document = self.workflow, request["native_request"]
        store.require(authorization == request["authorization"], "native recovery authorization changed")
        observed = self._recover(request, authorization, terminal_only=False, supervision_checked=True)
        if observed["status"] != "recoverable":
            return observed
        self._release_native(request)
        if self.kind == "handoff":
            report = handoff.recover(
                Path(workflow.components["handoff_policy"]["storage"]["journal"]), authorization["handoff_id"]
            )
        elif self.kind == "associate":
            report = output_association.recover(document["storage"], authorization["association_id"])
        elif self.kind == "dispatch":
            store.require(workflow.services.dispatch_client is not None, "trusted dispatch adapter is unavailable")
            assert workflow.services.dispatch_client is not None
            report = dispatch.recover(
                Path(workflow.components["dispatch_policy"]["storage"]["journal"]),
                workflow.ledger,
                workflow.documents,
                workflow.state_path,
                workflow._dispatch(document),
                authorization=authorization,
                client=workflow.services.dispatch_client,
            )
        else:
            raise store.StoreError("native phase has no separately authorized prepared recovery")
        return self._outcome(report)


def recover(
    journal: Path, journal_id: str, *, workflow: Workflow, authority: Authorizer, clock: Callable[[], dt.datetime] = now
) -> Doc:
    """Reconcile retained outcomes even after pause/cancellation, without authorizing new effects."""
    return step(journal, journal_id, workflow=workflow, authority=authority, clock=clock, recovery_only=True)


def native_contract(
    *,
    plan: Doc,
    record: str,
    readiness_policy: Doc,
    trust_policy: Doc,
    components: Doc,
    authorizer_identity: Doc,
    host: Doc,
    deadline_at: str,
    poll_seconds: float = 1,
    max_poll_seconds: float = 30,
) -> Doc:
    """Build a candidate contract for independent review; computing it grants no authority."""
    components = validate_components(components)
    actions = [action for action in plan["actions"] if action["record"] == record]
    store.require(len(actions) == 1, "select one existing verification action")
    action = actions[0]
    initial = components.get("initial_binding_id")
    stores = {
        **components["native_stores"],
        "controller": {"path": components["controller"]["journal"], "id": components["controller"]["journal_id"]},
        "configuration": {"path": components["configuration_path"], "id": components["configuration_digest"]},
        "ledger": {"path": components["ledger"], "id": components["ledger_anchor"]["digest"]},
        "ledger_anchor": {
            "path": components["ledger_anchor"]["anchor_path"],
            "id": components["ledger_anchor"]["digest"],
        },
        "state": {"path": components["state"], "id": plan["inputs"]["config"]["digest"]},
    }
    return validate_contract(
        {
            "schema_version": VERSION,
            "scope": "local-machine",
            "objective_id": authorizer_identity["objective"]["id"],
            "original_goal": authorizer_identity["objective"]["original_goal"],
            "input_target": plan["target"],
            "goal": {"record": record, "dimension": "verification"},
            "plan_digest": plan["digest"],
            "initial_binding_id": initial,
            "contract": {
                "model": plan["inputs"]["config"]["digest"],
                "criteria": action["criteria_digest"],
                "bindings": action["bindings_digest"],
                "readiness": planning.digest(readiness_policy),
                "trust": planning.digest(trust_policy),
                "observer": trust_policy["observer"]["digest"],
            },
            "stores": stores,
            "authorizer": authorizer_identity,
            "host": host,
            "deadline_at": deadline_at,
            "poll_seconds": poll_seconds,
            "max_poll_seconds": max_poll_seconds,
        }
    )


class NativeProducerReader:
    adapter_id = "native-producer-observer-v1"

    def __init__(self, workflow: NativeWorkflow) -> None:
        self.workflow = workflow

    def _check_call_capacity(self) -> None:
        preflight = self.workflow.components.get("authority_preflight")
        if preflight is None:
            return
        selected = self.workflow.contract["authorizer"]
        history = standing_authorization.inspect(
            Path(selected["store"]["path"]),
            store_id=selected["store"]["id"],
            objective_id=self.workflow.contract["objective_id"],
        )
        calls = sum(
            decision["approved"]
            and decision["request"]["proposal"]["effect"].get("meter", {}).get("purpose") == "producer-observe"
            for decision in history["decisions"]
        )
        # inspect also runs after the new slot is committed. At the boundary, count includes
        # this call and every uncertain historical slot; a failed excess slot is never refunded.
        store.require(calls <= preflight["max_producer_reads"], "approved objective producer-read limit exhausted")

    def inspect(self, request: Doc) -> Doc:
        self._check_call_capacity()
        contract = self.workflow.components["producer"]
        digest = planning.digest(contract)
        store.require(
            set(request) == {"contract_digest", "correlations"} and request["contract_digest"] == digest,
            "producer observation differs from the independently approved contract",
        )
        bound = self.workflow.components.get("producer_timeout_ms", 30000)
        store.require(type(bound) is int and 0 < bound <= 300000, "producer read requires a bounded total deadline")
        return {
            "schema_version": VERSION,
            "repository": self.workflow.trust["repository"],
            "contract_digest": digest,
            "native_configuration_digest": self.workflow.configuration_digest,
            "wall_time_ms": bound,
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        selected = self.inspect(request)
        self.workflow._read_permitted()
        store.require(
            wall_time_ms == selected["wall_time_ms"], "producer read bound differs from reserved wall authority"
        )
        remaining = (_timestamp(self.workflow.contract["deadline_at"]) - self.workflow.services.clock()).total_seconds()
        store.require(remaining > 0, "producer workflow deadline exhausted")
        self._check_call_capacity()
        return self.workflow.services.boundary(
            lambda: self.workflow.producer(request["correlations"]), min(wall_time_ms / 1000, remaining)
        )


class NativeBaselineReader:
    """Authenticate an explicit original-input producer without publishing state or completion."""

    adapter_id = "native-baseline-admission-v1"

    def __init__(self, workflow: NativeWorkflow) -> None:
        self.workflow = workflow

    def inspect(self, request: Doc) -> Doc:
        selected = self.workflow.components["baseline"]
        required = {
            "schema_version",
            "producer_contract_digest",
            "target",
            "plan_digest",
            "action_id",
            "readiness_digest",
            "trust_digest",
            "run_id",
            "attempt",
        }
        store.require(
            set(request) == {"contract", "correlations"}
            and request["contract"] == selected
            and set(selected) == required
            and selected["schema_version"] == VERSION,
            "baseline read differs from its independently approved exact producer selection",
        )
        store.require(
            selected["producer_contract_digest"] == planning.digest(self.workflow.components["producer"])
            and selected["target"] == self.workflow.contract["input_target"]
            and selected["plan_digest"] == self.workflow.plan["digest"]
            and selected["action_id"] == self.workflow.action["id"]
            and selected["readiness_digest"] == planning.digest(self.workflow.readiness)
            and selected["trust_digest"] == planning.digest(self.workflow.trust)
            and type(selected["run_id"]) is int
            and selected["run_id"] > 0
            and selected["attempt"] == 1,
            "baseline read changes approved goal, policies, input or attempt",
        )
        bound = self.workflow.components.get("producer_timeout_ms", 30000)
        store.require(type(bound) is int and 0 < bound <= 300000, "baseline admission requires a whole-call ceiling")
        return {
            "schema_version": VERSION,
            "repository": self.workflow.trust["repository"],
            "contract_digest": planning.digest(selected),
            "native_configuration_digest": self.workflow.configuration_digest,
            "wall_time_ms": bound,
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        facts = self.inspect(request)
        self.workflow._read_permitted()
        store.require(facts["wall_time_ms"] == wall_time_ms, "baseline read differs from reserved wall authority")
        remaining = (_timestamp(self.workflow.contract["deadline_at"]) - self.workflow.services.clock()).total_seconds()
        store.require(remaining > 0, "baseline admission workflow deadline exhausted")

        def observe() -> Doc:
            workflow = self.workflow
            workflow._check_config()
            selected = request["contract"]
            snapshot = state_writer.read(workflow.state_path)
            binding = request["correlations"].get("original_binding_id", workflow.components.get("initial_binding_id"))
            store.require(isinstance(binding, str), "baseline read requires its native registered original task")
            report = reconciliation.preview(
                workflow.services.source,
                workflow.plan,
                workflow.action["id"],
                workflow.readiness,
                workflow.trust,
                Target(selected["target"]),
                selected["run_id"],
                selected["attempt"],
                workflow.date,
                ledger=workflow.ledger,
                binding_id=binding,
                state_doc=snapshot.document,
            )
            origin = workflow.services.source.get_json(
                f"/repos/{workflow.trust['repository']['full_name']}/actions/runs/{selected['run_id']}/attempts/{selected['attempt']}"
            )
            repository_id = workflow.trust["repository"]["id"]
            store.require(
                origin.get("id") == selected["run_id"]
                and origin.get("run_attempt") == selected["attempt"]
                and origin.get("head_sha") == selected["target"]["revision"]
                and origin.get("head_branch") == workflow.trust["branch"]
                and origin.get("event") == "push"
                and origin.get("workflow_id") == workflow.components["producer"]["workflow_id"]
                and origin.get("repository", {}).get("id") == repository_id
                and origin.get("head_repository", {}).get("id") == repository_id
                and origin.get("status") == "completed"
                and origin.get("conclusion") in {"success", "failure"},
                "baseline producer identity or completed outcome changed after native admission",
            )
            failed_checks = [
                check
                for check in (report.get("readiness") or {}).get("checks", [])
                if check["record"] == workflow.action["record"]
                and check["dimension"] == "verification"
                and check["result"] == "not_satisfied"
            ]
            evidence: Doc | None = None
            if (report.get("admission") or {}).get("status") == "admitted":
                admitted_origin = report["admission"]["origin"]
                archive = workflow.services.source.download_artifact(
                    workflow.trust["repository"]["full_name"], admitted_origin["artifact_id"]
                )
                store.require(
                    admission.sha256(archive) == admitted_origin["archive_digest"],
                    "baseline archive changed after native admission",
                )
                manifest, report_bytes = admission.unpack(archive)
                store.require(
                    admission.sha256(report_bytes) == admitted_origin["report_digest"],
                    "baseline report bytes changed after native admission",
                )
                evidence = {
                    "archive_b64": base64.b64encode(archive).decode(),
                    "report_b64": base64.b64encode(report_bytes).decode(),
                    "manifest": manifest,
                }
            after = state_writer.read(workflow.state_path)
            negative = (
                report["status"] == "pending"
                and (report.get("admission") or {}).get("status") == "admitted"
                and origin["conclusion"] == "failure"
                and len(failed_checks) == 1
                and report["claims"]["registered_binding_verified"] is True
                and snapshot.content == after.content
            )
            return {
                "status": "observed",
                "negative_established": negative,
                "evidence": evidence,
                "producer": {
                    "run_id": origin["id"],
                    "attempt": origin["run_attempt"],
                    "repository_id": repository_id,
                    "workflow_id": origin["workflow_id"],
                    "revision": origin["head_sha"],
                    "branch": origin["head_branch"],
                    "conclusion": origin["conclusion"],
                },
                "report": report,
                "before_state_digest": snapshot.digest,
                "after_state_digest": after.digest,
                "pending_unchanged": snapshot.content == after.content,
            }

        return self.workflow.services.boundary(observe, min(wall_time_ms / 1000, remaining))


class NativeNegativeReader:
    """Read authentic evidence against explicitly declared rejecting request variants.

    The model/verifier cases change only a read's expected digest. They establish rejection of an
    independently declared incompatible expectation against the actual immutable source, not that
    somebody deployed changed observer bytes. No remote mutation, binding or completion occurs.
    """

    adapter_id = "native-negative-admission-v1"

    def __init__(self, workflow: NativeWorkflow) -> None:
        self.workflow = workflow

    def inspect(self, request: Doc) -> Doc:
        workflow = self.workflow
        selected = workflow.components["negatives"]
        store.require(
            set(request) == {"contract", "case", "correlations"}
            and request["contract"] == selected
            and request["case"] in selected["cases"]
            and selected["producer_contract_digest"] == planning.digest(workflow.components["producer"])
            and selected["target"] == workflow.contract["input_target"]
            and selected["plan_digest"] == workflow.plan["digest"]
            and selected["action_id"] == workflow.action["id"]
            and selected["readiness_digest"] == planning.digest(workflow.readiness)
            and selected["trust_digest"] == planning.digest(workflow.trust)
            and selected["criteria_model_digest"] != workflow.trust["model"]["digest"]
            and selected["verifier_digest"] != workflow.trust["observer"]["digest"],
            "negative read differs from its independently approved cases, goal, policies or incompatible expectations",
        )
        return {
            "schema_version": VERSION,
            "repository": workflow.trust["repository"],
            "contract_digest": planning.digest(selected),
            "native_configuration_digest": workflow.configuration_digest,
            "wall_time_ms": selected["wall_time_ms"],
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        facts = self.inspect(request)
        workflow = self.workflow
        workflow._read_permitted()
        store.require(facts["wall_time_ms"] == wall_time_ms, "negative read changes reserved whole-call authority")
        remaining = (_timestamp(workflow.contract["deadline_at"]) - workflow.services.clock()).total_seconds()
        store.require(remaining > 0, "negative admission workflow deadline exhausted")

        def observe() -> Doc:
            workflow._check_config()
            event = show(
                Path(workflow.components["controller"]["journal"]), workflow.components["controller"]["journal_id"]
            )["event"]
            correlations = request["correlations"]
            store.require(
                event["phase"] == "associate" and event["pending"] is None and correlations == event["correlations"],
                "negative admission requires the retained exact producer selection before output association",
            )
            workflow.current_publication(correlations)
            native = workflow.components["native_stores"]["handoff"]
            with handoff_store.opened(Path(native["path"]), write=False, deadline=store.Deadline()) as journal:
                store.require(journal.journal_id == native["id"], "negative admission handoff journal instance changed")
                retained = handoff._checked(journal, correlations["handoff"]["handoff_id"])
                store.require(
                    retained is not None and retained["phase"] == "published",
                    "negative admission requires a published native handoff",
                )
                assert retained is not None
                handoff_receipt = retained["receipt"]
            store.require(handoff_receipt == correlations["handoff"], "negative admission handoff provenance changed")
            target = handoff_receipt["target"]
            selection = correlations["producer_selection"]
            store.require(
                selection["revision"] == target["revision"] and selection["attempt"] == 1,
                "negative admission selected producer differs from the exact native output",
            )
            before = state_writer.read(workflow.state_path)
            matched, reason, envelope = executions.match(
                workflow.ledger,
                workflow._binding(correlations),
                plan_doc=workflow.plan,
                action_id=workflow.action["id"],
                state_doc=before.document,
                readiness_policy=workflow.readiness,
                trust_policy=workflow.trust,
                output_target=Target(workflow.contract["input_target"]),
                date=workflow.date,
            )
            store.require(matched, "negative admission original pending binding is stale: " + reason)
            control = admission.admit(
                workflow.services.source,
                workflow.trust,
                Target(target),
                selection["run_id"],
                1,
                workflow.date,
            )
            chosen = copy.deepcopy(workflow.trust)
            candidate, run_id, attempt = copy.deepcopy(target), selection["run_id"], 1
            selected, case = request["contract"], request["case"]
            if case == "unavailable-producer":
                store.require(selected["unavailable_run_id"] != run_id, "unavailable probe selects the real producer")
                run_id = selected["unavailable_run_id"]
            elif case == "wrong-revision":
                candidate = selected["target"]
                store.require(candidate["revision"] != target["revision"], "wrong-revision probe has no distinct input")
            elif case == "wrong-attempt":
                attempt = selected["wrong_attempt"]
            elif case == "changed-criteria":
                chosen["model"]["digest"] = selected["criteria_model_digest"]
            elif case == "changed-verifier":
                chosen["observer"]["digest"] = selected["verifier_digest"]
            denied = admission.admit(
                workflow.services.source, chosen, Target(candidate), run_id, attempt, workflow.date
            )
            after = state_writer.read(workflow.state_path)
            evidence = None
            if control.archive is not None and control.report is not None:
                evidence = {
                    "archive_b64": base64.b64encode(control.archive).decode(),
                    "report_b64": base64.b64encode(control.report).decode(),
                }
            return {
                "status": "observed",
                "case": case,
                "probe": {
                    "target": candidate,
                    "run_id": run_id,
                    "attempt": attempt,
                    "policy_digest": planning.digest(chosen),
                    "semantics": "expected-model-digest-mismatch"
                    if case == "changed-criteria"
                    else "expected-observer-digest-mismatch"
                    if case == "changed-verifier"
                    else "actual-request-variant",
                },
                "control_admission": control.receipt,
                "admission": denied.receipt,
                "evidence": evidence,
                "original_binding": envelope["binding"]["id"],
                "original_pending": envelope["binding"]["contract"]["pending"],
                "negative_established": control.receipt["status"] == "admitted"
                and denied.receipt["status"] == "rejected"
                and before.content == after.content,
                "before_state_digest": before.digest,
                "after_state_digest": after.digest,
                "pending_unchanged": before.content == after.content,
                "claims": {
                    "state_published": False,
                    "completion_invoked": False,
                    "remote_mutated": False,
                    "changed_remote_verifier_demonstrated": False,
                    "external_ref_drift_demonstrated": False,
                },
            }

        return workflow.services.boundary(observe, min(wall_time_ms / 1000, remaining))
