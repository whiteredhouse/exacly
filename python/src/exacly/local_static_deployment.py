"""A fixed, private, one-host static deployment owner; no application code is executed.

The host operator and filesystem are trusted. A directory flock serializes this owner, not arbitrary
external writers. Immutable intent/rollout files precede a conditional atomic active-pointer switch.
An incomplete write is discovered, never retried by reconciliation. This is not a cloud deployer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from exacly import delivery_automation as delivery
from exacly import execution_store as store

Doc = dict[str, Any]
PROFILE = "local-static-deployment-owner-v1"
KEY_MAGIC = b"EXACLY STATIC DEPLOYMENT OWNER KEY v1\n"
KEY_DOMAIN = b"exacly.static.deployment.owner.receipt.v1\0"
ACTIVATION_DOMAIN = b"exacly.static.deployment.owner.active-ancestry.v1\0"
MAX_BYTES = 16 * 1024 * 1024
MAX_HISTORY = 4096


def require(value: Any, reason: str) -> None:
    if not value:
        raise ValueError(reason)


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    content = store.canonical(value).encode()
    require(len(content) <= MAX_BYTES, "deployment document exceeds its fixed byte ceiling")
    return content


def _read(path: Path, maximum: int = MAX_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "deployment files must be owned private regular single-link files",
        )
        require(info.st_size <= maximum, "deployment file exceeds its fixed byte ceiling")
        content = os.read(fd, maximum + 1)
        require(len(content) <= maximum and len(content) == info.st_size, "deployment file changed while reading")
        return content
    finally:
        os.close(fd)


def _document(path: Path) -> Doc:
    value = json.loads(
        _read(path).decode(), object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant
    )
    require(isinstance(value, dict), "deployment record must be an object")
    return cast(Doc, value)


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    _sync(path.parent)


def _replace(path: Path, value: Doc) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".active-", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(_canonical(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        _sync(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def initialize_key(path: Path) -> str:
    """Create a distinct owning-workflow receipt key, outside any source tree."""
    with store.locked(path, write=True) as (absolute, _):
        _create(absolute, KEY_MAGIC + secrets.token_bytes(32))
        return _bytes_digest(_read(absolute, len(KEY_MAGIC) + 32))


def _key(path: Path, key_id: str) -> bytes:
    store.location(path)
    value = _read(path, len(KEY_MAGIC) + 32)
    require(value.startswith(KEY_MAGIC) and len(value) == len(KEY_MAGIC) + 32, "wrong deployment owner key purpose")
    require(hmac.compare_digest(_bytes_digest(value), key_id), "deployment owner key identity differs")
    return value[len(KEY_MAGIC) :]


def _sign(value: Doc, key: Doc, *, domain: bytes = KEY_DOMAIN) -> Doc:
    body = {**value, "key_id": key["id"]}
    return {
        **body,
        "signature": "sha256:"
        + hmac.new(_key(Path(key["path"]), key["id"]), domain + _canonical(body), hashlib.sha256).hexdigest(),
    }


def verify_record(value: Doc, key: Doc) -> Doc:
    """Authenticate an exact retained owner record. This says nothing about runtime health."""
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "profile",
            "owner_id",
            "generation",
            "parent_generation",
            "rollout_id",
            "effect_nonce",
            "stage",
            "intent_id",
            "request_digest",
            "payload_digest",
            "authority_digest",
            "subject",
            "source",
            "created_at",
            "key_id",
            "signature",
        },
        "invalid owning workflow receipt fields",
    )
    require(value["profile"] == PROFILE and value["key_id"] == key["id"], "owning workflow receipt identity differs")
    signature = value["signature"]
    expected = _sign({name: item for name, item in value.items() if name not in ("signature", "key_id")}, key)
    require(
        isinstance(signature, str) and hmac.compare_digest(signature, expected["signature"]),
        "owning workflow receipt authentication failed",
    )
    delivery._schema(value["source"], delivery.SOURCE)
    require(value["stage"] in ("deploy", "rollback"), "invalid owning workflow stage")
    delivery._schema(value["subject"], delivery.SUBJECTS[value["stage"]])
    for field in (
        "generation",
        "parent_generation",
        "rollout_id",
        "intent_id",
        "request_digest",
        "payload_digest",
        "authority_digest",
    ):
        require(
            isinstance(value[field], str) and bool(store.DIGEST.fullmatch(value[field])),
            "invalid owning workflow record identity",
        )
    require(
        isinstance(value["effect_nonce"], str)
        and len(value["effect_nonce"]) == 32
        and all(character in "0123456789abcdef" for character in value["effect_nonce"]),
        "invalid owning workflow effect nonce",
    )
    delivery._timestamp(value["created_at"])
    require(
        isinstance(value["owner_id"], str) and uuid.UUID(hex=value["owner_id"]).hex == value["owner_id"],
        "invalid owner identity",
    )
    require(
        value["generation"] == delivery.digest({"intent": value["intent_id"], "effect_nonce": value["effect_nonce"]})
        and value["rollout_id"] == delivery.digest({"owner": value["owner_id"], "intent": value["intent_id"]})
        and value["parent_generation"] == value["subject"]["expected_generation"],
        "owning workflow generation or parent identity disagrees",
    )
    return value


def activation_fields(value: Doc, record: Doc, key_id: str) -> Doc:
    """Pure retained-field consistency; this does not authenticate the owner's signature."""
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "profile",
            "kind",
            "owner_id",
            "intent_id",
            "generation",
            "record_digest",
            "active_generation",
            "observed_at",
            "key_id",
            "signature",
        },
        "applied deployment lacks a closed activation attestation",
    )
    require(
        value["profile"] == PROFILE
        and value["kind"] == "active-ancestry"
        and value["key_id"] == key_id
        and all(value[name] == record[name] for name in ("owner_id", "intent_id", "generation"))
        and value["record_digest"] == delivery.digest(record),
        "activation attestation differs from the exact deployment record",
    )
    require(
        isinstance(value["active_generation"], str) and bool(store.DIGEST.fullmatch(value["active_generation"])),
        "activation attestation lacks actual active generation",
    )
    require(
        isinstance(value["signature"], str) and bool(store.DIGEST.fullmatch(value["signature"])),
        "activation attestation signature has an invalid shape",
    )
    require(
        delivery._timestamp(record["created_at"]) <= delivery._timestamp(value["observed_at"]),
        "activation attestation predates its rollout",
    )
    return value


def verify_activation(value: Doc, record: Doc, key: Doc) -> Doc:
    """Authenticate the owner's post-read active-ancestry statement, not a prepared record."""
    activation_fields(value, record, key["id"])
    expected = _sign(
        {name: item for name, item in value.items() if name not in ("key_id", "signature")},
        key,
        domain=ACTIVATION_DOMAIN,
    )
    require(
        isinstance(value["signature"], str) and hmac.compare_digest(value["signature"], expected["signature"]),
        "activation attestation authentication failed",
    )
    return value


def initialize(root: Path, *, owner_id: str, application: str, environment: str, workflow: Doc) -> None:
    """Explicit bootstrap of a new empty private owner; never resets an existing environment."""
    require(uuid.UUID(hex=owner_id).hex == owner_id, "invalid owner identity")
    delivery._schema(workflow, delivery.WORKFLOW)
    require(
        all(isinstance(item, str) and 0 < len(item) <= 1024 for item in (application, environment)),
        "invalid environment identity",
    )
    with store.locked(root / "owner.lock", write=True) as (_, _fd):
        require(not any(root.iterdir()), "deployment owner root must be empty at initialization")
        _create(
            root / "identity.json",
            _canonical(
                {
                    "profile": PROFILE,
                    "id": owner_id,
                    "application": application,
                    "environment": environment,
                    "workflow": workflow,
                }
            ),
        )
        _create(root / "active.json", _canonical({"generation": delivery.digest({"owner": owner_id, "empty": True})}))


class LocalStaticOwner:
    """The independently selected owning workflow. Callers must authenticate current authority.

    This trusted Python interface is not an untrusted command runner. The public delivery adapter
    validates its separately selected approval pin immediately before calling ``apply``.
    """

    def __init__(
        self,
        root: Path,
        owner_id: str,
        key: Doc,
        *,
        approval_pin: delivery.ApprovalPin | None = None,
        approval_context: Doc | None = None,
        adapter_identity: Doc | None = None,
    ) -> None:
        self.root, self.owner_id, self.key = root, owner_id, dict(key)
        self._pin, self._context, self._adapter_identity = approval_pin, approval_context, adapter_identity

    def _authority(self, intent: Doc, authorization: Doc) -> None:
        require(
            self._pin is not None and self._context is not None,
            "owning workflow requires independently selected current approval",
        )
        assert self._pin is not None and self._context is not None
        approved = delivery.validate_approval(authorization, pin=self._pin, now=dt.datetime.now(dt.UTC))
        delivery.validate_intent(intent, approval=approved)
        require(
            approved["adapter"] == self._adapter_identity
            and approved["policy_digest"] == self._context["policy_digest"]
            and approved["contract"] == self._context["contract"],
            "owning workflow current approval/configuration differs",
        )

    def _active_chain(self, *, deadline: store.Deadline) -> set[str]:
        current = self._current()["rollout"]
        initial = delivery.digest({"owner": self.owner_id, "empty": True})
        visited: set[str] = set()
        while current is not None:
            deadline.check()
            require(
                current["generation"] not in visited and len(visited) < MAX_HISTORY,
                "deployment history exceeds its fixed bound",
            )
            visited.add(current["generation"])
            current = None if current["parent_generation"] == initial else self._record(current["parent_generation"])
        return visited

    def _fenced(self, *, deadline: store.Deadline, failure_report_id: str | None = None) -> None:
        active = self._active_chain(deadline=deadline)
        pending = list(self.root.glob("*.intent.json"))
        require(len(pending) < MAX_HISTORY, "owning workflow intent ceiling exhausted")
        for path in pending:
            deadline.check()
            retained = _document(path)
            require(set(retained) == {"intent", "payload", "authorization"}, "invalid retained owning workflow intent")
            if failure_report_id is not None and retained["payload"]["failure_observation"] is not None:
                require(
                    retained["payload"]["failure_observation"]["id"] != failure_report_id,
                    "rollback failure observation has already been consumed",
                )
            generation = delivery.digest(
                {"intent": retained["intent"]["id"], "effect_nonce": retained["payload"]["effect_nonce"]}
            )
            require(
                generation in active,
                "an unresolved owning workflow intent fences this environment; discover before recovery",
            )

    @contextmanager
    def _locked(self, *, write: bool, deadline: store.Deadline) -> Iterator[Doc]:
        deadline.check()
        with store.locked(self.root / "owner.lock", write=write, deadline=deadline):
            identity = _document(self.root / "identity.json")
            require(
                set(identity) == {"profile", "id", "application", "environment", "workflow"}
                and identity["profile"] == PROFILE
                and identity["id"] == self.owner_id,
                "deployment owner identity changed",
            )
            _key(Path(self.key["path"]), self.key["id"])
            yield identity
            deadline.check()

    def _record(self, generation: str) -> Doc:
        require(bool(store.DIGEST.fullmatch(generation)), "invalid active deployment generation")
        record = verify_record(_document(self.root / (generation[7:] + ".rollout.json")), self.key)
        require(
            record["generation"] == generation and record["owner_id"] == self.owner_id,
            "deployment record was substituted in its generation slot",
        )
        return record

    def _attestation(self, record: Doc, active_generation: str) -> Doc:
        # Only called while holding the owner lock after reading actual active state/ancestry.
        return _sign(
            {
                "profile": PROFILE,
                "kind": "active-ancestry",
                "owner_id": self.owner_id,
                "intent_id": record["intent_id"],
                "generation": record["generation"],
                "record_digest": delivery.digest(record),
                "active_generation": active_generation,
                "observed_at": dt.datetime.now(dt.UTC).isoformat(),
            },
            self.key,
            domain=ACTIVATION_DOMAIN,
        )

    def _current(self) -> Doc:
        pointer = _document(self.root / "active.json")
        require(
            set(pointer) == {"generation"} and isinstance(pointer["generation"], str),
            "invalid active deployment pointer",
        )
        initial = delivery.digest({"owner": self.owner_id, "empty": True})
        if pointer["generation"] == initial:
            return {"generation": initial, "rollout": None}
        record = self._record(pointer["generation"])
        require(record["owner_id"] == self.owner_id, "active record belongs to another owner")
        return {"generation": pointer["generation"], "rollout": record}

    def snapshot(self, *, deadline: store.Deadline, require_unfenced: bool = False) -> Doc:
        with self._locked(write=False, deadline=deadline):
            if require_unfenced:
                self._fenced(deadline=deadline)
            return self._current()

    def discover(self, intent_id: str, *, deadline: store.Deadline) -> tuple[Doc, Doc] | None:
        """Read-only discovery, including authenticated ancestors; absence grants no retry."""
        with self._locked(write=False, deadline=deadline):
            snapshot = self._current()
            current = snapshot["rollout"]
            visited: set[str] = set()
            initial = delivery.digest({"owner": self.owner_id, "empty": True})
            while current is not None:
                deadline.check()
                require(
                    current["generation"] not in visited and len(visited) < MAX_HISTORY,
                    "deployment history is cyclic or exceeds bounded discovery",
                )
                visited.add(current["generation"])
                if current["intent_id"] == intent_id:
                    return cast(Doc, current), self._attestation(current, snapshot["generation"])
                current = (
                    None if current["parent_generation"] == initial else self._record(current["parent_generation"])
                )
            return None

    def prior(self, generation: str, *, deadline: store.Deadline) -> Doc:
        """Return an actual ancestor, never an unactivated staged rollout file."""
        with self._locked(write=False, deadline=deadline):
            return self._prior(generation, deadline=deadline)

    def _prior(self, generation: str, *, deadline: store.Deadline) -> Doc:
        current = self._current()["rollout"]
        initial = delivery.digest({"owner": self.owner_id, "empty": True})
        for _ in range(MAX_HISTORY):
            deadline.check()
            require(current is not None, "selected prior rollout was never active")
            if current["generation"] == generation:
                return cast(Doc, current)
            current = None if current["parent_generation"] == initial else self._record(current["parent_generation"])
        raise ValueError("deployment history exceeds bounded discovery")

    def apply(
        self,
        intent: Doc,
        payload: Doc,
        authorization: Doc,
        content: bytes,
        *,
        deadline: store.Deadline,
        precommit: Callable[[Doc, Doc | None], None] | None = None,
    ) -> tuple[Doc, Doc]:
        """Persist intent and exact bytes, then CAS the active generation once.

        No retry is performed after an exception. A crash at any write leaves private retained facts;
        only active-chain discovery can establish that a generation became active.
        """
        request, subject = intent["request"], intent["request"]["subject"]
        require(
            delivery.digest(payload) == request["native_payload_digest"], "owner received a substituted native payload"
        )
        require(request["stage"] in ("deploy", "rollback"), "owner requires separate deployment or rollback authority")
        artifact = subject["artifact"]
        require(
            len(content) <= MAX_BYTES
            and len(content) == artifact["size"]
            and _bytes_digest(content) == artifact["digest"],
            "deployment bytes differ from approved artifact",
        )
        with self._locked(write=True, deadline=deadline) as identity:
            self._authority(intent, authorization)
            require(
                all(subject[name] == identity[name] for name in ("application", "environment", "workflow")),
                "deployment subject differs from independently selected owner",
            )
            current = self._current()
            require(
                current["generation"] == subject["expected_generation"], "deployment generation precondition changed"
            )
            failure = payload["failure_observation"]
            self._fenced(deadline=deadline, failure_report_id=None if failure is None else failure["id"])
            prior = None
            if request["stage"] == "rollback":
                require(
                    precommit is not None, "rollback needs a separately installed current failure/precondition check"
                )
                prior = self._prior(payload["rollback"]["prior_generation"], deadline=deadline)
            if precommit is not None:
                precommit(current, prior)
            retained = {"intent": intent, "payload": payload, "authorization": authorization}
            intent_path = self.root / (intent["id"][7:] + ".intent.json")
            require(not os.path.lexists(intent_path), "deployment intent already retained; reconcile before any retry")
            _create(intent_path, _canonical(retained))
            deadline.check()
            slot = self.root / (artifact["digest"][7:] + ".artifact")
            if os.path.lexists(slot):
                require(_read(slot) == content, "immutable artifact slot differs")
            else:
                _create(slot, content)
            record = _sign(
                {
                    "profile": PROFILE,
                    "owner_id": self.owner_id,
                    "generation": delivery.digest({"intent": intent["id"], "effect_nonce": payload["effect_nonce"]}),
                    "parent_generation": current["generation"],
                    "rollout_id": delivery.digest({"owner": self.owner_id, "intent": intent["id"]}),
                    "effect_nonce": payload["effect_nonce"],
                    "stage": request["stage"],
                    "intent_id": intent["id"],
                    "request_digest": delivery.digest(request),
                    "payload_digest": delivery.digest(payload),
                    "authority_digest": delivery.digest(authorization),
                    "subject": subject,
                    "source": payload["source"],
                    "created_at": dt.datetime.now(dt.UTC).isoformat(),
                },
                self.key,
            )
            _create(self.root / (record["generation"][7:] + ".rollout.json"), _canonical(record))
            deadline.check()
            # Active generation and the new record are covered by the same exclusive owner lock.
            self._authority(intent, authorization)
            if precommit is not None:
                precommit(current, prior)
            # Verification hashes installed observer inputs. Recheck actual time after that work.
            self._authority(intent, authorization)
            if request["stage"] == "rollback":
                current_time = dt.datetime.now(dt.UTC)
                require(
                    current_time < delivery._timestamp(payload["rollback"]["expires_at"])
                    and current_time < delivery._timestamp(payload["failure_observation"]["window"]["expires_at"]),
                    "rollback authority or failure observation expired before CAS",
                )
            deadline.check()
            _replace(self.root / "active.json", {"generation": record["generation"]})
            actual = self._current()
            require(
                actual["generation"] == record["generation"] and actual["rollout"] == record,
                "owner cannot confirm its actual active generation after CAS",
            )
            return record, self._attestation(record, actual["generation"])

    def served(self, *, deadline: store.Deadline) -> tuple[Doc, bytes]:
        with self._locked(write=False, deadline=deadline):
            record = self._current()["rollout"]
            require(record is not None, "environment has no active artifact")
            artifact = record["subject"]["artifact"]
            content = _read(self.root / (artifact["digest"][7:] + ".artifact"))
            require(
                len(content) == artifact["size"] and _bytes_digest(content) == artifact["digest"],
                "active artifact bytes changed",
            )
            return record, content


class StaticDeploymentServer(ThreadingHTTPServer):
    """Serve exact active bytes on a single loopback endpoint; no artifact code is executed."""

    daemon_threads = True
    block_on_close = True

    def __init__(self, owner: LocalStaticOwner, address: tuple[str, int] = ("127.0.0.1", 0)) -> None:
        require(address[0] == "127.0.0.1", "static deployment reference binds loopback only")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/artifact":
                    self.send_error(404)
                    return
                try:
                    record, content = owner.served(deadline=store.Deadline(5))
                except (ValueError, OSError, store.StoreError):
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                for header, value in (
                    ("Generation", record["generation"]),
                    ("Rollout-Id", record["rollout_id"]),
                    ("Effect-Nonce", record["effect_nonce"]),
                    ("Source-Revision", record["source"]["revision"]),
                ):
                    self.send_header("X-Exacly-" + header, value)
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        super().__init__(address, Handler)
