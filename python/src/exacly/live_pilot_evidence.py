"""Read-only retained evidence for the fixed live REQ-039 consumer.

Local JSON is not live proof. Acceptance authenticates the actual coordinator artifact and executed
code through GitHub, then freshly admits the exact independent push producer. Controlled sources
exercise this verifier's contract only. Exports contain neither credentials nor publication keys.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from exacly import (
    admission,
    completion,
    completion_store,
    dispatch,
    dispatch_store,
    executions,
    executor,
    executor_contract,
    executor_store,
    handoff,
    handoff_git,
    handoff_store,
    output_association,
    planning,
    producer_selection,
    remote_publication,
    resource_guard,
    standing_authorization,
    state_writer,
    worker_integration,
)
from exacly import execution_store as store
from exacly.applicability import Target
from exacly.github_evidence import ARCHIVE_LIMIT

Doc = dict[str, Any]
VERSION = "0.1.0"
REPOSITORY = "whiteredhouse/exacly"
REPOSITORY_ID = 1411700094
BRANCH = "pilot/req039-consumer"
COORDINATOR = ".github/workflows/req039-coordinator.yml"
UPLOAD_STEP = "Retain live acceptance evidence"
ARTIFACT_PREFIX = "exacly-req039-acceptance-"
MEMBER = "acceptance-evidence.json"
LIMIT = 32 * 1024 * 1024
IMPLEMENTATION_PATHS = [
    ".github",
    "ci",
    "conftest.py",
    "fixtures/unattended-pilot/model.yaml",
    "mise.toml",
    "pyproject.toml",
    "python",
    "spec/schema",
    "tests",
    "uv.lock",
    "verification",
]
_BLOBS = ("archive", "report", "before_state", "after_state")
EXCLUDED_PATHS = ["ci/verification.yaml"]
_CONTRACT_PATHS = ["spec/exacly/unattended-automation.yaml", "spec/exacly/automation.yaml"]
_REQUIREMENTS = {f"REQ-{number:03}" for number in range(34, 40)}
REQUIRED_CASES = (
    "dispatch_restart",
    "publication_response_loss",
    "duplicate_events",
    "divergent_ref",
    "producer_unavailable",
    "wrong_revision",
    "wrong_attempt",
    "criteria_changed",
    "verifier_changed",
    "delegation_revoked",
    "delegation_expired",
    "objective_budget_exhausted",
)
ORIGINAL_GOAL = planning.digest(
    {
        "repository_id": REPOSITORY_ID,
        "branch": BRANCH,
        "record": "REQ-001",
        "namespace": "req039-consumer",
        "dimension": "verification",
        "source": "fixtures/unattended-pilot/inputs/source.txt",
        "result": "fixtures/unattended-pilot/results/source-digests.json",
    }
)


def _digest(value: Any) -> Doc:
    store.require(
        isinstance(value, dict)
        and value.get("digest") == planning.digest({key: item for key, item in value.items() if key != "digest"}),
        "retained evidence content digest changed",
    )
    return dict(value)


def _contract_bytes(checkout: Path, relative: str) -> bytes:
    path = checkout / relative
    store.require(path.resolve() == path, "acceptance contract contains a path alias")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        store.require(
            stat.S_ISREG(before.st_mode) and before.st_size <= 8 * 1024**2,
            "acceptance contract is not a bounded regular file",
        )
        with os.fdopen(os.dup(descriptor), "rb") as source:
            value = source.read(8 * 1024**2 + 1)
        after = path.lstat()
        store.require(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and len(value) <= 8 * 1024**2,
            "acceptance contract changed during observation",
        )
        return value
    finally:
        os.close(descriptor)


def _requirements(contents: list[bytes]) -> str:
    selected: dict[str, Doc] = {}
    for content in contents:
        document = yaml.safe_load(content)
        store.require(
            isinstance(document, dict) and isinstance(document.get("records"), list),
            "automation contract is unavailable",
        )
        for record in document["records"]:
            if record.get("id") not in _REQUIREMENTS:
                continue
            identity = record["id"]
            store.require(
                identity not in selected and record.get("type") == "Requirement", "ambiguous automation requirement"
            )
            selected[identity] = {key: record[key] for key in ("statement", "acceptance_criteria", "observe")}
    store.require(set(selected) == _REQUIREMENTS, "current unattended acceptance requirements are incomplete")
    return planning.digest(selected)


def _current_entries(checkout: Path) -> list[Doc]:
    store.require(checkout.is_absolute() and checkout.resolve() == checkout, "implementation checkout is not canonical")
    files: set[Path] = set()
    for selector in IMPLEMENTATION_PATHS:
        chosen = checkout / selector
        if not os.path.lexists(chosen):
            continue
        stack = [chosen]
        while stack:
            path = stack.pop()
            info = path.lstat()
            store.require(not stat.S_ISLNK(info.st_mode), "implementation closure contains a symbolic link")
            if str(path.relative_to(checkout)) in EXCLUDED_PATHS:
                store.require(
                    stat.S_ISREG(info.st_mode) and not info.st_mode & 0o111,
                    "excluded own-model gate metadata must remain a non-executable regular file",
                )
                continue
            if stat.S_ISDIR(info.st_mode):
                if path.name == "__pycache__":
                    cached = list(path.iterdir())
                    store.require(len(cached) <= 20000, "implementation cache exceeds its entry bound")
                    for entry in cached:
                        metadata = entry.lstat()
                        matched = re.fullmatch(
                            r"([A-Za-z0-9_]+)\.cpython-[0-9]{2,3}(?:-pytest-[0-9.]+)?(?:\.opt-[12])?\.pyc", entry.name
                        )
                        store.require(
                            matched is not None
                            and stat.S_ISREG(metadata.st_mode)
                            and metadata.st_size <= 8 * 1024**2
                            and (path.parent / (matched.group(1) + ".py")).is_file(),
                            "implementation cache contains unsupported source or non-cache content",
                        )
                    continue
                stack.extend(path.iterdir())
            else:
                store.require(
                    stat.S_ISREG(info.st_mode) and info.st_size <= 8 * 1024**2,
                    "implementation closure contains unsupported or oversized files",
                )
                files.add(path)
            store.require(len(files) + len(stack) <= 20000, "implementation closure exceeds its file bound")
    entries: list[Doc] = []
    total = 0
    for path in sorted(files):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            with os.fdopen(os.dup(descriptor), "rb") as content:
                raw = content.read(8 * 1024**2 + 1)
            after = os.fstat(descriptor)

            def identity(info: os.stat_result) -> tuple[int, ...]:
                return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

            store.require(
                len(raw) <= 8 * 1024**2 and identity(before) == identity(after) == identity(path.lstat()),
                "implementation source changed during observation",
            )
            total += len(raw)
            store.require(total <= 128 * 1024**2, "implementation closure exceeds its byte bound")
            entries.append(
                {
                    "path": str(path.relative_to(checkout)),
                    "type": "blob",
                    "mode": "100755" if before.st_mode & 0o111 else "100644",
                    "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
                }
            )
        finally:
            os.close(descriptor)
    return entries


def _runtime_entries(entries: list[Doc]) -> list[Doc]:
    result = []
    for entry in entries:
        path = entry.get("path", "")
        if path in EXCLUDED_PATHS:
            store.require(
                entry.get("type") == "blob" and entry.get("mode") == "100644",
                "excluded own-model gate metadata is not a regular non-executable blob",
            )
            continue
        store.require(
            not any(path.startswith(excluded + "/") for excluded in EXCLUDED_PATHS),
            "excluded metadata path cannot alias a runtime code directory",
        )
        result.append(entry)
    return result


def _loaded_sources(checkout: Path) -> None:
    for name, module in tuple(sys.modules.items()):
        if name not in {"exacly", "ci", "__main__"} and not name.startswith(("exacly.", "ci.")):
            continue
        selected = getattr(module, "__file__", None)
        if selected is None:
            continue
        imported = Path(selected).resolve()
        if name == "exacly" or name.startswith("exacly."):
            root = checkout / "python/src/exacly"
        elif (
            name == "ci"
            or name.startswith("ci.")
            or (name == "__main__" and imported.name in {"req039_pilot.py", "req039_bootstrap.py", "req039_history.py"})
        ):
            root = checkout / "ci"
        else:
            continue
        store.require(
            imported.is_relative_to(root) and imported.suffix == ".py" and "__pycache__" not in imported.parts,
            "an imported Exacly or CI module escaped the exact checkout",
        )


def implementation_fingerprint(checkout: Path, repository: Path, revision: str) -> Doc:
    """Bind actual imported source and fixed CI closure to the exact sealed approved Git revision."""
    store.require(
        isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision), "an exact source revision is required"
    )
    package = Path(__file__).resolve().parent
    store.require(
        package == checkout / "python/src/exacly", "executed Exacly comes from another checkout or installation"
    )
    _loaded_sources(checkout)
    handoff_git.inspect_repository(repository)
    listed = handoff_git._run(
        repository, ["ls-tree", "-r", "-z", "--full-tree", revision], store.Deadline(), limit=8 * 1024**2
    )
    entries: list[Doc] = []
    for item in listed.split(b"\0"):
        if not item:
            continue
        header, encoded_name = item.split(b"\t", 1)
        mode, kind, identity = header.decode().split()
        entries.append({"path": encoded_name.decode(), "mode": mode, "type": kind, "sha": identity})
    expected = admission.observer_digest(_runtime_entries(entries), IMPLEMENTATION_PATHS)
    actual = admission.observer_digest(_current_entries(checkout), IMPLEMENTATION_PATHS)
    store.require(actual == expected, "executed implementation or CI bytes differ from the exact approved revision")
    by_path = {entry["path"]: entry for entry in entries}
    contracts = _requirements(
        [
            handoff_git._run(
                repository, ["cat-file", "blob", by_path[path]["sha"]], store.Deadline(), limit=8 * 1024**2
            )
            for path in _CONTRACT_PATHS
        ]
    )
    store.require(
        contracts == _requirements([_contract_bytes(checkout, path) for path in _CONTRACT_PATHS]),
        "executed acceptance contract semantics differ from the approved revision",
    )
    return {
        "schema_version": VERSION,
        "revision": revision,
        "paths": list(IMPLEMENTATION_PATHS),
        "digest": actual,
        "requirements_digest": contracts,
        "excluded_paths": list(EXCLUDED_PATHS),
    }


def current_implementation(checkout: Path) -> Doc:
    """Fingerprint this verifier's actual checkout, independently of a supplied evidence file."""
    store.require(
        Path(__file__).resolve().parent == checkout / "python/src/exacly",
        "current verifier comes from another checkout",
    )
    _loaded_sources(checkout)
    return {
        "schema_version": VERSION,
        "paths": list(IMPLEMENTATION_PATHS),
        "digest": admission.observer_digest(_current_entries(checkout), IMPLEMENTATION_PATHS),
        "requirements_digest": _requirements([_contract_bytes(checkout, path) for path in _CONTRACT_PATHS]),
        "excluded_paths": list(EXCLUDED_PATHS),
    }


def export_native(bundle: Doc, retained: Doc) -> Doc:
    """Read actual checked native stores after completion; never export a key or credential."""
    components, correlations = bundle["components"], retained["event"]["correlations"]
    controller = importlib.import_module("exacly.automation_controller")
    selected = components["controller"]
    current = controller.show(Path(selected["journal"]), selected["journal_id"])
    store.require(
        current == retained and retained["event"]["status"] == "completed",
        "controller is not the exact completed native history",
    )
    stores = components["native_stores"]
    ledger = Path(components["ledger"])
    anchor_module = importlib.import_module("exacly.controller_store")
    anchor = anchor_module.check_ledger_anchor(components["ledger_anchor"])
    store.require(
        anchor["ledger"] == str(ledger) and anchor["digest"] == stores["complete"]["id"],
        "native export ledger differs from its independently pinned physical anchor",
    )
    native: Doc = {}
    for kind, provider, reader, identity in (
        ("dispatch", dispatch_store, dispatch.show, "dispatch_id"),
        ("execute", executor_store, executor.show, "execution_id"),
        ("handoff", handoff_store, handoff.show, "handoff_id"),
    ):
        with provider.opened(Path(stores[kind]["path"]), write=False) as selected_store:
            store.require(
                selected_store.journal_id == stores[kind]["id"], f"native {kind} export store identity changed"
            )
            # Keep the checked instance open across the public reader. Its exit checks also
            # reject a replacement while the native view was being read.
            native[kind] = reader(Path(stores[kind]["path"]), correlations[kind][identity])
    native.update(
        {
            "publish": remote_publication.lookup(
                Path(stores["publish"]["path"]), stores["publish"]["id"], correlations["publish"]["operation_id"]
            ),
            "associate": output_association.show(
                components["association"], correlations["associate"]["association_id"]
            ),
            "completion": completion.show(ledger, correlations["complete"]["id"]),
        }
    )
    deadline = store.Deadline()
    with (
        store.locked(ledger, write=False, deadline=deadline) as (absolute, _),
        store.database(absolute, create=False, deadline=deadline) as (connection, _),
    ):
        envelope = completion_store.get(connection, correlations["complete"]["id"], deadline=deadline)
        native["binding"] = completion_store.envelope(connection, correlations["binding_id"], deadline=deadline)
        native["original_binding"] = completion_store.envelope(
            connection, correlations["original_binding_id"], deadline=deadline
        )
    store.require(
        anchor_module.check_ledger_anchor(components["ledger_anchor"]) == anchor,
        "native export ledger changed while reading its completion and binding views",
    )
    store.require(
        envelope["phase"] == "committed" and envelope["receipt"] == correlations["complete"],
        "native completion is not committed",
    )
    completion_store.semantic_facts(envelope, native["binding"]["binding"])
    store.require(
        state_writer.read(Path(components["state"])).content == envelope["after_state"],
        "current native state drifted after completion",
    )
    evidence = {key + "_b64": base64.b64encode(envelope[key]).decode() for key in _BLOBS}
    evidence.update({key: envelope[key] for key in ("phase", "admission_receipt", "model_documents", "admitted_state")})
    delegation = bundle["delegation"]
    accounting = standing_authorization.inspect(
        Path(delegation["accounting"]["path"]),
        store_id=delegation["accounting"]["store_id"],
        objective_id=delegation["objective"]["id"],
    )
    return {"schema_version": VERSION, "native": native, "completion_evidence": evidence, "accounting": accounting}


class _BoundedSource:
    def __init__(self, source: admission.Source, timeout: float) -> None:
        self.source, self.deadline = source, store.Deadline(timeout)

    def get_json(self, path: str) -> Doc:
        self.deadline.check()
        result = self.source.get_json(path)
        self.deadline.check()
        return result

    def download_artifact(self, repository: str, identity: int) -> bytes:
        self.deadline.check()
        result = self.source.download_artifact(repository, identity)
        self.deadline.check()
        return result


def _reference(value: Any) -> Doc:
    store.require(
        isinstance(value, dict)
        and set(value) == {"run_id", "attempt", "artifact_id"}
        and all(type(value[key]) is int and value[key] > 0 for key in value)
        and value["attempt"] <= 10,
        "select one exact coordinator run, bounded attempt and artifact ID",
    )
    return dict(value)


def configured_reference(checkout: Path, environment: Mapping[str, str] | None = None) -> Doc | None:
    """Read a non-authoritative selector; only verify_live can authenticate the selected proof."""
    supplied = os.environ if environment is None else environment
    run, artifact = supplied.get("EXACLY_REQ039_LIVE_RUN_ID"), supplied.get("EXACLY_REQ039_LIVE_ARTIFACT_ID")
    attempt = supplied.get("EXACLY_REQ039_LIVE_ATTEMPT")
    if any(value is not None for value in (run, artifact, attempt)):
        store.require(
            run is not None
            and artifact is not None
            and all(re.fullmatch(r"[1-9][0-9]{0,19}", value or "") for value in (run, artifact, attempt or "1")),
            "configured live selector requires exact actual run, attempt and artifact identities",
        )
        assert isinstance(run, str) and isinstance(artifact, str)
        return _reference({"run_id": int(run), "attempt": int(attempt or "1"), "artifact_id": int(artifact)})
    enabled = supplied.get("EXACLY_REQ039_VERIFY_RETAINED_PROOF")
    store.require(enabled in (None, "1"), "retained proof verification requires its explicit opt-in value 1")
    if enabled != "1":
        return None
    path = checkout / "verification-references/req039.json"
    if not os.path.lexists(path):
        return None
    store.require(
        path.resolve() == path and path.is_file() and path.stat().st_size <= 16384,
        "retained live selector must be a bounded regular repository file without aliases",
    )
    value = json.loads(path.read_bytes(), object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
    store.require(
        isinstance(value, dict)
        and set(value) == {"repo", "run_id", "attempt", "artifact_id"}
        and value["repo"] == REPOSITORY,
        "retained live selector cannot change repository or supply proof",
    )
    return _reference({key: value[key] for key in ("run_id", "attempt", "artifact_id")})


def _artifact(source: admission.Source, reference: Doc) -> tuple[Doc, Doc]:
    metadata = source.get_json(f"/repos/{REPOSITORY}/actions/artifacts/{reference['artifact_id']}")
    run = metadata.get("workflow_run", {})
    store.require(
        metadata.get("id") == reference["artifact_id"]
        and metadata.get("expired") is False
        and metadata.get("name") == f"{ARTIFACT_PREFIX}{reference['run_id']}-{reference['attempt']}"
        and run.get("id") == reference["run_id"]
        and run.get("repository_id") == run.get("head_repository_id") == REPOSITORY_ID,
        "live acceptance artifact identity, repository or expiry differs",
    )
    archive = source.download_artifact(REPOSITORY, reference["artifact_id"])
    store.require(
        isinstance(archive, bytes)
        and len(archive) <= ARCHIVE_LIMIT
        and metadata.get("digest") == admission.sha256(archive),
        "live acceptance bytes differ from the actual server digest",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            store.require(
                len(entries) == 1
                and entries[0].filename == MEMBER
                and not entries[0].is_dir()
                and not entries[0].flag_bits & 1
                and entries[0].file_size <= LIMIT,
                "unsupported live acceptance artifact layout",
            )
            with zipped.open(entries[0]) as stream:
                content = stream.read(LIMIT + 1)
            store.require(len(content) == entries[0].file_size <= LIMIT, "live acceptance metadata exceeds its bound")
        value = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(
            isinstance(value, dict) and content == (planning.canonical(value) + "\n").encode(),
            "live acceptance metadata is not canonical",
        )
        return value, metadata
    except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError) as error:
        raise store.StoreError("live acceptance archive could not be independently read") from error


def _blob(evidence: Doc, key: str, limit: int) -> bytes:
    value = evidence.get(key + "_b64")
    store.require(
        isinstance(value, str) and len(value) <= 4 * (limit // 3 + 1), "missing or oversized native completion bytes"
    )
    assert isinstance(value, str)
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise store.StoreError("native completion bytes are not canonical base64") from error
    store.require(
        len(decoded) <= limit and base64.b64encode(decoded).decode() == value, "invalid native completion byte identity"
    )
    return decoded


def _dispatch_history(report: Doc) -> Doc:
    record = report["record"]
    store.require(
        isinstance(record, dict) and set(record) == {"intent", "events", "phase", "digest"},
        "native dispatch history is incomplete",
    )
    intent = record["intent"]
    store.require(
        intent["id"] == report["dispatch_id"] == planning.digest(intent["identity"]),
        "native dispatch operation identity changed",
    )
    previous = planning.digest(intent)
    events = record["events"]
    store.require(
        isinstance(events, list) and 1 <= len(events) <= 256, "native dispatch events are missing or unbounded"
    )
    for number, event in enumerate(events, 1):
        store.require(
            event["seq"] == number
            and event["previous"] == previous
            and event["digest"] == planning.digest({key: value for key, value in event.items() if key != "digest"}),
            "native dispatch event chain changed",
        )
        previous = event["digest"]
    store.require(
        previous == record["digest"] and record["phase"] == events[-1]["phase"] == "registered",
        "native dispatch lacks its retained registered head",
    )
    contract = intent["contract"]
    date = max(executions.timestamp(event["at"]).date() for event in events)
    request = dispatch.Request(
        contract["plan"],
        intent["identity"]["action_id"],
        contract["readiness_policy"],
        contract["trust_policy"],
        contract["dispatch_policy"],
        Target(contract["target"]),
        date,
    )
    receipt = dispatch._validate_history(record, request)
    store.require(receipt is not None, "native dispatch has no semantically valid registration receipt")
    assert receipt is not None
    return receipt


def _publication_history(report: Doc) -> None:
    _digest(report)
    intent = report["intent"]
    store.require(
        intent["id"] == report["operation_id"] == intent["authorization"]["operation_id"],
        "native publication operation identity changed",
    )
    previous = planning.digest(intent)
    sent = 0
    for event in report["history"]:
        store.require(
            event["previous"] == previous
            and event["digest"] == planning.digest({key: value for key, value in event.items() if key != "digest"}),
            "native publication event chain changed",
        )
        previous = event["digest"]
        sent += event["phase"] == "sent"
        store.require(
            event["data"].get("server_rejected") is not True, "explicitly rejected operation cannot become live success"
        )
    store.require(
        sent == 1 and report["history"][-1]["phase"] == "published", "native publication lacks one exact retained send"
    )


def validate_native(evidence: Doc) -> Doc:
    """Validate retained native provenance and exact completion semantics, without calling it live."""
    packet, native = evidence["packet"], evidence["native"]
    controller = evidence["controller"]
    store.require(
        controller["event"]["status"] == "completed", "a controller phase alone does not establish completion"
    )
    correlations = controller["event"]["correlations"]
    statuses = {
        "dispatch": "registered",
        "execute": "succeeded",
        "handoff": "published",
        "publish": "published",
        "associate": "associated",
        "completion": "completed",
    }
    for key, status in statuses.items():
        store.require(native[key]["status"] == status, f"native {key} did not establish its terminal outcome")
    for phase in ("execute", "handoff", "associate"):
        store.require(
            native[phase]["receipt"] == correlations[phase],
            "controller correlation differs from native retained receipt",
        )
        _digest(native[phase]["receipt"])
    store.require(
        native["publish"] == correlations["publish"], "controller publication differs from native exact operation"
    )
    dispatched = _dispatch_history(native["dispatch"])
    store.require(dispatched == correlations["dispatch"], "native dispatch differs from controller correlation")
    _publication_history(native["publish"])
    complete = _digest(native["completion"]["receipt"])
    jsonschema.validate(complete, completion.RECEIPT_SCHEMA)
    store.require(complete == correlations["complete"], "native completion receipt differs from controller correlation")
    binding = executions.validate_record(native["binding"]["binding"])
    original = executions.validate_record(native["original_binding"]["binding"])
    store.require(
        native["binding"]["active"] is True
        and native["binding"]["completed_receipt"] == complete
        and native["original_binding"]["active"] is False
        and binding["supersedes"] == original["id"]
        and native["original_binding"]["superseded_by"] == binding["id"]
        and binding["pending_key"] == original["pending_key"] == complete["pending_key"]
        and binding["id"] == complete["binding_id"] == correlations["binding_id"],
        "completion lacks its exact active output binding and preserved pending identity",
    )
    handoff_receipt = native["handoff"]["receipt"]
    executed = native["execute"]["receipt"]
    executor_contract.validate_history(native["execute"]["history"])
    associated = native["associate"]["receipt"]
    publication = native["publish"]
    authorization = publication["intent"]["authorization"]
    store.require(
        dispatched["binding_id"] == original["id"] == executed["binding_id"]
        and dispatched["pending_key"] == original["pending_key"]
        and dispatched["remote"]["url"] == original["contract"]["pending"]["task"] == executed["task"]
        and native["execute"]["history"]["intent"]["contract"]["binding"] == original
        and native["handoff"]["proposal"]["source"]["receipt"] == executed
        and native["handoff"]["proposal"]["source"]["contract"] == native["execute"]["history"]["intent"]["contract"]
        and publication["intent"]["handoff"] == handoff_receipt,
        "native provenance substitutes a foreign task, execution binding or source receipt",
    )
    association_intent = native["associate"]["intent"]
    store.require(
        planning.digest(association_intent["request"]) == associated["association_id"]
        and association_intent["replacement"] == binding
        and output_association._receipt(associated["association_id"], association_intent) == associated,
        "native output association is not the exact retained registration intent",
    )
    store.require(
        handoff_receipt["source_receipt"] == executed["digest"]
        and handoff_receipt["build"] == executed["build"]
        and handoff_receipt["commit"] == authorization["commit"] == complete["target"]["revision"]
        and handoff_receipt["target"] == complete["target"] == binding["contract"]["target"]
        and handoff_receipt["base_revision"] == packet["input_revision"] == original["contract"]["target"]["revision"]
        and associated["handoff_receipt_digest"] == handoff_receipt["digest"]
        and associated["original_binding"] == original["id"]
        and associated["binding"] == binding["id"],
        "native output provenance does not join execution, handoff, publication, binding and completion",
    )
    observation = publication["history"][-1]["data"].get("observation", {})
    store.require(
        observation.get("operation_id") == publication["operation_id"]
        and observation.get("revision") == authorization["commit"]
        and observation.get("commit_present") is True
        and publication["intent"]["request"]["repository_id"] == str(packet["repository"]["id"])
        and publication["intent"]["request"]["ref"] == "refs/heads/" + packet["consumer_branch"],
        "native publication lacks the exact live operation and destination",
    )
    payload = evidence["completion_evidence"]
    envelope = {key: payload[key] for key in ("phase", "admission_receipt", "model_documents", "admitted_state")}
    envelope.update({key: _blob(payload, key, ARCHIVE_LIMIT if key == "archive" else LIMIT) for key in _BLOBS})
    envelope.update({"intent": native["completion"]["intent"], "receipt": complete})
    store.require(envelope["phase"] == "committed", "native completion intent is not committed")
    completion_store.semantic_facts(envelope, binding)
    store.require(
        handoff_receipt["commit"] != handoff_receipt["base_revision"],
        "pilot did not produce a new exact implementation target",
    )
    store.require(
        complete["trust_policy"]["digest"] == planning.digest(packet["producer_policy"]),
        "native completion used another approved producer policy",
    )
    return envelope


def _fixed_native_goal(evidence: Doc) -> None:
    """The fixed profile is established by native registered contracts, not packet labels."""
    packet, native = evidence["packet"], evidence["native"]
    workflow = packet["workflow_contract"]
    original, output = native["original_binding"]["binding"], native["binding"]["binding"]
    registered = original["contract"]
    goal = registered["goal"]
    complete = native["completion"]["receipt"]
    executed = native["execute"]["history"]["intent"]["contract"]
    dispatched = native["dispatch"]["record"]["intent"]["contract"]
    store.require(
        registered["pending"]["namespace"] == output["contract"]["pending"]["namespace"] == "req039-consumer"
        and goal["record"] == "REQ-001"
        and goal["dimension"] == "verification"
        and workflow["goal"] == {"record": "REQ-001", "dimension": "verification"}
        and goal == output["contract"]["goal"] == complete["goal"]
        and workflow["original_goal"] == packet["original_goal"]
        and workflow["objective_id"] == packet["delegation"]["objective"]["id"]
        and workflow["initial_binding_id"] is None
        and workflow["input_target"] == registered["original_target"] == registered["target"]
        and workflow["plan_digest"] == registered["plan"]["digest"] == complete["plan"]["digest"]
        and registered["plan"] == complete["plan"]
        and executed["action"]["id"] == registered["plan"]["action_id"]
        and workflow["contract"]
        == {
            "model": executed["config_digest"],
            "criteria": goal["criteria_digest"],
            "bindings": goal["bindings_digest"],
            "readiness": registered["policy"]["digest"],
            "trust": registered["trust_policy"]["digest"],
            "observer": packet["producer_policy"]["observer"]["digest"],
        }
        and workflow["contract"]["model"] == dispatched["plan"]["inputs"]["config"]["digest"]
        and workflow["contract"]["readiness"] == planning.digest(dispatched["readiness_policy"])
        and workflow["contract"]["trust"] == planning.digest(packet["producer_policy"]),
        "actual native goal or namespace, plan, criteria, binding or policy differs from the approved fixed consumer",
    )


def _fixed_executor(evidence: Doc) -> None:
    packet, native = evidence["packet"], evidence["native"]
    retained = native["execute"]["history"]["intent"]["contract"]
    policy = executor_contract.validate_policy(packet["components"]["executor_policy"], check_storage=False)
    guard = resource_guard.from_descriptor(packet["resource_guard"])
    identity = packet["worker_identity"]
    snapshot_fields = {
        "schema_version",
        "recipe",
        "repository",
        "revision",
        "tree",
        "model_digest",
        "allowed_paths",
        "protected_paths",
        "files",
    }
    store.require(
        set(identity)
        == snapshot_fields | {"snapshot_digest", "recipe_digest", "inputs", "argv", "expected_outputs", "claims"},
        "fixed worker identity is missing or has unapproved fields",
    )
    snapshot = {key: identity[key] for key in snapshot_fields}
    inputs = executor_contract.artifact_roster(identity["inputs"], snapshots_only=True)
    script = executor_contract.roster({worker_integration.SCRIPT: worker_integration._SCRIPT})[0]
    manifest = (
        json.dumps(
            {"snapshot_digest": identity["snapshot_digest"], "files": identity["files"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    files = identity["files"]
    store.require(
        retained["policy"] == policy
        and policy["resource_guard"] == packet["resource_guard"]
        and guard.bounds == resource_guard.PILOT_BOUNDS
        and guard.identity["boot_id"] == evidence["context"]["host"]["boot_id"]
        and identity["schema_version"] == VERSION
        and identity["recipe"] == worker_integration.RECIPE
        and identity["revision"] == packet["input_revision"]
        and identity["model_digest"] == packet["producer_policy"]["model"]["digest"]
        and identity["repository"] == native["handoff"]["receipt"]["repository"]
        and identity["allowed_paths"] == ["fixtures/unattended-pilot/inputs/source.txt"]
        and identity["protected_paths"] == packet["delegation"]["protected_paths"]
        and bool(identity["protected_paths"])
        and identity["snapshot_digest"] == planning.digest(snapshot)
        and identity["argv"] == policy["argv"] == list(worker_integration.ARGV)
        and identity["expected_outputs"] == policy["expected_outputs"] == [worker_integration.OUTPUT]
        and policy["runtime"]["python"] == worker_integration.ARGV[0]
        and policy["max_attempts"] == 1
        and policy["timeout_seconds"] == 30
        and policy["permitted_repository_effects"] == []
        and identity["recipe_digest"] == script["digest"]
        and planning.canonical(identity["claims"])
        == planning.canonical(
            {"execution_authorized": False, "verification_established": False, "evidence_admitted": False}
        )
        and isinstance(files, list)
        and len(files) == 1
        and set(files[0]) == {"path", "input", "mode", "oid", "size", "digest"}
        and files[0]["path"] == identity["allowed_paths"][0]
        and files[0]["input"] == "source/" + files[0]["path"]
        and files[0]["mode"] == "100644"
        and inputs
        == executor_contract.roster(
            {
                worker_integration.SCRIPT: worker_integration._SCRIPT,
                worker_integration.MANIFEST: manifest,
                files[0]["input"]: base64.b64decode(
                    _native_execution_inputs(evidence)[files[0]["input"]], validate=True
                ),
            }
        )
        and [item for item in retained["inputs"] if item["path"] != executor_contract.TASK_INPUT] == inputs,
        "retained execution differs from the approved fixed worker snapshots, recipe or resource envelope",
    )
    for attempt in native["execute"]["history"]["attempts"]:
        for event in attempt["events"]:
            if event["data"].get("kind") in {"worker", "namespace"}:
                store.require(
                    event["data"]["identity"]["boot_id"] == guard.identity["boot_id"],
                    "native worker process belongs to another resource-envelope host",
                )


def _native_execution_inputs(evidence: Doc) -> Doc:
    reservation = evidence["controller"]["event"]["correlations"]["authorizations"]["execute"]
    decision = next(
        item
        for item in evidence["accounting"]["decisions"]
        if item["request"]["proposal"]["id"] == reservation["proposal_id"]
    )
    return dict(decision["request"]["proposal"]["effect"]["request"]["native_request"]["inputs"])


def _standing_native_authority(evidence: Doc) -> None:
    """Join retained native exact grants to the full approved standing configuration, offline.

    Retained paths are identifiers here. This validator does not reopen operator files or guards.
    """
    packet, native = evidence["packet"], evidence["native"]
    delegated = packet["delegation"]
    store.require(
        set(delegated)
        == {
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
        "live proof lacks the full closed independently approved standing delegation",
    )
    authority_parser = importlib.import_module("exacly.automation_authorization")
    controller_parser = importlib.import_module("exacly.automation_controller")
    delegated = authority_parser.parse_retained_delegation(delegated)
    components = controller_parser.validate_components(packet["components"])
    workflow = controller_parser.validate_contract(packet["workflow_contract"])
    phases = ("dispatch", "execute", "handoff", "publish", "associate", "complete")
    adapters = dict(
        zip(
            phases,
            (
                "native-dispatch-v1",
                "native-worker-v1",
                "native-handoff-v1",
                "native-publication-v1",
                "native-association-v1",
                "native-completion-v1",
            ),
            strict=True,
        )
    )
    configuration = planning.digest({key: value for key, value in components.items() if key != "configuration_digest"})
    store.require(
        components["configuration_digest"]
        == delegated["native_configuration_digest"]
        == workflow["stores"]["configuration"]["id"]
        == configuration
        and delegated["contract"] == workflow["contract"]
        and delegated["repository"] == packet["repository"]
        and delegated["base_revision"] == packet["input_revision"]
        and delegated["allowed_paths"]
        == ["fixtures/unattended-pilot/inputs/source.txt", "fixtures/unattended-pilot/results/source-digests.json"]
        and delegated["destinations"] == [{"repository_id": REPOSITORY_ID, "ref": "refs/heads/" + BRANCH}]
        and delegated["actions"] == [native["completion"]["receipt"]["plan"]["action_id"]]
        and set(delegated["effects"]) == set(phases),
        "standing scope differs from the exact approved native component configuration or goal",
    )
    grants = {
        "dispatch": native["dispatch"]["record"]["intent"]["authorization"],
        "execute": native["execute"]["receipt"]["authorization"],
        "handoff": native["handoff"]["receipt"]["authorization"],
        "publish": native["publish"]["intent"]["authorization"],
        "associate": {"association_id": native["associate"]["association_id"]},
        "complete": {
            "invocation": "native-completion-v1",
            "request_digest": native["completion"]["receipt"]["request_digest"],
        },
    }
    actual_storage = {
        "dispatch": native["dispatch"]["record"]["intent"]["contract"]["dispatch_policy"]["storage"],
        "execute": native["execute"]["history"]["intent"]["contract"]["policy"]["storage"],
        "handoff": native["handoff"]["proposal"]["request"]["policy"]["storage"],
        "associate": native["associate"]["intent"]["request"]["storage"],
    }
    for kind, storage in actual_storage.items():
        store.require(
            components["native_stores"][kind] == {"path": storage["journal"], "id": storage["journal_id"]},
            "standing native store differs from the actual retained component instance",
        )
    publication = native["publish"]["intent"]["request"]
    anchor = _digest(components["ledger_anchor"])
    store.require(
        components["native_stores"]["publish"] == {"path": publication["journal"], "id": publication["journal_id"]}
        and components["native_stores"]["complete"] == {"path": components["ledger"], "id": anchor["digest"]}
        and anchor["ledger"] == components["ledger"],
        "standing publication or completion store differs from the exact retained native source",
    )
    correlations = evidence["controller"]["event"]["correlations"]
    for phase in phases:
        reservation = correlations["authorizations"][phase]
        decision = next(
            item
            for item in evidence["accounting"]["decisions"]
            if item["request"]["proposal"]["id"] == reservation["proposal_id"]
        )
        proposal = decision["request"]["proposal"]
        saved, scope = proposal["effect"], delegated["effects"][phase]
        facts, grant = authority_parser.parse_retained_facts(saved["facts"]), grants[phase]
        operation = next(
            grant[key]
            for key in ("dispatch_id", "execution_id", "handoff_id", "operation_id", "association_id", "request_digest")
            if key in grant
        )
        request = saved["request"]
        store.require(
            set(saved) == {"kind", "adapter_id", "request", "facts", "delegation_digest", "delegation"}
            and set(facts)
            == {
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
            }
            and set(request) == {"phase", "correlations", "native_request", "authorization", "controller"}
            and saved["kind"] == request["phase"] == phase
            and saved["adapter_id"] == scope["adapter_id"] == adapters[phase]
            and saved["delegation"] == delegated
            and proposal["delegation_id"] == delegated["id"]
            and proposal["policy_digest"] == saved["delegation_digest"] == planning.digest(delegated)
            and facts["objective_id"] == delegated["objective"]["id"]
            and facts["original_goal"] == packet["original_goal"]
            and facts["action_id"] == delegated["actions"][0]
            and facts["contract"] == delegated["contract"]
            and facts["repository"] == packet["repository"]
            and facts["base_revision"] == packet["input_revision"]
            and facts["native"] == {"store": components["native_stores"][phase], "operation_id": operation}
            and facts["native"]["store"] == scope["native_store"]
            and facts["capabilities"] == ["intent-before-effects-v1", adapters[phase]]
            and set(facts["capabilities"]) <= set(scope["capabilities"])
            and proposal["cost"] == facts["cost"] == components["costs"][phase]
            and all(facts["cost"][key] <= scope["max_cost"][key] for key in standing_authorization.DIMENSIONS)
            and proposal["authorization"] == facts["authorization"] == request["authorization"] == grant
            and proposal["preconditions"] == facts["preconditions"]
            and facts["preconditions"]["native_request_digest"] == planning.digest(request["native_request"])
            and facts["preconditions"]["native_configuration_digest"] == configuration
            and request["controller"] == {**components["controller"], "workflow_id": planning.digest(workflow)}
            and facts["source_paths"] == (["fixtures/unattended-pilot/inputs/source.txt"] if phase == "execute" else [])
            and facts["write_paths"]
            == (["fixtures/unattended-pilot/results/source-digests.json"] if phase == "handoff" else [])
            and facts["destination"]
            == ({"repository_id": REPOSITORY_ID, "ref": "refs/heads/" + BRANCH} if phase == "publish" else None),
            "native terminal receipt lacks its exact approved Authorizer Facts, grant, store and reservation",
        )
        document = request["native_request"]
        if phase in {"dispatch", "execute", "complete"}:
            store.require(
                document["plan"] == native["dispatch"]["record"]["intent"]["contract"]["plan"]
                and document["action_id"] == delegated["actions"][0]
                and planning.digest(document["readiness_policy"]) == workflow["contract"]["readiness"]
                and document["trust_policy"] == packet["producer_policy"],
                "standing native request changed the approved plan, action or complete policy",
            )
        if phase == "dispatch":
            store.require(
                document["dispatch_policy"]
                == native["dispatch"]["record"]["intent"]["contract"]["dispatch_policy"]
                == components["dispatch_policy"]
                and document["target"] == workflow["input_target"],
                "standing dispatch source changed",
            )
        elif phase == "execute":
            store.require(
                document["executor_policy"] == components["executor_policy"]
                and document["worker_identity"] == packet["worker_identity"]
                and document["resource_guard"] == packet["resource_guard"]["identity"],
                "standing worker request differs from the approved fixed worker or kernel envelope",
            )
        elif phase == "handoff":
            store.require(document == native["handoff"]["proposal"]["request"], "standing handoff request changed")
        elif phase == "publish":
            store.require(document == native["publish"]["intent"]["request"], "standing publication request changed")
        elif phase == "associate":
            store.require(
                all(document[key] == native["associate"]["intent"]["request"][key] for key in document),
                "standing association request changed",
            )
        elif phase == "complete":
            expected_request = {
                "binding_id": document["binding_id"],
                "state_path": document["state_path"],
                "plan_digest": document["plan"]["digest"],
                "action_id": document["action_id"],
                "readiness_policy_digest": planning.digest(document["readiness_policy"]),
                "trust_policy_digest": planning.digest(document["trust_policy"]),
                "target": document["output_target"],
                "run_id": document["run_id"],
                "attempt": document["attempt"],
                "as_of": document["date"],
            }
            store.require(
                native["completion"]["intent"]["request"] == expected_request
                and planning.digest(expected_request) == grant["request_digest"],
                "standing completion differs from the actual exact registered completion request",
            )


def validate_accounting(evidence: Doc) -> None:
    """Re-derive authoritative reserved totals and link each native effect to its settled grant."""
    packet, accounting = evidence["packet"], evidence["accounting"]
    delegated = packet["delegation"]
    store.require(
        accounting["store_id"] == delegated["accounting"]["store_id"]
        and accounting["objective"] == delegated["objective"]
        and accounting["objective"]["original_goal"] == packet["original_goal"]
        and accounting["limits"] == delegated["limits"],
        "accounting store, logical objective or immutable budgets changed",
    )
    decisions = accounting["decisions"]
    store.require(isinstance(decisions, list), "authoritative accounting history is unavailable")
    by_id: dict[str, Doc] = {}
    totals = dict.fromkeys(standing_authorization.DIMENSIONS, 0)
    for decision in decisions:
        proposal = decision["request"]["proposal"]
        original = {
            key: decision[key]
            for key in ("schema_version", "store_id", "objective_id", "request", "approved", "reason")
        }
        store.require(
            type(decision["approved"]) is bool and decision["digest"] == planning.digest(original),
            "authoritative budget decision content changed",
        )
        store.require(
            proposal["id"] not in by_id
            and decision["store_id"] == accounting["store_id"]
            and decision["objective_id"] == accounting["objective"]["id"],
            "duplicate or differently scoped budget decision",
        )
        by_id[proposal["id"]] = decision
        if decision["approved"]:
            for dimension in totals:
                cost = proposal["cost"][dimension]
                store.require(type(cost) is int and cost >= 0, "invalid retained reservation cost")
                totals[dimension] += cost
    store.require(
        totals == accounting["reserved"] and all(totals[key] <= accounting["limits"][key] for key in totals),
        "objective-wide accounting omitted, refunded or exceeded a retained reservation",
    )
    correlations = evidence["controller"]["event"]["correlations"]
    for phase in ("dispatch", "execute", "handoff", "publish", "associate", "complete"):
        reservation = correlations["authorizations"][phase]
        decision = by_id.get(reservation["proposal_id"])
        store.require(
            decision is not None
            and decision["approved"] is True
            and decision["status"] == "settled"
            and decision["digest"] == reservation["digest"]
            and decision["outcome"] == {"status": "succeeded", "receipt": correlations[phase]},
            "native effect lacks its exact settled standing-authority reservation",
        )


def _baseline(source: _BoundedSource, evidence: Doc, envelope: Doc, date: dt.date) -> None:
    packet = evidence["packet"]
    selection = packet["baseline_selection"]
    outcome = evidence["baseline"]["receipt"]["result"]
    store.require(
        outcome["negative_established"] is True
        and outcome["pending_unchanged"] is True
        and outcome["before_state_digest"]
        == outcome["after_state_digest"]
        == admission.sha256(envelope["before_state"])
        and outcome["producer"]["conclusion"] == "failure"
        and outcome["report"]["status"] == "pending"
        and outcome["report"]["admission"]["status"] == "admitted",
        "failed baseline did not preserve the exact registered pending goal",
    )
    observed = producer_selection.poll(
        source, packet["components"]["producer"], selection, timeout=min(30, source.deadline.remaining())
    )
    store.require(observed["status"] == "failed", "baseline was not an actual failed exact producer")
    target = Target(packet["workflow_contract"]["input_target"])
    admitted = admission.admit(
        source, packet["producer_policy"], target, selection["run_id"], selection["attempt"], date
    )
    store.require(
        admitted.receipt["status"] == "admitted" and admitted.archive is not None and admitted.report is not None,
        "failed baseline no longer has authentic producer evidence",
    )
    _pilot_protection(outcome["report"]["admission"])
    _pilot_protection(admitted.receipt)
    retained = outcome["evidence"]
    store.require(
        admitted.archive == _blob(retained, "archive", ARCHIVE_LIMIT)
        and admitted.report == _blob(retained, "report", LIMIT),
        "failed baseline bytes differ from fresh admission",
    )


def _pilot_protection(receipt: Doc) -> None:
    origin = receipt.get("origin", {})
    store.require(
        receipt.get("schema_version") == "0.2.0"
        and receipt.get("claims", {}).get("branch_protection_verified") is False
        and origin.get("branch_protection") == "not_required"
        and "branch_protection_checked_at" in origin
        and origin["branch_protection_checked_at"] is None
        and origin.get("protection_identity") is None
        and origin.get("branch_protection_identity") is None,
        "dedicated pilot receipt falsely claims verified branch protection",
    )


def _remote_chain(source: admission.Source, evidence: Doc) -> None:
    packet, native = evidence["packet"], evidence["native"]
    published = native["publish"]
    authorization = published["intent"]["authorization"]
    selected = native["handoff"]["proposal"]["request"]["mappings"]
    store.require(
        selected == packet["components"]["handoff"]["mappings"]
        and set(selected.values()) == {"fixtures/unattended-pilot/results/source-digests.json"},
        "handoff wrote paths outside the fixed independently approved fixture result",
    )

    base = "/repos/" + REPOSITORY
    commit = source.get_json(f"{base}/git/commits/{authorization['commit']}")
    store.require(
        commit.get("sha") == authorization["commit"]
        and commit.get("tree", {}).get("sha") == authorization["tree"]
        and [item.get("sha") for item in commit.get("parents", [])] == [authorization["base_revision"]],
        "actual remote commit differs from the exact native handoff",
    )
    before = admission.tree(source, base, authorization["base_revision"])
    after = admission.tree(source, base, authorization["commit"])
    worker = packet["worker_identity"]
    source_path = "fixtures/unattended-pilot/inputs/source.txt"
    original_source = admission.blob(source, base, before, source_path)
    source_entry = next(item for item in before if item["path"] == source_path)
    base_commit = source.get_json(f"{base}/git/commits/{authorization['base_revision']}")
    store.require(
        base_commit.get("sha") == worker["revision"]
        and base_commit.get("tree", {}).get("sha") == worker["tree"]
        and worker["files"][0]
        == {
            "path": source_path,
            "input": "source/" + source_path,
            "mode": source_entry["mode"],
            "oid": source_entry["sha"],
            "size": len(original_source),
            "digest": admission.sha256(original_source),
        },
        "approved fixed worker snapshot differs from actual immutable remote base source bytes",
    )
    old_files = {item["path"]: item for item in before if item.get("type") != "tree"}
    new_files = {item["path"]: item for item in after if item.get("type") != "tree"}
    changed = {
        path
        for path in old_files.keys() | new_files.keys()
        if {key: old_files.get(path, {}).get(key) for key in ("mode", "sha")}
        != {key: new_files.get(path, {}).get(key) for key in ("mode", "sha")}
    }
    store.require(changed == set(selected.values()), "remote publication changed unrelated or protected base files")
    artifacts = {item["path"]: item for item in native["execute"]["receipt"]["build"]["artifacts"]}
    for artifact, destination in selected.items():
        raw = admission.blob(source, base, after, destination)
        store.require(
            destination in new_files
            and new_files[destination].get("mode") == "100644"
            and artifacts[artifact]["size"] == len(raw)
            and artifacts[artifact]["digest"] == admission.sha256(raw),
            "actual remote output bytes differ from the retained isolated build artifact",
        )
    marker = authorization["operation_marker"]
    store.require(marker["ref"].startswith("refs/tags/req039-operations/"), "unsupported operation-marker destination")
    reference = source.get_json(f"{base}/git/ref/" + marker["ref"].removeprefix("refs/"))
    store.require(
        reference.get("ref") == marker["ref"]
        and reference.get("object", {}).get("type") == "tag"
        and reference["object"].get("sha") == marker["object"],
        "actual remote marker identity changed",
    )
    tag = source.get_json(f"{base}/git/tags/{marker['object']}")
    store.require(
        tag.get("sha") == marker["object"]
        and tag.get("tag") == marker["ref"].removeprefix("refs/tags/")
        and tag.get("object", {}).get("sha") == authorization["commit"]
        and tag.get("object", {}).get("type") == "commit"
        and tag.get("message") == marker["content"].split("\n\n", 1)[1],
        "actual remote operation proof differs from the native verified signed marker",
    )


def _metered_report(evidence: Doc, report: Doc, request: Doc, purpose: str, case: str) -> Doc:
    reservation = report["reservation"]
    accounting = evidence["accounting"]
    decision = next(
        (item for item in accounting["decisions"] if item["request"]["proposal"]["id"] == reservation["proposal_id"]),
        None,
    )
    store.require(decision is not None, "metered proof has no actual authoritative decision")
    assert decision is not None
    proposal = decision["request"]["proposal"]
    meter = proposal["effect"]["meter"]
    operation = planning.digest({"packet": evidence["packet"]["digest"], "case": case})
    store.require(
        reservation["store_id"] == accounting["store_id"]
        and reservation["objective_id"] == accounting["objective"]["id"]
        and reservation["digest"] == decision["digest"]
        and reservation["status"] == decision["status"]
        and report["kind"] == meter["purpose"] == purpose
        and report["effect_id"] == meter["operation_id"] == operation
        and meter["request"] == request
        and proposal["policy_digest"] == planning.digest(evidence["packet"]["delegation"])
        and proposal["delegation_id"] == evidence["packet"]["delegation"]["id"]
        and proposal["preconditions"]
        == {
            "approval_id": evidence["packet"]["delegation"]["approval_id"],
            "contract": evidence["packet"]["delegation"]["contract"],
        }
        and meter["adapter_id"] == evidence["packet"]["delegation"]["reads"][purpose]["adapter_id"]
        and 1 <= meter["wall_time_ms"] <= evidence["packet"]["delegation"]["reads"][purpose]["max_wall_time_ms"]
        and planning.digest(request["contract"])
        == evidence["packet"]["delegation"]["reads"][purpose]["contract_digest"]
        and proposal["id"]
        == planning.digest(
            {"store": accounting["store_id"], "objective": accounting["objective"]["id"], "slot": meter["slot"]}
        )
        and proposal["authorization"]
        == {
            "operation": "bounded-control-call",
            "purpose": purpose,
            "adapter_id": meter["adapter_id"],
            "slot": meter["slot"],
            "wall_time_ms": meter["wall_time_ms"],
        }
        and proposal["cost"]
        == {key: meter["wall_time_ms"] if key == "wall_time_ms" else 0 for key in standing_authorization.DIMENSIONS},
        "metered proof differs from its exact original-objective purpose, request, slot or reservation",
    )
    if report["status"] == "completed":
        store.require(
            decision["approved"] is True
            and decision["status"] == "settled"
            and decision["outcome"]
            == {"status": "succeeded", "receipt": {"result": report["receipt"]["result"], "slot": meter["slot"]}},
            "metered proof is not its actual settled native read result",
        )
    else:
        store.require(
            report["status"] == "denied"
            and decision["approved"] is False
            and decision["status"] == "denied"
            and report["receipt"] is None
            and report["reason"] == "Authoritative control budget exhausted: " + decision["reason"],
            "budget denial is not its actual denied capacity reservation",
        )
    return dict(meter)


def _rejected_admission(receipt: Doc, probe: Doc) -> None:
    _digest(receipt)
    store.require(
        set(receipt) == {"schema_version", "status", "policy", "target", "as_of", "claims", "checks", "digest"}
        and receipt["schema_version"] == "0.2.0"
        and receipt["status"] == "rejected"
        and receipt["target"] == Target(probe["target"]).data
        and receipt["policy"]["digest"] == probe["policy_digest"]
        and planning.canonical(receipt["claims"])
        == planning.canonical(
            {
                "scope": "origin_and_target_applicability",
                "branch_protection_verified": False,
                "test_honesty_verified": False,
                "coverage_verified": False,
                "self_authenticating": False,
            }
        )
        and isinstance(receipt["checks"], list)
        and len(receipt["checks"]) >= 1
        and all(
            set(check) == {"code", "passed", "reason"}
            and type(check["passed"]) is bool
            and isinstance(check["code"], str)
            and isinstance(check["reason"], str)
            for check in receipt["checks"]
        )
        and receipt["checks"][-1]["passed"] is False,
        "negative evidence is not a native rejected admission for the exact requested variant",
    )
    dt.date.fromisoformat(receipt["as_of"])


def _account_prefix(snapshot: Doc, final: Doc) -> set[str]:
    store.require(
        all(snapshot[key] == final[key] for key in ("store_id", "objective", "limits")),
        "negative/replay proof replaced the objective account or immutable budgets",
    )
    all_decisions = {item["request"]["proposal"]["id"]: item for item in final["decisions"]}
    chosen: set[str] = set()
    reserved = dict.fromkeys(standing_authorization.DIMENSIONS, 0)
    for item in snapshot["decisions"]:
        identity = item.get("id") or item.get("request", {}).get("proposal", {}).get("id")
        actual = all_decisions.get(identity)
        store.require(
            actual is not None
            and identity not in chosen
            and item["digest"] == actual["digest"]
            and item["status"] in {"reserved", "uncertain", "settled", "denied"},
            "accounting prefix is not retained in the actual final history",
        )
        assert actual is not None
        chosen.add(identity)
        if actual["approved"]:
            for dimension in reserved:
                reserved[dimension] += actual["request"]["proposal"]["cost"][dimension]
    store.require(reserved == snapshot["reserved"], "accounting prefix replenished or omitted reserved capacity")
    return chosen


def _preserved(proof: Doc, evidence: Doc, *, pending: bool) -> None:
    packet = evidence["packet"]
    store.require(
        proof["packet_digest"] == packet["digest"] and proof["host"] == evidence["context"]["host"],
        "negative proof belongs to another approval packet or physical host",
    )
    before, after = proof["before"], proof["after"]
    old_state, new_state = _blob(before, "state", LIMIT), _blob(after, "state", LIMIT)
    store.require(
        old_state == new_state and before["binding"] == after["binding"],
        "negative case changed native state or binding",
    )
    binding = executions.validate_record(before["binding"]["binding"])
    final_binding = evidence["native"]["binding"]["binding"]
    store.require(
        binding["pending_key"] == final_binding["pending_key"]
        and binding["contract"]["goal"] == final_binding["contract"]["goal"],
        "negative case selected another verification objective",
    )
    if pending:
        contract = binding["contract"]
        selected = contract["pending"]
        executions.selected_pending(
            state_writer.decode(old_state),
            selected["record"],
            selected["task"],
            selected["opened_at"],
            contract["adopted_verification"],
        )
    prior = _account_prefix(before["accounting"], evidence["accounting"])
    following = _account_prefix(after["accounting"], evidence["accounting"])
    store.require(
        prior <= following
        and all(
            after["accounting"]["reserved"][key] >= before["accounting"]["reserved"][key]
            for key in standing_authorization.DIMENSIONS
        ),
        "negative case reset consumed or uncertain objective reservations",
    )
    observed = admission.timestamp(proof["facts"]["observed_at"])
    store.require(
        admission.timestamp(evidence["context"]["created_at"])
        <= observed
        <= admission.timestamp(evidence["context"]["deadline_at"]),
        "negative case ran outside its approved lifetime",
    )


def validate_recovery_case(evidence: Doc, case: str) -> None:
    """Check actual exported native facts; this pure validation alone establishes no live origin."""
    store.require(case in REQUIRED_CASES, "unsupported live acceptance case")
    correlations = evidence["controller"]["event"]["correlations"]
    packet = evidence["packet"]
    if case == "dispatch_restart":
        marker = evidence["controller_crash"]
        restarts = evidence["recovery"]["controller_restarts"]
        store.require(
            marker["packet_digest"] == packet["digest"]
            and marker["phase"] == "dispatch"
            and marker["native_receipt"] == correlations["dispatch"]
            and isinstance(restarts, list)
            and len(restarts) == 1,
            "actual declared controller crash is missing",
        )
        actual = restarts[0]
        resumed = actual["after_restart"]
        store.require(
            actual["returncode"] == -9
            and type(actual["pid"]) is int
            and actual["pid"] > 0
            and actual["host"] == evidence["context"]["host"]
            and actual["run_attempt"] == evidence["context"]["run_attempt"]
            and actual["native_dispatch_receipt"] == correlations["dispatch"]
            and actual["before"]["event"]["phase"] == actual["after_termination"]["event"]["phase"] == "dispatch"
            and actual["before"]["sequence"]
            <= actual["after_termination"]["sequence"]
            < resumed["controller"]["sequence"]
            and resumed["returncode"] == 0
            and resumed["pid"] != actual["pid"]
            and resumed["controller"]["event"]["correlations"]["dispatch"] == correlations["dispatch"],
            "declared marker is not an actual same-host process loss and retained dispatch recovery",
        )
        return
    if case == "publication_response_loss":
        marker = evidence["remote_response_drop"]
        publication = evidence["native"]["publish"]
        phases = [event["phase"] for event in publication["history"]]
        store.require(
            marker["packet_digest"] == packet["digest"]
            and marker["operation_id"] == publication["operation_id"]
            and marker["actual_exchange_returned"] is True
            and phases.count("sent") == 1
            and "ambiguous" in phases
            and phases.index("sent") < phases.index("ambiguous") < phases.index("published"),
            "actual lost publication response was not reconciled without another send",
        )
        return
    if case == "duplicate_events":
        before, after = evidence["replay_checks"]["before"], evidence["replay_checks"]["after"]
        store.require(
            before == after
            and before["controller"] == evidence["controller"]
            and before["native"] == evidence["native"]
            and before["completion_evidence"] == evidence["completion_evidence"],
            "actual completed duplicate step changed native effects, completion or history",
        )
        _account_prefix(before["accounting"], evidence["accounting"])
        return
    names = {
        "divergent_ref": "divergent-ref",
        "producer_unavailable": "unavailable-producer",
        "wrong_revision": "wrong-revision",
        "wrong_attempt": "wrong-attempt",
        "criteria_changed": "changed-criteria",
        "verifier_changed": "changed-verifier",
        "delegation_revoked": "revoked-delegation",
        "delegation_expired": "expired-delegation",
        "objective_budget_exhausted": "exhausted-objective-budget",
    }
    proof = evidence.get("negative_cases", {}).get(names[case])
    store.require(
        isinstance(proof, dict) and proof.get("case") == names[case],
        f"required actual live case {names[case]} is missing",
    )
    assert isinstance(proof, dict)
    authority_case = case in {"delegation_revoked", "delegation_expired", "objective_budget_exhausted"}
    _preserved(proof, evidence, pending=not authority_case and case != "divergent_ref")
    report, facts = proof["report"], proof["facts"]
    if case in {"delegation_expired", "delegation_revoked"}:
        dispatch_reservation = correlations["authorizations"]["dispatch"]["proposal_id"]
        dispatch_decision = next(
            item
            for item in evidence["accounting"]["decisions"]
            if item["request"]["proposal"]["id"] == dispatch_reservation
        )
        store.require(
            set(report) == {"schema_version", "status", "kind", "effect_id", "reason", "reservation", "receipt"}
            and report["schema_version"] == VERSION
            and report["kind"] == "dispatch"
            and report["effect_id"] == planning.digest({"packet": packet["digest"], "case": names[case]})
            and report["reservation"] is None
            and report["receipt"] is None
            and proof["request"] == dispatch_decision["request"]["proposal"]["effect"]["request"],
            "lifetime/revocation case did not deny the declared new actual dispatch effect identity",
        )
    if case == "delegation_expired":
        expired = facts["expired_delegation"]
        store.require(
            report["status"] == "denied"
            and report["reason"] == "Standing delegation expired."
            and expired == packet["scope_tests"]["expired_delegation"]
            and expired["accounting"] == packet["delegation"]["accounting"]
            and expired["objective"] == packet["delegation"]["objective"]
            and expired["limits"] == packet["delegation"]["limits"]
            and admission.timestamp(expired["expires_at"]) <= admission.timestamp(facts["observed_at"])
            and proof["before"] == proof["after"],
            "another denial was substituted for actual delegation expiry",
        )
        return
    if case == "delegation_revoked":
        revoked = facts["revocation"]
        store.require(
            revoked["delegation_id"] == packet["delegation"]["id"]
            and report["status"] == "denied"
            and report["reason"] == "Standing delegation revoked: " + revoked["reason"]
            and admission.timestamp(revoked["revoked_at"]) <= admission.timestamp(facts["observed_at"])
            and proof["before"] == proof["after"],
            "another denial was substituted for actual revocation",
        )
        return
    if case == "objective_budget_exhausted":
        for actual in facts["reports"]:
            reservation = actual["reservation"]
            decision = next(
                item
                for item in evidence["accounting"]["decisions"]
                if item["request"]["proposal"]["id"] == reservation["proposal_id"]
            )
            request = decision["request"]["proposal"]["effect"]["meter"]["request"]
            store.require(
                request["contract"] == packet["scope_tests"]["budget"],
                "budget read selected another approved control contract",
            )
            _metered_report(evidence, actual, request, "budget-exhaustion", "exhausted-objective-budget")
        store.require(proof["request"] == request, "budget denial request differs from its actual final reservation")
        account = proof["after"]["accounting"]
        store.require(
            account["reserved"]["wall_time_ms"] == account["limits"]["wall_time_ms"]
            and facts["remaining_wall_time_ms"] == 0
            and facts["reports"][-1] == report
            and report["status"] == "denied"
            and report["reason"].startswith("Authoritative control budget exhausted:"),
            "another denial was substituted for exhausted original-objective reserved capacity",
        )
        return
    store.require(
        report["status"] == "completed"
        and report["receipt"]["result"] == {key: value for key, value in facts.items() if key != "observed_at"},
        "negative result is not its retained actual metered native read",
    )
    _metered_report(
        evidence,
        report,
        proof["request"],
        "remote-precondition" if case == "divergent_ref" else "negative-admission",
        names[case],
    )
    if case == "divergent_ref":
        native = _digest(facts["report"])
        request = native["intent"]["request"]
        store.require(
            native["status"] == "conflict"
            and native["observation"]["revision"] == correlations["handoff"]["commit"]
            and request["expected_revision"] == packet["input_revision"] != native["observation"]["revision"]
            and request["ref"] == "refs/heads/" + BRANCH
            and request["repository_id"] == str(REPOSITORY_ID)
            and request["operation_marker_ref"]
            == packet["scope_tests"]["remote_precondition"]["request_base"]["operation_marker_ref"]
            and native["claims"]["remote_published"] is False
            and native["claims"]["effect_authorized"] is False
            and facts["probe_semantics"] == "genuine-prior-base-versus-actual-main-publication-result"
            and facts["external_actor_mutation_demonstrated"] is False,
            "readonly actual ref conflict was replaced by a synthetic mismatch or actor-race claim",
        )
        return
    rejected = facts["admission"]
    control = _digest(facts["control_admission"])
    _rejected_admission(rejected, facts["probe"])
    store.require(
        proof["request"]["contract"] == packet["components"]["negatives"] and proof["request"]["case"] == names[case],
        "negative admission did not select its exact independently approved contract and case",
    )
    store.require(
        control["status"] == "admitted"
        and rejected["status"] == "rejected"
        and control["target"] == correlations["target"]
        and control["origin"]["run_id"] == correlations["producer_selection"]["run_id"]
        and control["origin"]["archive_digest"]
        == evidence["native"]["completion"]["receipt"]["evidence"]["archive_digest"]
        and facts["before_state_digest"]
        == facts["after_state_digest"]
        == admission.sha256(_blob(proof["before"], "state", LIMIT))
        and facts["pending_unchanged"] is True
        and facts["negative_established"] is True,
        "negative admission lacks actual successful control evidence and unchanged native pending state",
    )
    _pilot_protection(control)
    probe = facts["probe"]
    approved_probe = packet["components"]["negatives"]
    expected_policy = json.loads(planning.canonical(packet["producer_policy"]))
    if case == "criteria_changed":
        expected_policy["model"]["digest"] = approved_probe["criteria_model_digest"]
    if case == "verifier_changed":
        expected_policy["observer"]["digest"] = approved_probe["verifier_digest"]
    store.require(
        probe["target"] == (approved_probe["target"] if case == "wrong_revision" else control["target"])
        and probe["run_id"]
        == (approved_probe["unavailable_run_id"] if case == "producer_unavailable" else control["origin"]["run_id"])
        and probe["attempt"] == (approved_probe["wrong_attempt"] if case == "wrong_attempt" else 1)
        and probe["policy_digest"] == planning.digest(expected_policy),
        "negative result selected another target, run, attempt or policy than its approved actual probe",
    )
    if case == "wrong_revision":
        store.require(
            probe["target"]["revision"] != control["target"]["revision"]
            and probe["run_id"] == control["origin"]["run_id"],
            "wrong revision case did not vary the actual target",
        )
    elif case == "wrong_attempt":
        store.require(
            type(probe["attempt"]) is int and probe["attempt"] != 1 and probe["run_id"] == control["origin"]["run_id"],
            "wrong attempt case did not vary the exact attempt",
        )
    elif case == "producer_unavailable":
        store.require(
            probe["run_id"] != control["origin"]["run_id"] and probe["target"] == control["target"],
            "unavailable producer case selected the actual successful run",
        )
    else:
        expected_semantics = (
            "expected-model-digest-mismatch" if case == "criteria_changed" else "expected-observer-digest-mismatch"
        )
        store.require(
            probe["semantics"] == expected_semantics
            and probe["policy_digest"] != planning.digest(packet["producer_policy"])
            and facts["claims"]["changed_remote_verifier_demonstrated"] is False,
            "expected contract mismatch is not an actual protected verifier mutation",
        )


def validate_required_cases(evidence: Doc) -> None:
    """Missing declared/required cases are failures; generic 'passed' flags are never proof."""
    for case in REQUIRED_CASES:
        validate_recovery_case(evidence, case)


def _timestamp_precision(value: str) -> dt.timedelta:
    # GitHub commonly represents whole seconds. Permit only that represented unit's
    # rounding interval, rather than adding a general clock-skew allowance.
    fraction = re.search(r"\.([0-9]+)(?:Z|[+-][0-9]{2}:[0-9]{2})$", value)
    return dt.timedelta(microseconds=10 ** (6 - min(6, len(fraction[1])))) if fraction else dt.timedelta(seconds=1)


def verify_live(
    source: admission.Source,
    reference: Doc,
    expected_implementation: Doc,
    approval_verifier: Callable[[Doc, Doc], None],
    *,
    now: dt.datetime | None = None,
    timeout: float = 120,
) -> Doc:
    """Authenticate the selected actual workflow export and freshly admit its exact producer.

    expected_implementation comes from current_implementation of this checkout, never the artifact.
    approval_verifier must read the original human comment through the independently chosen channel.
    This operation is read-only and grants no execution, state import or completion authority.
    """
    selected = _reference(reference)
    store.require(
        expected_implementation.get("schema_version") == VERSION
        and expected_implementation.get("paths") == IMPLEMENTATION_PATHS
        and expected_implementation.get("excluded_paths") == EXCLUDED_PATHS
        and isinstance(expected_implementation.get("digest"), str)
        and store.DIGEST.fullmatch(expected_implementation["digest"]),
        "current implementation fingerprint is required",
    )
    bounded = _BoundedSource(source, timeout)
    base = "/repos/" + REPOSITORY
    run = bounded.get_json(f"{base}/actions/runs/{selected['run_id']}/attempts/{selected['attempt']}")
    store.require(
        run.get("id") == selected["run_id"]
        and run.get("run_attempt") == selected["attempt"]
        and run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and run.get("repository", {}).get("id") == run.get("head_repository", {}).get("id") == REPOSITORY_ID
        and run.get("path") in (COORDINATOR, COORDINATOR + "@refs/heads/main")
        and run.get("status") == "completed"
        and run.get("conclusion") == "success",
        "selected coordinator run is not the actual approved successful single-consumer workflow",
    )
    entries = admission.tree(bounded, base, run["head_sha"])
    store.require(
        admission.observer_digest(_runtime_entries(entries), IMPLEMENTATION_PATHS) == expected_implementation["digest"]
        and _requirements([admission.blob(bounded, base, entries, path) for path in _CONTRACT_PATHS])
        == expected_implementation.get("requirements_digest"),
        "live proof executed stale or different implementation/CI bytes",
    )
    workflow = bounded.get_json(f"{base}/actions/workflows/{run['workflow_id']}")
    store.require(
        workflow.get("id") == run["workflow_id"] and workflow.get("path") == COORDINATOR,
        "coordinator workflow identity changed",
    )
    evidence, metadata = _artifact(bounded, selected)
    store.require(
        evidence.get("schema_version") == VERSION and evidence.get("kind") == "req039-live-acceptance",
        "artifact is not native live acceptance evidence",
    )
    packet = _digest(evidence["packet"])
    context = evidence["context"]
    implementation = evidence["implementation"]
    store.require(
        packet.get("scope") == "req039-fixed-single-consumer-same-runner"
        and packet.get("repository") == {"id": REPOSITORY_ID, "full_name": REPOSITORY}
        and packet.get("consumer_branch") == BRANCH
        and packet.get("original_goal") == ORIGINAL_GOAL
        and packet["context"] == context
        and packet["implementation"] == implementation
        and implementation == {**expected_implementation, "revision": run["head_sha"]}
        and context["coordinator_revision"] == run["head_sha"]
        and context["run_id"] == str(selected["run_id"])
        and context["run_attempt"] == str(selected["attempt"])
        and metadata["workflow_run"].get("head_sha") == run["head_sha"],
        "approved packet or executed implementation identity differs from actual coordinator",
    )
    store.require(
        packet["producer_policy"]["repository"] == packet["repository"]
        and packet["producer_policy"]["branch"] == BRANCH
        and packet["producer_policy"].get("branch_protection") == "not_required"
        and packet["components"]["profile"] == "single-producer-local-v1"
        and packet["components"]["required_members"] == ["producer"],
        "live proof has an unsupported consumer, producer or composition scope",
    )
    jobs = bounded.get_json(
        f"{base}/actions/runs/{selected['run_id']}/attempts/{selected['attempt']}/jobs?per_page=100"
    )
    store.require(
        jobs.get("total_count") == 1 and isinstance(jobs.get("jobs"), list) and len(jobs["jobs"]) == 1,
        "coordinator job origin is unavailable or ambiguous",
    )
    job = jobs["jobs"][0]
    uploads = [step for step in job.get("steps", []) if step.get("name") == UPLOAD_STEP]
    store.require(
        str(job.get("id")) == context["host"]["job_id"]
        and job.get("run_id") == selected["run_id"]
        and job.get("head_sha") == run["head_sha"]
        and job.get("name") == context["job_key"]
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and len(uploads) == 1
        and uploads[0].get("status") == "completed"
        and uploads[0].get("conclusion") == "success",
        "live artifact lacks its exact successful trusted workflow upload step",
    )
    approval_verifier(packet, evidence["approval"])
    envelope = validate_native(evidence)
    _fixed_native_goal(evidence)
    validate_accounting(evidence)
    _standing_native_authority(evidence)
    _fixed_executor(evidence)
    _remote_chain(bounded, evidence)
    correlations = evidence["controller"]["event"]["correlations"]
    producer_contract = packet["components"]["producer"]
    selection = correlations["producer_selection"]
    observed = producer_selection.poll(
        bounded, producer_contract, selection, timeout=min(30, bounded.deadline.remaining())
    )
    store.require(observed["status"] == "succeeded", "exact approved push producer is not currently successful")
    timestamp = now if now is not None else dt.datetime.now(dt.UTC)
    store.require(timestamp.utcoffset() is not None, "live verification requires an explicit timezone")
    _baseline(bounded, evidence, envelope, timestamp.date())
    admitted = admission.admit(
        bounded,
        packet["producer_policy"],
        Target(correlations["target"]),
        selection["run_id"],
        selection["attempt"],
        timestamp.date(),
    )
    store.require(
        admitted.receipt.get("status") == "admitted"
        and admitted.archive == envelope["archive"]
        and admitted.report == envelope["report"],
        "fresh live admission differs from native completion evidence",
    )
    _pilot_protection(envelope["admission_receipt"])
    _pilot_protection(admitted.receipt)
    completion_time = admission.timestamp(envelope["receipt"]["completed_at"])
    store.require(
        admission.timestamp(run["created_at"]) <= completion_time <= timestamp
        and completion_time
        < admission.timestamp(metadata["created_at"]) + _timestamp_precision(metadata["created_at"]),
        "native completion timing is outside the actual coordinator/artifact lifetime",
    )
    validate_required_cases(evidence)
    return {
        "schema_version": VERSION,
        "status": "verified_live_chain",
        "reference": selected,
        "artifact_digest": metadata["digest"],
        "implementation": implementation,
        "evidence": evidence,
        "fresh_admission": admitted.receipt,
        "claims": {"authority_granted": False, "state_imported": False, "completion_invoked": False},
    }
