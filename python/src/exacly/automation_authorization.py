"""Independently selected standing scope around exact native effect adapters.

Operator bootstrap, outside candidate authority, supplies a protected configuration path, its
independently approved content digest, issuer/approval identity, and installed Effect objects. There
is no candidate provider loader, approval-bypass flag, blanket grant, or implicit retry permission.
The first contract supports the fixed-verifier fixture pilot in one local coordination domain.

This authorizer checks trusted native facts against that scope, retains the exact native grant and
objective-wide reservation before invocation, and marks uncertainty before crossing the boundary.
Only the selected native adapter can establish outcome or prove an operation never started. Ordinary
remote absence is not such proof. Native APIs retain their own current-state and intent-before-effect
checks. Revocation stops new requests while historical receipt recovery remains available.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from exacly import automation_revocations as revocations
from exacly import execution_store as store
from exacly import standing_authorization as accounting

Doc = dict[str, Any]
VERSION = "0.1.0"
ADAPTERS = {
    "dispatch": "native-dispatch-v1",
    "execute": "native-worker-v1",
    "handoff": "native-handoff-v1",
    "publish": "native-publication-v1",
    "associate": "native-association-v1",
    "complete": "native-completion-v1",
}
CAPABILITIES = {
    "intent-before-effects-v1",
    "finish-prepared-v1",
    "cancel-inflight-v1",
    "local-receipt-read-v1",
    *ADAPTERS.values(),
}
READERS = {
    "producer-observe": "native-producer-observer-v1",
    "baseline-admission": "native-baseline-admission-v1",
}
MAX_CALL_MS = 300_000
CONTRACT_FIELDS = {"model", "criteria", "bindings", "readiness", "trust", "observer"}
PILOT_ROOTS = ("fixtures/unattended-pilot/inputs/", "fixtures/unattended-pilot/results/")
PROTECTED_ROOTS = ("python/", "spec/", "ci/", ".github/", "skills/")
UNCERTAIN_REASON = "Native invocation may start; reconcile the exact native journal before any retry."


class Effect(Protocol):
    """Trusted installed native adapter; candidate JSON cannot implement or select this object.

    inspect independently derives current facts and exact native authorization. invoke preserves the
    native API's authorization and precondition gates. recover never sends another request: terminal
    native receipts or authoritative pinned-journal absence are its only conclusive outcomes.
    """

    @property
    def adapter_id(self) -> str: ...

    @property
    def native_configuration_digest(self) -> str:
        """Recompute checked actual components; content cannot select its own approval pin."""
        ...

    def bound_ms(self, method: str, request: Doc) -> int:
        """The actual enforced whole-call ceiling, including nested requests and waiting."""
        ...

    def inspect(self, request: Doc) -> Doc: ...

    def invoke(self, request: Doc, authorization: Doc) -> Doc: ...

    def recover(self, request: Doc, authorization: Doc) -> Doc: ...


class MeteredReader(Protocol):
    """Trusted installed read-only adapter, selected in protected operator configuration."""

    @property
    def adapter_id(self) -> str: ...

    def inspect(self, request: Doc) -> Doc:
        """Read only local configuration; return pinned repository, contract and whole-call bound."""
        ...

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        """Enforce this total deadline around all requests, polls, backoff and subprocesses."""
        ...


class _MeterStopped(ValueError):
    def __init__(self, decision: Doc, reason: str, *, started: bool) -> None:
        super().__init__(reason)
        self.decision = decision
        self.started = started


def digest(value: Any) -> str:
    """Content identity only; approval comes from the independently supplied operator pin."""
    return "sha256:" + hashlib.sha256(store.canonical(value).encode()).hexdigest()


def _object(value: Any, keys: set[str] | None = None) -> Doc:
    store.require(isinstance(value, dict), "standing contracts require JSON objects")
    result: Doc = json.loads(store.canonical(value))
    store.require(keys is None or set(result) == keys, "unknown or missing standing contract fields")
    return result


def _text(value: Any) -> str:
    store.require(isinstance(value, str) and bool(value.strip()) and "\0" not in value, "invalid standing text")
    return str(value)


def _digest(value: Any) -> str:
    store.require(isinstance(value, str) and store.DIGEST.fullmatch(value), "invalid standing content digest")
    return str(value)


def _uuid(value: Any) -> str:
    _text(value)
    try:
        identity = uuid.UUID(hex=value)
    except (ValueError, AttributeError):
        raise store.StoreError("invalid standing store UUID") from None
    store.require(identity.hex == value and identity.version == 4, "invalid standing store UUID")
    return str(value)


def _absolute(value: Any) -> Path:
    _text(value)
    path = Path(value)
    store.require(
        path.is_absolute() and str(path) == value and ".." not in path.parts and path.resolve() == path,
        "standing storage requires canonical absolute paths without symbolic links",
    )
    store.location(path)
    return path


def _store(value: Any, *, accounting_store: bool = False) -> Doc:
    key = "store_id" if accounting_store else "id"
    result = _object(value, {"path", key})
    _absolute(result["path"])
    if accounting_store or not (isinstance(result[key], str) and store.DIGEST.fullmatch(result[key])):
        _uuid(result[key])
    return result


def _timestamp(value: Any) -> dt.datetime:
    _text(value)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        store.require(parsed.utcoffset() is not None, "standing lifetime requires timezone-aware timestamps")
        return parsed.astimezone(dt.UTC)
    except (ValueError, OverflowError, AttributeError):
        raise store.StoreError("invalid standing lifetime") from None


def _units(value: Any) -> Doc:
    result = _object(value, set(accounting.DIMENSIONS))
    store.require(
        all(type(item) is int and 0 <= item <= accounting.MAX_UNITS for item in result.values()),
        "standing budgets require finite nonnegative bounded integers",
    )
    store.require(
        result["model_requests"] == result["model_spend_microunits"] == 0,
        "model-service authority is unsupported by the first standing contract",
    )
    return result


def _contract(value: Any) -> Doc:
    result = _object(value, CONTRACT_FIELDS)
    for item in result.values():
        _digest(item)
    return result


def _paths(value: Any, *, prefixes: bool = False) -> list[str]:
    store.require(isinstance(value, list) and all(isinstance(item, str) for item in value), "invalid standing paths")
    store.require(
        len(value) <= 1024 and len(set(value)) == len(value) and value == sorted(value), "ambiguous standing paths"
    )
    for name in value:
        path = name[:-1] if prefixes and name.endswith("/") else name
        parsed = PurePosixPath(path)
        store.require(
            bool(path)
            and "\0" not in path
            and "\\" not in path
            and not parsed.is_absolute()
            and parsed.as_posix() == path
            and ".." not in parsed.parts
            and len(path.encode()) <= 4096,
            "standing paths must be normalized repository-relative paths",
        )
    return list(value)


def _within(path: str, root: str) -> bool:
    return path.startswith(root) if root.endswith("/") else path == root


def _repository(value: Any) -> Doc:
    result = _object(value, {"id", "full_name"})
    store.require(
        type(result["id"]) is int and result["id"] > 0, "standing repository requires immutable numeric identity"
    )
    store.require(
        isinstance(result["full_name"], str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result["full_name"]),
        "invalid standing repository name",
    )
    return result


def _destination(value: Any) -> Doc:
    result = _object(value, {"repository_id", "ref"})
    store.require(
        type(result["repository_id"]) is int and result["repository_id"] > 0, "invalid destination repository"
    )
    store.require(
        isinstance(result["ref"], str)
        and re.fullmatch(r"refs/heads/pilot/[A-Za-z0-9_/-]+", result["ref"])
        and not result["ref"].endswith("/")
        and "//" not in result["ref"],
        "first standing contract permits only explicitly selected nonproduction pilot branch refs",
    )
    return result


def _capabilities(value: Any) -> list[str]:
    store.require(
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and value == sorted(set(value))
        and bool(value)
        and set(value) <= CAPABILITIES,
        "unknown or ambiguous standing runtime capabilities",
    )
    return list(value)


def validate_delegation(value: Any) -> Doc:
    """Validate a closed candidate contract without approving it or selecting an authorizer."""
    result = _object(
        value,
        {
            "schema_version",
            "id",
            "issuer",
            "approval_id",
            "config_path",
            "accounting",
            "revocations",
            "objective",
            "limits",
            "not_before",
            "expires_at",
            "contract",
            "repository",
            "base_revision",
            "allowed_paths",
            "protected_paths",
            "destinations",
            "actions",
            "effects",
            "reads",
            "native_configuration_digest",
        },
    )
    store.require(result["schema_version"] == VERSION, "unsupported standing delegation version")
    _digest(result["native_configuration_digest"])
    for key in ("id", "issuer", "approval_id"):
        _text(result[key])
    _absolute(result["config_path"])
    _store(result["accounting"], accounting_store=True)
    _store(result["revocations"], accounting_store=True)
    store.require(
        result["accounting"]["path"] != result["revocations"]["path"], "accounting and revocation stores overlap"
    )
    objective = _object(result["objective"], {"schema_version", "id", "original_goal"})
    store.require(objective["schema_version"] == VERSION, "unsupported standing objective version")
    _digest(objective["id"])
    _digest(objective["original_goal"])
    _units(result["limits"])
    store.require(_timestamp(result["not_before"]) < _timestamp(result["expires_at"]), "standing lifetime is empty")
    _contract(result["contract"])
    _repository(result["repository"])
    store.require(
        isinstance(result["base_revision"], str) and re.fullmatch(r"[0-9a-f]{40}", result["base_revision"]),
        "standing source requires exact approved base revision",
    )
    allowed = _paths(result["allowed_paths"], prefixes=True)
    protected = _paths(result["protected_paths"], prefixes=True)
    store.require(bool(allowed), "standing source scope must explicitly select permitted fixture paths")
    store.require(
        all(any(_within(path, root) for root in PILOT_ROOTS) for path in allowed),
        "first standing contract confines worker paths to selected root pilot fixtures",
    )
    store.require(
        all(
            not any(_within(path, root) or _within(root.rstrip("/"), path) for root in (*PROTECTED_ROOTS, *protected))
            for path in allowed
        ),
        "standing permitted and protected paths overlap",
    )
    destinations = result["destinations"]
    store.require(isinstance(destinations, list) and len(destinations) <= 16, "invalid standing destination scope")
    checked = [_destination(item) for item in destinations]
    store.require(len({store.canonical(item) for item in checked}) == len(checked), "ambiguous destination scope")
    store.require(
        all(item["repository_id"] == result["repository"]["id"] for item in checked),
        "standing destination selects another repository",
    )
    actions = result["actions"]
    store.require(
        isinstance(actions, list)
        and bool(actions)
        and all(isinstance(item, str) for item in actions)
        and actions == sorted(set(actions)),
        "ambiguous eligible actions",
    )
    for action in actions:
        _digest(action)
    effects = _object(result["effects"])
    store.require(bool(effects) and set(effects) <= ADAPTERS.keys(), "unsupported standing effect authority")
    for kind, scope in effects.items():
        scope = _object(
            scope,
            {
                "adapter_id",
                "capabilities",
                "native_store",
                "max_cost",
                "cancel_inflight",
                "recover_after_revocation",
                "recover_after_expiry",
            },
        )
        store.require(scope["adapter_id"] == ADAPTERS[kind], "standing scope selects an unsupported installed adapter")
        capabilities = _capabilities(scope["capabilities"])
        store.require(
            "intent-before-effects-v1" in capabilities, "standing adapter requires native intent-before-effects"
        )
        _store(scope["native_store"])
        costs = _units(scope["max_cost"])
        store.require(
            costs["attempts"] >= 1 and costs["wall_time_ms"] >= 1, "standing effects require bounded attempts and time"
        )
        store.require(type(scope["cancel_inflight"]) is bool, "ambiguous standing cancellation authority")
        store.require(
            type(scope["recover_after_revocation"]) is bool and type(scope["recover_after_expiry"]) is bool,
            "historical recovery needs explicit independently approved lifetime semantics",
        )
        store.require(
            not scope["cancel_inflight"] or "cancel-inflight-v1" in capabilities,
            "standing cancellation requires its explicit installed capability",
        )
    reads = _object(result["reads"])
    store.require(set(reads) <= READERS.keys(), "unsupported standing read-only operation")
    for purpose, scope in reads.items():
        scope = _object(scope, {"adapter_id", "max_wall_time_ms", "contract_digest"})
        store.require(scope["adapter_id"] == READERS[purpose], "unsupported installed read-only adapter")
        store.require(
            type(scope["max_wall_time_ms"]) is int and 1 <= scope["max_wall_time_ms"] <= MAX_CALL_MS,
            "read-only request requires a finite enforced whole-call ceiling",
        )
        _digest(scope["contract_digest"])
    return result


def validate_facts(value: Any) -> Doc:
    """Closed facts supplied by a reviewed native adapter, never accepted from a candidate report."""
    result = _object(
        value,
        {
            "schema_version",
            "objective_id",
            "original_goal",
            "action_id",
            "contract",
            "repository",
            "base_revision",
            "source_paths",
            "write_paths",
            "destination",
            "native",
            "capabilities",
            "cost",
            "preconditions",
            "authorization",
        },
    )
    store.require(result["schema_version"] == VERSION, "unsupported current facts version")
    for key in ("objective_id", "original_goal", "action_id"):
        _digest(result[key])
    _contract(result["contract"])
    _repository(result["repository"])
    store.require(
        isinstance(result["base_revision"], str) and re.fullmatch(r"[0-9a-f]{40}", result["base_revision"]),
        "native facts require exact approved base revision",
    )
    _paths(result["source_paths"])
    _paths(result["write_paths"])
    if result["destination"] is not None:
        _destination(result["destination"])
    native = _object(result["native"], {"store", "operation_id"})
    _store(native["store"])
    _digest(native["operation_id"])
    _capabilities(result["capabilities"])
    costs = _units(result["cost"])
    store.require(
        costs["attempts"] >= 1 and costs["wall_time_ms"] >= 1, "native effect must reserve bounded attempt and time"
    )
    preconditions = _object(result["preconditions"])
    _digest(preconditions.get("native_configuration_digest"))
    store.require(bool(_object(result["authorization"])), "native effect requires an exact nonempty authorization")
    return result


def _outcome(value: Any, facts: Doc) -> Doc:
    result = _object(value)
    if result.get("status") in ("succeeded", "failed", "cancelled"):
        _object(result, {"status", "receipt"})
        store.require(bool(_object(result["receipt"])), "native terminal outcome requires retained receipt")
    elif result.get("status") == "uncertain":
        _object(result, {"status", "reason"})
        _text(result["reason"])
    elif result.get("status") in ("not_started", "recoverable"):
        _object(result, {"status", "proof"})
        proof = _object(result["proof"], {"native", "authorization_digest"})
        store.require(
            proof["native"] == facts["native"]
            and proof["authorization_digest"] == digest(facts["authorization"])
            and "intent-before-effects-v1" in facts["capabilities"],
            "not-started recovery lacks proof from the exact pinned native journal",
        )
    else:
        raise store.StoreError("unknown native effect outcome")
    return result


class Authorizer:
    """Trusted-selected scope and invocation boundary for one immutable logical objective."""

    def __init__(
        self,
        config_path: Path,
        *,
        approved_digest: str,
        issuer: str,
        approval_id: str,
        effects: Mapping[str, Effect],
        readers: Mapping[str, MeteredReader] | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._path = _absolute(str(config_path))
        self._approved_digest = _digest(approved_digest)
        self._issuer = _text(issuer)
        self._approval_id = _text(approval_id)
        self._effects = dict(effects)
        self._readers = dict(readers or {})
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._lock_timeout = lock_timeout
        with self._configuration() as config:
            self._config = config
            for kind, scope in config["effects"].items():
                store.require(
                    kind in self._effects and self._effects[kind].adapter_id == scope["adapter_id"],
                    "approved standing adapter is not installed by trusted operator configuration",
                )
            for purpose, scope in config["reads"].items():
                store.require(
                    purpose in self._readers and self._readers[purpose].adapter_id == scope["adapter_id"],
                    "approved read-only adapter is not installed by trusted operator configuration",
                )
            accounting.enroll(
                Path(config["accounting"]["path"]),
                store_id=config["accounting"]["store_id"],
                objective=config["objective"],
                limits=config["limits"],
                lock_timeout=lock_timeout,
            )
            self._availability(config)  # Establish the independently pinned revocation history at construction.
            self._initialize_objective_lock(config)

    def _objective_lock_identity(self, config: Doc) -> tuple[Path, bytes]:
        accounting_path = _absolute(config["accounting"]["path"])
        name = ".exacly-objective-" + config["accounting"]["store_id"] + "-" + config["objective"]["id"][7:] + ".lock"
        path = accounting_path.parent / name
        expected = (
            store.canonical(
                {
                    "schema_version": VERSION,
                    "accounting_path": str(accounting_path),
                    "store_id": config["accounting"]["store_id"],
                    "objective": config["objective"],
                }
            )
            + "\n"
        ).encode()
        return path, expected

    def _initialize_objective_lock(self, config: Doc) -> None:
        """Initialize the derived coordination identity, never a policy-selected lock namespace."""
        path, expected = self._objective_lock_identity(config)
        history = accounting.inspect(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
            lock_timeout=self._lock_timeout,
        )
        has_writes = any(self._native_write(item) for item in history["decisions"])
        deadline = store.Deadline(self._lock_timeout)
        # This short directory lock initializes/checks bytes only. It is released before taking
        # the file mutex and before any nested accounting or native operation.
        with store.locked(path, write=True, deadline=deadline) as (absolute, parent_fd):
            if not os.path.lexists(absolute):
                store.require(
                    not has_writes,
                    "objective invocation lock is missing after native authority; preserve history and escalate",
                )
                descriptor = os.open(absolute, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    store.file_info(descriptor)
                    store.require(
                        os.write(descriptor, expected) == len(expected),
                        "objective invocation lock write was incomplete",
                    )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(parent_fd)
            verified = self._checked_objective_lock(absolute, expected)
            try:
                os.fsync(verified)
                os.fsync(parent_fd)
            finally:
                os.close(verified)

    @staticmethod
    def _checked_objective_lock(path: Path, expected: bytes) -> int:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = store.file_info(descriptor)
            store.require(
                os.pread(descriptor, len(expected) + 1, 0) == expected, "objective invocation lock identity changed"
            )
            current = path.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
                "objective invocation lock file changed",
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _objective_operation(self, config: Doc) -> Iterator[None]:
        """Serialize admission/recovery across every approved policy for this logical objective.

        Accounting's atomic reservations remain authoritative. This separate immutable file mutex
        protects the check-and-reserve admission sequence without holding its directory flock
        across nested accounting calls. Policy filenames, revisions, actions and native journals
        cannot choose a different fence. Deleting or replacing this file is not recovery.
        """
        path, expected = self._objective_lock_identity(config)
        deadline = store.Deadline(self._lock_timeout)
        descriptor = self._checked_objective_lock(path, expected)
        try:
            before = os.fstat(descriptor)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, InterruptedError):
                    deadline.check()
                    time.sleep(min(store.LOCK_RETRY_INTERVAL, deadline.remaining()))
            current = path.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
                "objective invocation lock was replaced",
            )
            yield
            current = path.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
                "objective invocation lock was replaced",
            )
            store.require(
                os.pread(descriptor, len(expected) + 1, 0) == expected, "objective invocation lock identity changed"
            )
        finally:
            os.close(descriptor)

    @staticmethod
    def _native_write(decision: Doc) -> bool:
        effect = decision["request"]["proposal"]["effect"]
        return bool(
            decision["approved"] and effect.get("kind") in ADAPTERS and "facts" in effect and "meter" not in effect
        )

    def _unresolved_write(self, config: Doc, *, excluding: str | None = None) -> str | None:
        history = accounting.inspect(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
            lock_timeout=self._lock_timeout,
        )
        for decision in history["decisions"]:
            proposal = decision["request"]["proposal"]
            if (
                self._native_write(decision)
                and decision["status"] in ("reserved", "uncertain")
                and proposal["id"] != excluding
            ):
                effect = proposal["effect"]
                native = effect["facts"]["native"]
                return str(
                    "Unresolved original-objective native write "
                    + proposal["id"]
                    + " ("
                    + effect["kind"]
                    + ", native "
                    + native["operation_id"]
                    + ", store "
                    + native["store"]["path"]
                    + ") blocks another write. Reconcile that exact approved operation; another policy, action, "
                    "native journal or workflow alias cannot forget it. If its adapter is unavailable, escalate."
                )
        return None

    @contextmanager
    def _configuration(self) -> Iterator[Doc]:
        descriptor: int | None = None
        deadline = store.Deadline(self._lock_timeout)
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = store.file_info(descriptor)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, InterruptedError):
                    deadline.check()
                    time.sleep(min(store.LOCK_RETRY_INTERVAL, deadline.remaining()))
            store.require(before.st_size <= store.RECORD_LIMIT, "standing configuration exceeds the size limit")
            content = os.read(descriptor, store.RECORD_LIMIT + 1)
            value = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
            config = validate_delegation(value)
            store.require(config["config_path"] == str(self._path), "standing configuration was copied to another path")
            store.require(
                digest(config) == self._approved_digest
                and config["issuer"] == self._issuer
                and config["approval_id"] == self._approval_id,
                "standing delegation lacks the independently selected approval identity",
            )
            current = self._path.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "standing configuration changed"
            )
            yield config
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise store.StoreError("cannot read protected approved standing configuration") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def identity(self) -> Doc:
        config = self._config
        return _object(
            {
                "schema_version": VERSION,
                "delegation_id": config["id"],
                "delegation_digest": self._approved_digest,
                "issuer": self._issuer,
                "approval_id": self._approval_id,
                "store": {"path": config["accounting"]["path"], "id": config["accounting"]["store_id"]},
                "objective": {key: config["objective"][key] for key in ("id", "original_goal")},
            }
        )

    def _availability(self, config: Doc) -> Doc:
        now = self._clock()
        store.require(
            isinstance(now, dt.datetime) and now.utcoffset() is not None, "standing clock must be timezone-aware"
        )
        revoked = revocations.status(
            Path(config["revocations"]["path"]),
            store_id=config["revocations"]["store_id"],
            delegation_id=config["id"],
            lock_timeout=self._lock_timeout,
        )
        if revoked is not None:
            return {"available": False, "reason": "Standing delegation revoked: " + revoked["reason"]}
        if now < _timestamp(config["not_before"]):
            return {"available": False, "reason": "Standing delegation is not yet effective."}
        if now >= _timestamp(config["expires_at"]):
            return {"available": False, "reason": "Standing delegation expired."}
        return {"available": True, "reason": "Standing delegation is current and not revoked."}

    def check_available(self) -> Doc:
        try:
            with self._configuration() as config:
                return self._availability(config)
        except (ValueError, OSError) as error:
            return {"available": False, "reason": "Standing authority unavailable: " + str(error)}

    def _meter(
        self,
        config: Doc,
        *,
        purpose: str,
        operation_id: str,
        request: Doc,
        adapter_id: str,
        wall_time_ms: int,
        callback: Callable[[], Doc],
    ) -> tuple[Doc, Doc]:
        """Mint and commit one fresh authoritative slot before releasing a bounded control call.

        Caller IDs correlate requests; they never select or replay a budget slot. Every actual call
        gets a new operator-minted UUID, also across restarts and approved policy versions. Atomic
        reservation in the original objective store arbitrates concurrent callers. No outcome,
        including a failed or interrupted read, refunds any of the reserved whole-call maximum.
        """
        slot = uuid.uuid4().hex
        identity = digest(
            {"store": config["accounting"]["store_id"], "objective": config["objective"]["id"], "slot": slot}
        )
        store.require(
            self._decision(config, identity) is None, "control slot already exists; minting cannot replay authority"
        )
        cost = {key: 0 for key in accounting.DIMENSIONS}
        cost["wall_time_ms"] = wall_time_ms
        proposal = {
            "schema_version": VERSION,
            "id": identity,
            "delegation_id": config["id"],
            "policy_digest": self._approved_digest,
            "effect": {
                "meter": {
                    "slot": slot,
                    "purpose": purpose,
                    "operation_id": operation_id,
                    "adapter_id": adapter_id,
                    "request": request,
                    "wall_time_ms": wall_time_ms,
                }
            },
            "preconditions": {"approval_id": self._approval_id, "contract": config["contract"]},
            "authorization": {
                "operation": "bounded-control-call",
                "purpose": purpose,
                "adapter_id": adapter_id,
                "slot": slot,
                "wall_time_ms": wall_time_ms,
            },
            "cost": cost,
        }
        decision = accounting.reserve(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
            proposal=proposal,
            approved=True,
            reason="Reserve the enforced whole-call maximum before this exact control request.",
            lock_timeout=self._lock_timeout,
        )
        if not decision["approved"]:
            raise _MeterStopped(
                decision, "Authoritative control budget exhausted: " + decision["reason"], started=False
            )
        # A collision cannot release a previously used grant; do not treat idempotent reserve as a
        # fresh ticket. UUID4 makes a collision exceptional, but its authority semantics stay closed.
        store.require(decision["status"] == "reserved", "control slot was already used; no request is released")
        decision = accounting.mark_uncertain(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
            proposal_id=identity,
            reason="Control request may start; another actual request requires a new reserved slot.",
            lock_timeout=self._lock_timeout,
        )
        try:
            result = _object(callback())
            decision = accounting.settle(
                Path(config["accounting"]["path"]),
                store_id=config["accounting"]["store_id"],
                objective_id=config["objective"]["id"],
                proposal_id=identity,
                outcome={"status": "succeeded", "receipt": {"result": result, "slot": slot}},
                lock_timeout=self._lock_timeout,
            )
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RuntimeError) as error:
            raise _MeterStopped(decision, "Bounded control request interrupted: " + str(error), started=True) from error
        return result, decision

    def _bound(self, config: Doc, kind: str, effect: Effect, method: str, request: Doc) -> int:
        bound = effect.bound_ms(method, request)
        store.require(
            type(bound) is int and 0 <= bound <= MAX_CALL_MS, "native call lacks a finite enforced whole-call bound"
        )
        scope = config["effects"][kind]
        store.require(
            bound <= scope["max_cost"]["wall_time_ms"], "native call exceeds its approved whole-call time scope"
        )
        if bound == 0:
            store.require(
                method in ("inspect", "recover") and "local-receipt-read-v1" in scope["capabilities"],
                "only explicitly approved purely local receipt reads may have zero wall reservation",
            )
        return int(bound)

    @staticmethod
    def _native_configuration(config: Doc, effect: Effect) -> None:
        store.require(
            _digest(effect.native_configuration_digest) == config["native_configuration_digest"],
            "actual native configuration differs from the independently approved standing pin",
        )

    def _native(
        self,
        config: Doc,
        kind: str,
        effect: Effect,
        method: str,
        request: Doc,
        authorization: Doc | None = None,
        *,
        historical: bool = False,
    ) -> Doc:
        scope = config["effects"][kind]

        def allowed() -> None:
            self._native_configuration(config, effect)
            availability = self._availability(config)
            if not availability["available"]:
                permitted = (
                    historical
                    and method == "recover"
                    and (
                        ("revoked" in availability["reason"] and scope["recover_after_revocation"])
                        or ("expired" in availability["reason"] and scope["recover_after_expiry"])
                    )
                )
                permitted = permitted or (
                    method == "cancel" and "revoked" in availability["reason"] and scope["cancel_inflight"]
                )
                store.require(permitted, availability["reason"])

        allowed()
        bound = self._bound(config, kind, effect, method, request)
        callback = getattr(effect, method, None)
        store.require(callable(callback), "approved bounded native control adapter is unavailable")
        assert callable(callback)
        correlation = digest({"kind": kind, "method": method, "request": request, "authorization": authorization})

        def call() -> Doc:
            # The installed adapter enforces this same bound around the complete process, not
            # separately per nested HTTP/Git call. Its real deadline tests establish that boundary.
            allowed()
            return _object(callback(request) if method == "inspect" else callback(request, authorization))

        result, _ = self._meter(
            config,
            purpose="native-" + method,
            operation_id=correlation,
            request=request,
            adapter_id=effect.adapter_id,
            wall_time_ms=bound,
            callback=call,
        )
        return result

    def call_metered(self, purpose: str, request: Any, *, operation_id: str) -> Doc:
        """Run one fresh approved verification read; repeated IDs consume another bounded slot."""
        decision: Doc | None = None
        try:
            _digest(operation_id)
            request = _object(request)
            with self._configuration() as config:
                store.require(
                    purpose in config["reads"] and purpose in self._readers,
                    "read-only operation is outside the independently approved standing scope",
                )
                available = self._availability(config)
                store.require(available["available"], available["reason"])
                scope = config["reads"][purpose]
                reader = self._readers[purpose]
                store.require(reader.adapter_id == scope["adapter_id"], "installed verification reader changed")
                fact_keys = {
                    "schema_version",
                    "repository",
                    "contract_digest",
                    "wall_time_ms",
                    "native_configuration_digest",
                }
                facts = _object(reader.inspect(request), fact_keys)
                store.require(
                    facts["schema_version"] == VERSION
                    and facts["repository"] == config["repository"]
                    and facts["contract_digest"] == scope["contract_digest"]
                    and facts["native_configuration_digest"] == config["native_configuration_digest"],
                    "verification read selects another approved repository or exact read contract",
                )
                bound = facts["wall_time_ms"]
                store.require(
                    type(bound) is int and 1 <= bound <= scope["max_wall_time_ms"],
                    "verification read lacks its approved enforced whole-call ceiling",
                )
                # Recheck immediately before reserving/releasing another external read.
                available = self._availability(config)
                store.require(available["available"], available["reason"])

                def read() -> Doc:
                    current = self._availability(config)
                    store.require(current["available"], current["reason"])
                    store.require(
                        _object(reader.inspect(request), fact_keys) == facts,
                        "approved native/read configuration changed after metered reservation",
                    )
                    return _object(reader.read(request, wall_time_ms=bound))

                result, decision = self._meter(
                    config,
                    purpose=purpose,
                    operation_id=operation_id,
                    request=request,
                    adapter_id=reader.adapter_id,
                    wall_time_ms=bound,
                    callback=read,
                )
                report = self._report(
                    purpose,
                    operation_id,
                    "completed",
                    "Fresh bounded verification read retained.",
                    decision,
                    {"result": result},
                )
                assert report["reservation"] is not None
                report["reservation"]["proposal_id"] = decision["request"]["proposal"]["id"]
                return report
        except _MeterStopped as error:
            decision = error.decision
            report = self._report(
                purpose, operation_id, "uncertain" if error.started else "denied", str(error), decision
            )
            assert report["reservation"] is not None
            report["reservation"]["proposal_id"] = decision["request"]["proposal"]["id"]
            return report
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RuntimeError) as error:
            return self._report(purpose, operation_id, "paused", "Metered request stopped: " + str(error), decision)

    def _scope(self, config: Doc, kind: str, effect: Effect, facts: Doc) -> str | None:
        scope = config["effects"][kind]
        if facts["preconditions"]["native_configuration_digest"] != config["native_configuration_digest"]:
            return "Native facts differ from the independently approved native configuration pin."
        for key in ("objective_id", "original_goal"):
            expected = config["objective"]["id" if key == "objective_id" else key]
            if facts[key] != expected:
                return "Native facts select another logical objective or original goal."
        if facts["action_id"] not in config["actions"]:
            return "Native action is outside the approved eligible action identities."
        if facts["contract"] != config["contract"]:
            return "Approved model, criteria, bindings, readiness, trust or observer contract changed."
        if facts["repository"] != config["repository"] or facts["base_revision"] != config["base_revision"]:
            return "Native source repository or approved base revision changed."
        if effect.adapter_id != scope["adapter_id"] or facts["native"]["store"] != scope["native_store"]:
            return "Native effect selects another installed adapter or canonical journal."
        if not set(facts["capabilities"]) <= set(scope["capabilities"]):
            return "Native effect requests an unapproved runtime capability."
        if any(facts["cost"][key] > scope["max_cost"][key] for key in accounting.DIMENSIONS):
            return "Native effect exceeds approved per-invocation resource bounds."
        for path in (*facts["source_paths"], *facts["write_paths"]):
            if not any(_within(path, allowed) for allowed in config["allowed_paths"]):
                return "Native worker path is outside the explicitly approved fixture scope."
            if any(_within(path, protected) for protected in (*PROTECTED_ROOTS, *config["protected_paths"])):
                return "Native worker path overlaps protected model, observer or policy source."
        if facts["destination"] is not None and facts["destination"] not in config["destinations"]:
            return "Native publication destination is outside the exact approved repository and refs."
        return None

    def _decision(self, config: Doc, effect_id: str) -> Doc | None:
        history = accounting.inspect(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
            lock_timeout=self._lock_timeout,
        )
        return next((item for item in history["decisions"] if item["request"]["proposal"]["id"] == effect_id), None)

    def _report(
        self,
        kind: str,
        effect_id: str,
        status: str,
        reason: str,
        decision: Doc | None = None,
        receipt: Doc | None = None,
    ) -> Doc:
        reservation = None
        if decision is not None:
            reservation = {
                "store_id": decision["store_id"],
                "objective_id": decision["objective_id"],
                "proposal_id": effect_id,
                "digest": decision["digest"],
                "status": decision["status"],
            }
        return {
            "schema_version": VERSION,
            "status": status,
            "kind": kind,
            "effect_id": effect_id,
            "reason": reason,
            "reservation": reservation,
            "receipt": receipt,
        }

    def _settle(self, config: Doc, kind: str, effect_id: str, decision: Doc, outcome: Doc) -> Doc:
        if outcome["status"] in ("succeeded", "failed", "cancelled"):
            decision = accounting.settle(
                Path(config["accounting"]["path"]),
                store_id=config["accounting"]["store_id"],
                objective_id=config["objective"]["id"],
                proposal_id=effect_id,
                outcome=outcome,
                lock_timeout=self._lock_timeout,
            )
            status = "completed" if outcome["status"] == "succeeded" else outcome["status"]
            return self._report(
                kind, effect_id, status, "Exact native terminal outcome retained.", decision, outcome["receipt"]
            )
        if outcome["status"] in ("not_started", "recoverable"):
            return self._report(
                kind,
                effect_id,
                outcome["status"],
                "Pinned native journal proves the exact permitted recovery state.",
                decision,
            )
        return self._report(kind, effect_id, "uncertain", outcome["reason"], decision)

    def _historical(self, config: Doc, kind: str, request: Doc, effect_id: str, effect: Effect, decision: Doc) -> Doc:
        saved = decision["request"]["proposal"]["effect"]
        store.require(
            saved["kind"] == kind and saved["request"] == request,
            "effect identity conflicts with the retained exact invocation",
        )
        store.require(
            saved["facts"]["native"]["store"] == config["effects"][kind]["native_store"]
            and saved["adapter_id"] == effect.adapter_id,
            "historical native operation selects another adapter or canonical journal",
        )
        if not decision["approved"]:
            return self._report(kind, effect_id, "denied", decision["reason"], decision)
        facts = validate_facts(saved["facts"])
        store.require(
            facts["preconditions"]["native_configuration_digest"] == config["native_configuration_digest"],
            "retained native configuration differs from the independently approved standing pin",
        )
        outcome = _outcome(
            self._native(config, kind, effect, "recover", request, _object(facts["authorization"]), historical=True),
            facts,
        )
        if decision["status"] == "settled":
            store.require(
                outcome["status"] in ("succeeded", "failed", "cancelled") and outcome == decision["outcome"],
                "native history conflicts with the retained settled outcome; never replay it as a new effect",
            )
        if outcome["status"] == "uncertain":
            availability = self._availability(config)
            scope = config["effects"][kind]
            cancel = getattr(effect, "cancel", None)
            if not availability["available"] and "revoked" in availability["reason"] and scope["cancel_inflight"]:
                store.require(callable(cancel), "approved cancellation adapter is unavailable")
                assert callable(cancel)
                outcome = _outcome(
                    self._native(config, kind, effect, "cancel", request, _object(facts["authorization"])), facts
                )
        return self._settle(config, kind, effect_id, decision, outcome)

    def reconcile(self, kind: str, request: Any, *, effect_id: str) -> Doc:
        """Recover historical facts without invoking a new effect, including after expiry or revocation."""
        return self._run(kind, request, effect_id=effect_id, invoke=False)

    def request_cancel(self, kind: str, request: Any, *, effect_id: str) -> Doc:
        """Request only a separately approved cancellation of one exact retained native grant.

        Cancellation deliberately does not wait on the objective launch mutex: its owner may be
        supervising the worker that must be stopped. Immutable config reads share their lock, and
        atomic accounting meters the native discovery/cancellation requests independently. No new
        execution grant is created and no absence/request-only response invents a terminal outcome.
        The controller's durable cancelled state prevents later phase scheduling.
        """
        decision: Doc | None = None
        try:
            _digest(effect_id)
            request = _object(request)
            with self._configuration() as config:
                store.require(
                    kind in config["effects"] and kind in self._effects, "cancellation adapter is not approved"
                )
                decision = self._decision(config, effect_id)
                store.require(
                    decision is not None and decision["approved"],
                    "cancellation requires an exact retained native grant",
                )
                assert decision is not None
                effect = self._effects[kind]
                saved = decision["request"]["proposal"]["effect"]
                scope = config["effects"][kind]
                store.require(
                    saved["kind"] == kind
                    and saved["request"] == request
                    and saved["adapter_id"] == effect.adapter_id == scope["adapter_id"]
                    and saved["facts"]["native"]["store"] == scope["native_store"],
                    "cancellation selects another native grant, adapter or canonical journal",
                )
                if decision["status"] == "settled":
                    return self._historical(config, kind, request, effect_id, effect, decision)
                store.require(
                    scope["cancel_inflight"] and "cancel-inflight-v1" in scope["capabilities"],
                    "native cancellation is outside the separately approved recovery scope",
                )
                facts = validate_facts(saved["facts"])
                store.require(
                    facts["preconditions"]["native_configuration_digest"] == config["native_configuration_digest"],
                    "retained cancellation configuration differs from the independently approved standing pin",
                )
                outcome = _outcome(
                    self._native(
                        config, kind, effect, "recover", request, _object(facts["authorization"]), historical=True
                    ),
                    facts,
                )
                if outcome["status"] in ("succeeded", "failed", "cancelled"):
                    return self._settle(config, kind, effect_id, decision, outcome)
                if outcome["status"] == "not_started":
                    return self._report(
                        kind,
                        effect_id,
                        "paused",
                        "No native intent exists to cancel; preserve the reservation "
                        "and durable workflow cancellation.",
                        decision,
                    )
                outcome = _outcome(
                    self._native(config, kind, effect, "cancel", request, _object(facts["authorization"])), facts
                )
                store.require(
                    outcome["status"] not in ("not_started", "recoverable"),
                    "native cancellation did not establish a retained outcome",
                )
                return self._settle(config, kind, effect_id, decision, outcome)
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RuntimeError) as error:
            return self._report(
                kind,
                effect_id,
                "uncertain" if decision is not None and decision["approved"] else "paused",
                "Cancellation stopped: " + str(error),
                decision,
            )

    def execute(self, kind: str, request: Any, *, effect_id: str) -> Doc:
        """Evaluate exact native facts, reserve once, and invoke only under current approved scope."""
        return self._run(kind, request, effect_id=effect_id, invoke=True)

    def _run(self, kind: str, request: Any, *, effect_id: str, invoke: bool) -> Doc:
        decision: Doc | None = None
        try:
            _digest(effect_id)
            request = _object(request)
            with self._configuration() as config, self._objective_operation(config):
                store.require(
                    kind in ADAPTERS and kind in config["effects"] and kind in self._effects,
                    "effect is outside the independently approved standing scope",
                )
                effect = self._effects[kind]
                store.require(
                    effect.adapter_id == config["effects"][kind]["adapter_id"], "installed native adapter changed"
                )
                decision = self._decision(config, effect_id)
                repeat_invocation = decision is not None
                if decision is not None:
                    recovered = self._historical(config, kind, request, effect_id, effect, decision)
                    if recovered["status"] not in ("not_started", "recoverable") or not invoke:
                        return recovered
                    conflict = self._unresolved_write(config, excluding=effect_id)
                    if conflict is not None:
                        return self._report(kind, effect_id, "paused", conflict, decision)
                    saved = decision["request"]["proposal"]["effect"]
                    store.require(
                        saved["delegation_digest"] == self._approved_digest,
                        "changed standing contract cannot reuse an old grant for a new invocation",
                    )
                    facts = saved["facts"]
                    prepared_recovery = recovered["status"] == "recoverable"
                    if prepared_recovery:
                        store.require(
                            "finish-prepared-v1" in config["effects"][kind]["capabilities"],
                            "prepared native recovery requires separate standing scope",
                        )
                else:
                    prepared_recovery = False
                    conflict = self._unresolved_write(config) if invoke else None
                    if conflict is not None:
                        return self._report(kind, effect_id, "paused", conflict)
                    availability = self._availability(config)
                    if invoke and not availability["available"]:
                        return self._report(kind, effect_id, "denied", availability["reason"])
                    facts = validate_facts(self._native(config, kind, effect, "inspect", request))
                    if not invoke:
                        reason = self._scope(config, kind, effect, facts)
                        if reason is not None:
                            return self._report(kind, effect_id, "paused", reason)
                        outcome = _outcome(
                            self._native(config, kind, effect, "recover", request, _object(facts["authorization"])),
                            facts,
                        )
                        if outcome["status"] == "not_started":
                            return self._report(
                                kind,
                                effect_id,
                                "not_started",
                                "Native journal and accounting prove no authorized invocation started.",
                            )
                        return self._report(
                            kind,
                            effect_id,
                            "paused",
                            "Native operation has no retained standing decision; explicit review is required.",
                        )
                    availability = self._availability(config)
                    reason = self._scope(config, kind, effect, facts)
                    if not availability["available"]:
                        reason = availability["reason"]
                    if reason is None:
                        history = accounting.inspect(
                            Path(config["accounting"]["path"]),
                            store_id=config["accounting"]["store_id"],
                            objective_id=config["objective"]["id"],
                            lock_timeout=self._lock_timeout,
                        )
                        alias = any(
                            item["approved"]
                            and item["request"]["proposal"]["effect"].get("facts", {}).get("native") == facts["native"]
                            for item in history["decisions"]
                        )
                        if alias:
                            reason = (
                                "Native operation already has another stable standing effect identity; reconcile it."
                            )
                        else:
                            discovered = _outcome(
                                self._native(config, kind, effect, "recover", request, _object(facts["authorization"])),
                                facts,
                            )
                            if discovered["status"] != "not_started":
                                reason = (
                                    "Preexisting native operation has no retained standing decision; "
                                    "review its original authority."
                                )
                    if reason is None and facts["cost"]["wall_time_ms"] < self._bound(
                        config, kind, effect, "invoke", request
                    ):
                        reason = "Native reservation undercharges the actually enforced whole-call deadline."
                    proposal = {
                        "schema_version": VERSION,
                        "id": effect_id,
                        "delegation_id": config["id"],
                        "policy_digest": self._approved_digest,
                        "effect": {
                            "kind": kind,
                            "adapter_id": effect.adapter_id,
                            "request": request,
                            "facts": facts,
                            "delegation_digest": self._approved_digest,
                            "delegation": config,
                        },
                        "preconditions": facts["preconditions"],
                        "authorization": facts["authorization"],
                        "cost": facts["cost"],
                    }
                    decision = accounting.reserve(
                        Path(config["accounting"]["path"]),
                        store_id=config["accounting"]["store_id"],
                        objective_id=config["objective"]["id"],
                        proposal=proposal,
                        approved=reason is None,
                        reason=reason or "Exact native effect is within independently approved standing scope.",
                        lock_timeout=self._lock_timeout,
                    )
                    if not decision["approved"]:
                        return self._report(kind, effect_id, "denied", decision["reason"], decision)
                decision = accounting.mark_uncertain(
                    Path(config["accounting"]["path"]),
                    store_id=config["accounting"]["store_id"],
                    objective_id=config["objective"]["id"],
                    proposal_id=effect_id,
                    reason=UNCERTAIN_REASON,
                    lock_timeout=self._lock_timeout,
                )
                current = (
                    facts
                    if prepared_recovery
                    else validate_facts(self._native(config, kind, effect, "inspect", request))
                )
                reason = self._scope(config, kind, effect, current)
                availability = self._availability(config)
                if current != facts:
                    reason = (
                        "Native facts, current preconditions or exact grant changed after reservation; "
                        "replan explicitly."
                    )
                if not availability["available"]:
                    reason = availability["reason"]
                if reason is not None:
                    return self._report(kind, effect_id, "paused", reason, decision)
                if prepared_recovery:
                    resume = getattr(effect, "resume", None)
                    store.require(callable(resume), "approved native prepared-recovery adapter is unavailable")
                    assert callable(resume)
                    result = self._native(config, kind, effect, "resume", request, _object(facts["authorization"]))
                elif repeat_invocation:
                    # Not-started proof establishes native absence, not that the original call
                    # spent no time before its intent. Another invocation needs fresh capacity.
                    result = self._native(config, kind, effect, "invoke", request, _object(facts["authorization"]))
                else:
                    self._native_configuration(config, effect)
                    store.require(
                        facts["cost"]["wall_time_ms"] >= self._bound(config, kind, effect, "invoke", request),
                        "native reservation undercharges the actually enforced whole-call deadline",
                    )
                    result = effect.invoke(request, _object(facts["authorization"]))
                outcome = _outcome(result, facts)
                store.require(
                    outcome["status"] not in ("not_started", "recoverable"),
                    "invoke or resume cannot substitute historical proof for an outcome",
                )
                return self._settle(config, kind, effect_id, decision, outcome)
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RuntimeError) as error:
            # A callback may have sent its request before failing. Never refund, infer success from
            # output, or invoke again here. Recovery uses the same retained native grant and journal.
            return self._report(
                kind,
                effect_id,
                "uncertain" if decision is not None and decision["approved"] else "paused",
                "Standing effect stopped: " + str(error),
                decision,
            )
