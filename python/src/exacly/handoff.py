"""Reviewed local publication of retained executor artifacts as an exact Git revision.

The operator owns this boundary. Candidate files supply bytes, never repository configuration,
commands, publication authority or completion evidence. Prepared object bytes are durable before
one local ref can change; recovery needs neither the candidate bundle nor the current model.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exacly import execution_store as store
from exacly import executor_contract, executor_store, handoff_git, handoff_store, isolation, operation_locks, planning

Doc = dict[str, Any]
VERSION = "0.1.0"
ADAPTER = "local-git-v1"
CLAIMS = {
    "artifacts_to_git_verified": True,
    "evidence_admitted": False,
    "completion_authorized": False,
    "remote_pushed": False,
    "binding_changed": False,
}
_REQUEST_FIELDS = {
    "policy",
    "execution_id",
    "attempt",
    "base_revision",
    "ref",
    "expected_revision",
    "mappings",
    "message",
    "author",
}


@dataclass(frozen=True)
class Request:
    policy: Doc
    execution_id: str
    attempt: int
    base_revision: str
    ref: str
    expected_revision: str | None
    mappings: Mapping[str, str]
    message: str
    author: Doc


def _require(value: Any, message: str) -> None:
    store.require(value, message)


def _path(value: Any) -> Path:
    _require(isinstance(value, str) and "\0" not in value, "handoff storage needs canonical absolute paths")
    path = Path(value)
    _require(path.is_absolute() and str(path) == value and ".." not in path.parts, "invalid handoff storage path")
    return path


def _uuid(value: Any) -> None:
    try:
        identity = uuid.UUID(hex=value)
        _require(identity.hex == value and identity.version == 4, "invalid handoff storage instance identity")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid handoff storage instance identity") from None


def validate_policy(value: Any, *, check_storage: bool = True) -> Doc:
    try:
        _require(
            isinstance(value, dict)
            and set(value) == {"schema_version", "name", "version", "adapter", "storage", "allowed_paths"}
            and value["schema_version"] == VERSION
            and value["adapter"] == ADAPTER
            and all(isinstance(value[key], str) and value[key] for key in ("name", "version")),
            "invalid local Git handoff policy",
        )
        storage = value["storage"]
        _require(
            isinstance(storage, dict)
            and set(storage)
            == {"journal", "journal_id", "executor_journal", "executor_journal_id", "repository", "repository_id"},
            "invalid handoff storage scope",
        )
        for key in ("journal_id", "executor_journal_id", "repository_id"):
            _uuid(storage[key])
        paths = {key: _path(storage[key]) for key in ("journal", "executor_journal", "repository")}
        names = [
            str(path) + suffix
            for key, path in paths.items()
            if key != "repository"
            for suffix in ("", "-journal", "-wal", "-shm")
        ]
        _require(len(set(names)) == 8, "handoff and executor journals overlap")
        repository = paths["repository"]
        _require(
            not any(
                path == repository or repository in path.parents or path in repository.parents
                for key, path in paths.items()
                if key != "repository"
            ),
            "handoff repository overlaps protected journal storage",
        )
        allowed = value["allowed_paths"]
        _require(
            isinstance(allowed, list) and 0 < len(allowed) <= isolation.MAX_ARTIFACT_FILES,
            "invalid permitted handoff paths",
        )
        _require(
            all(isinstance(name, str) for name in allowed) and len(set(allowed)) == len(allowed),
            "ambiguous permitted handoff paths",
        )
        for name in allowed:
            handoff_git.relative_path(name[:-1] if name.endswith("/") else name)
        if check_storage:
            for path in paths.values():
                _require(store.location(path) == path, "handoff storage path changed")
        return copy.deepcopy(value)
    except (KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("invalid local Git handoff policy") from error


def _request(request: Request) -> Doc:
    _require(isinstance(request, Request), "an explicit handoff Request is required")
    policy = validate_policy(request.policy, check_storage=False)
    _require(
        isinstance(request.execution_id, str) and store.DIGEST.fullmatch(request.execution_id),
        "invalid source execution identity",
    )
    _require(
        type(request.attempt) is int and 1 <= request.attempt <= executor_store.MAX_ATTEMPTS, "invalid source attempt"
    )
    _require(
        isinstance(request.base_revision, str) and re.fullmatch(r"[0-9a-f]{40}", request.base_revision),
        "handoff needs an exact base revision",
    )
    handoff_git.reference(request.ref)
    _require(
        request.expected_revision is None or request.expected_revision == request.base_revision,
        "an existing handoff branch must still name the approved base",
    )
    _require(
        isinstance(request.mappings, Mapping) and 0 < len(request.mappings) <= isolation.MAX_ARTIFACT_FILES,
        "invalid handoff artifact mapping",
    )
    destinations: list[str] = []
    for source, destination in request.mappings.items():
        _require(
            isinstance(source, str) and source.startswith("artifacts/"),
            "handoff source must be a retained artifact path",
        )
        executor_contract.relative(source)
        handoff_git.relative_path(destination)
        _require(
            any(
                destination == name or (name.endswith("/") and destination.startswith(name))
                for name in policy["allowed_paths"]
            ),
            "handoff destination is outside permitted policy paths",
        )
        destinations.append(destination)
    _require(len(set(destinations)) == len(destinations), "handoff destinations are ambiguous")
    _require(
        not any(left != right and right.startswith(left + "/") for left in destinations for right in destinations),
        "handoff file and directory destinations overlap",
    )
    _require(
        isinstance(request.message, str)
        and 0 < len(request.message.encode()) <= 64 * 1024
        and "\0" not in request.message,
        "invalid handoff commit message",
    )
    _require(
        isinstance(request.author, dict) and set(request.author) == {"name", "email", "time"},
        "invalid handoff commit author",
    )
    # The Git codec checks exact identity syntax before a proposal is retained.
    _require(type(request.author["time"]) is int, "handoff commit time must be an integer")
    return {
        "schema_version": VERSION,
        "policy": policy,
        "execution_id": request.execution_id,
        "attempt": request.attempt,
        "base_revision": request.base_revision,
        "ref": request.ref,
        "expected_revision": request.expected_revision,
        "mappings": dict(request.mappings),
        "message": request.message,
        "author": copy.deepcopy(request.author),
    }


def request_from_document(value: Any) -> Request:
    _require(
        isinstance(value, dict)
        and set(value) == {"schema_version", *_REQUEST_FIELDS}
        and value["schema_version"] == VERSION,
        "invalid handoff request document",
    )
    request = Request(**{key: copy.deepcopy(value[key]) for key in _REQUEST_FIELDS})
    _request(request)
    return request


def _summary(prepared: Doc) -> Doc:
    return copy.deepcopy({key: value for key, value in prepared.items() if key != "objects"})


def _identity(contract: Doc) -> Doc:
    request = contract["request"]
    return {
        "schema_version": VERSION,
        "adapter": ADAPTER,
        "execution_id": request["execution_id"],
        "attempt": request["attempt"],
        "source_receipt": contract["source"]["receipt"]["digest"],
        "repository_id": contract["repository"]["instance_id"],
        "base_revision": request["base_revision"],
        "ref": request["ref"],
    }


def _authorization(journal: handoff_store.Journal, contract: Doc) -> Doc:
    identity = _identity(contract)
    request = contract["request"]
    return {
        "schema_version": VERSION,
        "handoff_id": planning.digest(identity),
        "identity": identity,
        "effect": "publish_local_git_ref",
        "journal": {"path": request["policy"]["storage"]["journal"], "instance_id": journal.journal_id},
        "repository": contract["repository"],
        "base_revision": request["base_revision"],
        "ref": request["ref"],
        "expected_revision": request["expected_revision"],
        "source_receipt": contract["source"]["receipt"]["digest"],
        "build_id": contract["source"]["receipt"]["build"]["id"],
        "tree": contract["prepared"]["tree"],
        "commit": contract["prepared"]["commit"],
        "request_digest": planning.digest(request),
        "contract_digest": planning.digest(contract),
    }


def _report(
    operation: str, status: str, reason: str, *, identity: str | None = None, dry_run: bool = False, **values: Any
) -> Doc:
    report = {
        "schema_version": VERSION,
        "operation": operation,
        "dry_run": dry_run,
        "status": status,
        "handoff_id": identity,
        "reason": reason,
        "authorization_template": None,
        "receipt": None,
        "claims": {**CLAIMS, "artifacts_to_git_verified": status == "published"},
        **values,
    }
    report["digest"] = planning.digest(report)
    return report


def _receipt(record: Doc) -> Doc:
    contract = record["intent"]["contract"]
    request, package, source = contract["request"], contract["prepared"], contract["source"]
    value = {
        "schema_version": VERSION,
        "handoff_id": record["intent"]["id"],
        "authorization": copy.deepcopy(record["intent"]["authorization"]),
        "source_receipt": source["receipt"]["digest"],
        "build": copy.deepcopy(source["receipt"]["build"]),
        "repository": copy.deepcopy(contract["repository"]),
        "base_revision": request["base_revision"],
        "ref": request["ref"],
        "expected_revision": request["expected_revision"],
        "tree": package["tree"],
        "commit": package["commit"],
        "target": {**source["input_target"], "revision": package["commit"]},
        "claims": copy.deepcopy(CLAIMS),
    }
    value["digest"] = planning.digest(value)
    return value


def _checked(journal: handoff_store.Journal, identity: str) -> Doc | None:
    record = journal.get(identity)
    if record is None:
        return None
    return _validate_record(journal, record)


def _validate_record(journal: handoff_store.Journal, record: Doc) -> Doc:
    try:
        intent, package = record["intent"], record["prepared"]
        contract = intent["contract"]
        _require(set(contract) == {"request", "repository", "source", "prepared"}, "invalid retained handoff contract")
        request = contract["request"]
        _require(_request(request_from_document(request)) == request, "retained handoff request changed")
        storage = request["policy"]["storage"]
        _require(
            storage["journal_id"] == journal.journal_id and contract["repository"] == journal.repository,
            "retained handoff belongs to another storage instance",
        )
        _require(
            storage["repository"] == journal.repository["path"]
            and storage["repository_id"] == journal.repository["instance_id"],
            "retained handoff repository scope changed",
        )
        _require(
            intent["identity"] == _identity(contract) and intent["id"] == planning.digest(intent["identity"]),
            "retained handoff identity changed",
        )
        _require(intent["authorization"] == _authorization(journal, contract), "retained handoff authorization changed")
        source = contract["source"]
        _require(
            set(source) == {"journal_id", "receipt", "input_target", "contract"}
            and source["journal_id"] == storage["executor_journal_id"],
            "retained source journal changed",
        )
        receipt = source["receipt"]
        source_contract = source["contract"]
        _require(
            planning.digest(source_contract) == receipt["authorization"]["contract_digest"]
            and source["input_target"] == source_contract["target"]
            and source_contract["policy"]["storage"] == receipt["authorization"]["storage"]
            and source_contract["policy"]["storage"]["journal"] == storage["executor_journal"]
            and source_contract["policy"]["storage"]["journal_id"] == storage["executor_journal_id"]
            and source_contract["binding"]["id"] == receipt["binding_id"]
            and source_contract["task"]["task"] == receipt["task"],
            "retained source target or contract differs from its executor authorization",
        )
        _require(
            receipt["schema_version"] == executor_contract.VERSION
            and receipt["execution_id"] == request["execution_id"]
            and receipt["attempt"] == request["attempt"]
            and receipt["outcome"] == "succeeded",
            "handoff requires its exact successful source receipt",
        )
        _require(
            receipt["digest"] == planning.digest({key: value for key, value in receipt.items() if key != "digest"})
            and receipt["claims"] == executor_contract.CLAIMS,
            "retained source receipt changed",
        )
        artifacts = executor_contract.artifact_roster(receipt["build"]["artifacts"])
        _require(
            receipt["build"]
            == {
                "kind": "build",
                "artifacts": artifacts,
                "id": planning.digest({"schema_version": executor_contract.VERSION, "artifacts": artifacts}),
            },
            "retained source build changed",
        )
        target = source["input_target"]
        _require(
            set(target) == {"context", "model_revision", "revision"}
            and target["revision"] == request["base_revision"]
            and re.fullmatch(r"[0-9a-f]{40}", target["model_revision"]),
            "retained source target changed",
        )
        handoff_git.verify_prepared(package)
        _require(
            contract["prepared"] == _summary(package)
            and package["repository"] == journal.repository
            and package["base"] == request["base_revision"]
            and package["author"] == request["author"]
            and package["message"] == request["message"],
            "retained Git preparation differs from authorization",
        )
        roster = {item["path"]: item for item in artifacts}
        import base64

        objects = {item["oid"]: item for item in package["objects"]}
        changes = {item["path"]: item for item in package["changes"]}
        _require(set(changes) == set(request["mappings"].values()), "retained Git changes differ from reviewed mapping")
        for name, destination in request["mappings"].items():
            _require(name in roster, "handoff mapping names an unretained artifact")
            after = changes[destination]["after"]
            obj = objects[after["oid"]]
            content = base64.b64decode(obj["data_base64"], validate=True)
            _require(
                obj["type"] == "blob"
                and len(content) == roster[name]["size"]
                and "sha256:" + hashlib.sha256(content).hexdigest() == roster[name]["digest"],
                "prepared Git bytes differ from retained executor artifacts",
            )
        if record["phase"] == "published":
            _require(record["receipt"] == _receipt(record), "retained publication receipt changed")
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError) as error:
        raise ValueError("invalid retained handoff history") from error
    return record


@contextmanager
def _locked(request: Doc, *, write: bool, deadline: store.Deadline, source: bool) -> Iterator[handoff_store.Journal]:
    storage = request["policy"]["storage"]
    journal, repository = Path(storage["journal"]), Path(storage["repository"])
    directories = [journal.parent, repository.parent, repository]
    if source:
        directories.append(Path(storage["executor_journal"]).parent)
    with (
        operation_locks.directories(directories, write=write, deadline=deadline),
        handoff_store.opened(journal, write=write, deadline=deadline, already_locked=True) as retained,
    ):
        _require(retained.journal_id == storage["journal_id"], "handoff journal instance differs from reviewed policy")
        current = handoff_git.inspect_repository(repository, timeout=deadline.remaining())
        _require(
            current == retained.repository and current["instance_id"] == storage["repository_id"],
            "handoff repository identity changed",
        )
        yield retained


def _artifact(root: Path, name: str, item: Doc) -> bytes:
    _require(root.is_absolute() and root == root.resolve(), "executor artifact bundle traverses a symbolic link")
    descriptors: list[int] = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        info = os.fstat(parent)
        _require(
            info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "executor artifact bundle is not owner-private",
        )
        parts = name.split("/")
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
            info = os.fstat(parent)
            _require(info.st_uid == os.geteuid() and not info.st_mode & 0o022, "unsafe executor artifact directory")
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.geteuid()
            and before.st_size == item["size"],
            "executor artifact is not its retained regular file",
        )
        content = bytearray()
        while block := os.read(descriptor, min(128 * 1024, item["size"] + 1 - len(content))):
            content.extend(block)
            _require(len(content) <= item["size"], "executor artifact grew while inspected")
        after = os.fstat(descriptor)
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

        _require(
            identity(before) == identity(after) == identity(current)
            and len(content) == item["size"]
            and "sha256:" + hashlib.sha256(content).hexdigest() == item["digest"],
            "executor artifact bytes changed from their retained identity",
        )
        return bytes(content)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _selection(request: Doc, journal: handoff_store.Journal, deadline: store.Deadline) -> tuple[Doc, Doc]:
    storage = request["policy"]["storage"]
    with executor_store.opened(
        Path(storage["executor_journal"]), write=False, deadline=deadline, already_locked=True
    ) as source:
        _require(source.journal_id == storage["executor_journal_id"], "source executor journal instance changed")
        work = source.get(request["execution_id"])
        _require(work is not None, "source executor work does not exist")
        assert work is not None
        executor_contract.validate_history(
            work, journal_id=source.journal_id, ledger=source.ledger, max_concurrency=source.max_concurrency
        )
        _require(
            len(work["attempts"]) == request["attempt"] and work["attempts"][-1]["phase"] == "succeeded",
            "handoff requires a successful retained executor attempt",
        )
        receipt = work["attempts"][-1]["events"][-1]["data"]["receipt"]
        target = work["intent"]["contract"]["target"]
        _require(target["revision"] == request["base_revision"], "handoff base differs from the executor input target")
        protected = work["intent"]["contract"]["policy"]["storage"]
        _require(
            protected["journal"] == storage["executor_journal"]
            and protected["journal_id"] == storage["executor_journal_id"],
            "source executor history belongs to another canonical journal",
        )
        for key in ("journal", "repository"):
            destination = Path(storage[key])
            for name in ("journal", "ledger", "state", "output_root"):
                other = Path(protected[name])
                _require(
                    not (destination == other or destination in other.parents or other in destination.parents),
                    "handoff destination overlaps executor authority or artifact storage",
                )
        roster = {item["path"]: item for item in receipt["build"]["artifacts"]}
        files: dict[str, bytes] = {}
        for name, destination in request["mappings"].items():
            _require(name in roster, "handoff mapping names an unretained artifact")
            files[destination] = _artifact(Path(receipt["output"]), name, roster[name])
        package = handoff_git.prepare(
            Path(storage["repository"]),
            request["base_revision"],
            files,
            message=request["message"],
            author=request["author"],
            timeout=deadline.remaining(),
        )
        contract = {
            "request": copy.deepcopy(request),
            "repository": journal.repository,
            "source": {
                "journal_id": source.journal_id,
                "receipt": copy.deepcopy(receipt),
                "input_target": copy.deepcopy(target),
                "contract": copy.deepcopy(work["intent"]["contract"]),
            },
            "prepared": _summary(package),
        }
        return contract, package


def _check_ref(request: Doc, deadline: store.Deadline) -> None:
    _require(
        handoff_git.read_ref(
            Path(request["policy"]["storage"]["repository"]), request["ref"], timeout=deadline.remaining()
        )
        == request["expected_revision"],
        "handoff destination ref changed; review a fresh proposal",
    )


def _view(operation: str, record: Doc) -> Doc:
    return _report(
        operation,
        record["phase"],
        "The exact local ref publication is retained."
        if record["phase"] == "published"
        else "The prepared publication requires reconciliation before another effect.",
        identity=record["intent"]["id"],
        proposal=copy.deepcopy(record["intent"]["contract"]),
        receipt=copy.deepcopy(record["receipt"]),
    )


def preview(request: Request, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    document = _request(request)
    validate_policy(document["policy"])
    budget = store.Deadline(lock_timeout)
    with _locked(document, write=False, deadline=budget, source=True) as journal:
        contract, package = _selection(document, journal, budget)
        authorization = _authorization(journal, contract)
        prior = _checked(journal, authorization["handoff_id"])
        if prior is not None:
            _require(
                prior["intent"]["authorization"] == authorization,
                "this handoff identity retains another immutable contract",
            )
            report = _view("preview", prior)
            report["dry_run"] = True
            report.pop("digest")
            report["digest"] = planning.digest(report)
            return report
        _check_ref(document, budget)
        # Check the complete proposed record before any durable write, including cross-codec facts.
        _validate_record(journal, _proposed_record(authorization, contract, package))
        return _report(
            "preview",
            "ready",
            "Review this exact local Git publication and authorize it separately.",
            identity=authorization["handoff_id"],
            dry_run=True,
            authorization_template=authorization,
            proposal=contract,
        )


def _install_objects(repository: Path, prepared: Doc, deadline: store.Deadline) -> None:
    handoff_git.write_objects(repository, prepared, timeout=deadline.remaining())


def _publish_ref(repository: Path, ref: str, expected: str | None, commit: str, deadline: store.Deadline) -> None:
    handoff_git.compare_and_swap(repository, ref, expected, commit, timeout=deadline.remaining())


def _finalize(journal: handoff_store.Journal, identity: str, receipt: Doc) -> Doc:
    return journal.finish(identity, receipt)


def _proposed_record(authorization: Doc, contract: Doc, package: Doc) -> Doc:
    return {
        "intent": {
            "schema_version": VERSION,
            "id": authorization["handoff_id"],
            "identity": _identity(contract),
            "authorization": copy.deepcopy(authorization),
            "contract": contract,
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
        },
        "prepared": package,
        "phase": "prepared",
        "receipt": None,
    }


def _publish(journal: handoff_store.Journal, record: Doc, deadline: store.Deadline, operation: str) -> Doc:
    contract = record["intent"]["contract"]
    request = contract["request"]
    repository = Path(contract["repository"]["path"])
    _require(
        handoff_git.inspect_repository(repository, timeout=deadline.remaining()) == journal.repository,
        "handoff repository identity changed",
    )
    current = handoff_git.read_ref(repository, request["ref"], timeout=deadline.remaining())
    commit = record["prepared"]["commit"]
    if current not in (request["expected_revision"], commit):
        return _report(
            operation,
            "conflict",
            "The destination ref names a third revision; retain the prepared handoff and resolve the conflict.",
            identity=record["intent"]["id"],
            proposal=copy.deepcopy(contract),
        )
    _install_objects(repository, record["prepared"], deadline)
    if current != commit:
        _publish_ref(repository, request["ref"], request["expected_revision"], commit, deadline)
    handoff_git.synchronize(repository, request["ref"], timeout=deadline.remaining())
    _require(
        handoff_git.read_ref(repository, request["ref"], timeout=deadline.remaining()) == commit,
        "handoff destination moved before receipt finalization",
    )
    _require(
        handoff_git.inspect_repository(repository, timeout=deadline.remaining()) == journal.repository,
        "handoff repository identity changed before finalization",
    )
    result = _finalize(journal, record["intent"]["id"], _receipt(record))
    checked = _checked(journal, result["intent"]["id"])
    assert checked is not None
    return _view(operation, checked)


def apply(request: Request, *, authorization: Any, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    document = _request(request)
    storage = document["policy"]["storage"]
    _require(
        isinstance(authorization, dict) and isinstance(authorization.get("handoff_id"), str),
        "separate reviewed handoff authorization is required",
    )
    budget = store.Deadline(lock_timeout)
    # Immutable replay needs no source artifacts, current model or destination inspection.
    with handoff_store.opened(Path(storage["journal"]), write=False, deadline=budget) as journal:
        _require(journal.journal_id == storage["journal_id"], "handoff journal instance changed")
        prior = _checked(journal, authorization["handoff_id"])
        if prior is not None:
            _require(
                prior["intent"]["authorization"] == authorization
                and prior["intent"]["contract"]["request"] == document,
                "retained handoff differs from this request or authorization",
            )
            return _view("apply", prior)
    validate_policy(document["policy"])
    with _locked(document, write=True, deadline=budget, source=True) as journal:
        prior = _checked(journal, authorization["handoff_id"])
        if prior is not None:
            _require(
                prior["intent"]["authorization"] == authorization
                and prior["intent"]["contract"]["request"] == document,
                "retained handoff differs from this request or authorization",
            )
            return _view("apply", prior)
        contract, package = _selection(document, journal, budget)
        expected = _authorization(journal, contract)
        _require(
            authorization == expected, "handoff authorization differs from current artifacts, repository, base or ref"
        )
        _check_ref(document, budget)
        proposed = _validate_record(journal, _proposed_record(expected, contract, package))
        record = journal.prepare(proposed["intent"], package)
        checked = _checked(journal, record["intent"]["id"])
        assert checked is not None
        return _publish(journal, checked, budget, "apply")


def show(journal: Path, identity: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    with handoff_store.opened(journal, write=False, deadline=store.Deadline(lock_timeout)) as source:
        record = _checked(source, identity)
        return (
            _view("show", record)
            if record
            else _report("show", "missing", "The handoff identity is not retained.", identity=identity)
        )


def recover(journal: Path, identity: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    budget = store.Deadline(lock_timeout)
    with handoff_store.opened(journal, write=False, deadline=budget) as source:
        record = _checked(source, identity)
        _require(record is not None, "handoff intent does not exist")
        assert record is not None
        if record["phase"] == "published":
            return _view("recover", record)
        request = record["intent"]["contract"]["request"]
        _require(
            request["policy"]["storage"]["journal"] == str(journal.absolute()),
            "recovery journal path differs from retained authority",
        )
    with _locked(request, write=True, deadline=budget, source=False) as retained:
        current = _checked(retained, identity)
        assert current is not None
        return (
            _view("recover", current)
            if current["phase"] == "published"
            else _publish(retained, current, budget, "recover")
        )


def recover_journal(journal: Path, *, authorize: bool = False, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    _require(authorize is True, "explicit handoff journal recovery authorization is required")
    with handoff_store.opened(journal, write=True, recover=True, deadline=store.Deadline(lock_timeout)) as source:
        return _report(
            "recover-journal",
            "recovered",
            "Handoff journal storage was checked; inspect prepared publications separately.",
            recovery_backup=str(source.recovery_backup) if source.recovery_backup else None,
        )


def as_text(report: Doc) -> str:
    rows = [f"Handoff: {report.get('handoff_id') or '(none)'}", f"Status: {report['status']}", str(report["reason"])]
    if report.get("receipt"):
        rows.append("Git revision: " + report["receipt"]["commit"])
    rows.append("Local artifact publication only; evidence admission and completion require their own authorization.")
    return "\n".join(rows)
