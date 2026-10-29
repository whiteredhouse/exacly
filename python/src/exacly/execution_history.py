"""Audit-only history format with required exact-byte baselines and fresh semantic audit.

The protected operator runtime establishes this baseline during explicit copy-only maintenance.
Neither hashes nor verdicts authenticate their writer or establish the latest global generation.
Version 3 deliberately has no registration, completion, mutation, or semantic-reuse path.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import inspect
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import jsonschema

from exacly import admission, completion_store, core, executions, planning, state_writer
from exacly import execution_store as store
from exacly.github_evidence import ARCHIVE_LIMIT

Doc = dict[str, Any]
VERSION = 3
FAMILY = "exacly-ledger-audit-v1"
CODEC = "typed-cells-v1"
DOMAIN = b"EXACLY-HISTORY\x00\x01"
SCHEMA = {
    "history_integrity": (
        "CREATE TABLE history_integrity (table_name TEXT NOT NULL, row_key TEXT NOT NULL, "
        "row_digest TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK (byte_count >= 0), "
        "cell_count INTEGER NOT NULL CHECK (cell_count > 0), PRIMARY KEY (table_name,row_key))"
    ),
    "history_profile": (
        "CREATE TABLE history_profile (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "profile_json TEXT NOT NULL, roster_digest TEXT NOT NULL, row_count INTEGER NOT NULL, "
        "cell_count INTEGER NOT NULL, byte_count INTEGER NOT NULL)"
    ),
    "history_semantics": (
        "CREATE TABLE history_semantics (intent_id TEXT PRIMARY KEY NOT NULL REFERENCES completion_intents(id), "
        "profile_digest TEXT NOT NULL, dependencies_digest TEXT NOT NULL, verdict_json TEXT NOT NULL, "
        "verdict_digest TEXT NOT NULL)"
    ),
}

# This code-owned registry defines the source-cell contract; a ledger cannot select its own fields.
# Each tuple is (column, exact SQLite storage class, byte limit, nullable).
_TEXT = store.RECORD_LIMIT
_FIELDS: dict[str, tuple[tuple[str, str, int, bool], ...]] = {
    "bindings": (
        ("id", "text", 256, False),
        ("pending_key", "text", 256, False),
        ("supersedes", "text", 256, True),
        ("record_json", "text", _TEXT, False),
    ),
    "active": (("pending_key", "text", 256, False), ("binding_id", "text", 256, False)),
    "watermarks": (
        ("pending_key", "text", 256, False),
        ("serial", "integer", 32, False),
        ("digest", "text", 256, False),
    ),
    "completion_intents": (
        ("id", "text", 256, False),
        ("binding_id", "text", 256, False),
        ("pending_key", "text", 256, False),
        ("request_digest", "text", 256, False),
        ("phase", "text", 32, False),
        ("intent_json", "text", _TEXT, False),
        ("before_state", "blob", completion_store.STATE_LIMIT, False),
        ("after_state", "blob", completion_store.STATE_LIMIT, False),
        ("archive", "blob", ARCHIVE_LIMIT, False),
        ("report", "blob", admission.REPORT_LIMIT, False),
        ("admission_json", "text", _TEXT, False),
        ("model_json", "text", _TEXT, False),
        ("admitted_state_json", "text", _TEXT, False),
    ),
    "completion_receipts": (
        ("binding_id", "text", 256, False),
        ("intent_id", "text", 256, False),
        ("request_digest", "text", 256, False),
        ("receipt_json", "text", _TEXT, False),
    ),
}
_KEYS = {
    "bindings": "id",
    "active": "pending_key",
    "watermarks": "pending_key",
    "completion_intents": "id",
    "completion_receipts": "intent_id",
}
_INDEX_FIELDS: dict[str, tuple[tuple[str, str, int, bool], ...]] = {
    "history_integrity": (
        ("table_name", "text", 128, False),
        ("row_key", "text", 256, False),
        ("row_digest", "text", 128, False),
        ("byte_count", "integer", 32, False),
        ("cell_count", "integer", 32, False),
    ),
    "history_profile": (
        ("singleton", "integer", 32, False),
        ("profile_json", "text", _TEXT, False),
        ("roster_digest", "text", 128, False),
        ("row_count", "integer", 32, False),
        ("cell_count", "integer", 32, False),
        ("byte_count", "integer", 32, False),
    ),
    "history_semantics": (
        ("intent_id", "text", 256, False),
        ("profile_digest", "text", 128, False),
        ("dependencies_digest", "text", 128, False),
        ("verdict_json", "text", _TEXT, False),
        ("verdict_digest", "text", 128, False),
    ),
}
_REQUIRED_MODULES = (
    "admission",
    "applicability",
    "completion",
    "completion_state",
    "completion_store",
    "core",
    "execution_history",
    "execution_store",
    "executions",
    "github_evidence",
    "manual_qa",
    "planning",
    "policy",
    "query",
    "reconciliation",
    "refresh",
    "state_writer",
    "views",
)
_STDLIB = (
    "json",
    "xml",
    "zipfile",
    "sqlite3",
    "datetime",
    "re",
    "hashlib",
    "struct",
    "codecs",
    "encodings",
    "_json",
    "_elementtree",
    "pyexpat",
    "_sqlite3",
    "_datetime",
    "_hashlib",
    "zlib",
)


class HistoryError(store.StoreError):
    """Required history bytes, associations, or fresh semantics failed validation."""


class UnsupportedProfileError(HistoryError):
    """This installation has no trusted compatibility rule for the retained profile."""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise HistoryError(message)


def _execute(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
    deadline: store.Deadline | None = None,
) -> sqlite3.Cursor:
    if deadline is not None:
        store.busy_timeout(connection, deadline)
    return connection.execute(statement, parameters)


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _part(name: str, kind: str, value: bytes | None, *, present: bool = True) -> bytes:
    """Frame field identity, declared type, explicit missing/null/value, and raw byte length."""
    label, tag = name.encode("utf-8"), kind.encode("ascii")
    presence = b"\x00" if not present else b"\x01" if value is None else b"\x02"
    raw = b"" if value is None else value
    return (
        len(label).to_bytes(8, "big")
        + label
        + len(tag).to_bytes(8, "big")
        + tag
        + presence
        + len(raw).to_bytes(8, "big")
        + raw
    )


def _transcript(domain: str, fields: list[tuple[str, str, bytes | None]]) -> bytes:
    return DOMAIN + _part("domain", "text", domain.encode()) + b"".join(_part(*field) for field in fields)


def _file(path: Path) -> bytes:
    _require(path.is_file(), f"installed validation profile input is missing: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise HistoryError(f"cannot read installed validation profile input: {path}") from error


def _tree(files: list[tuple[str, Path]]) -> tuple[str, int]:
    _require(bool(files), "installed validation profile has an empty artifact set")
    digest = hashlib.sha256(DOMAIN + _part("domain", "text", b"installed-artifacts-v1"))
    size = 0
    for name, path in sorted(files):
        content = _file(path)
        size += len(content)
        digest.update(_part(name, "blob", content))
    return "sha256:" + digest.hexdigest(), size


def _distribution_inputs() -> list[Doc]:
    """Conservative installed dependency closure, including available optional checker extras."""
    mandatory = {"jsonschema", "pyyaml", "attrs", "referencing", "rpds-py", "jsonschema-specifications"}
    if sys.version_info < (3, 13):
        mandatory.add("typing-extensions")
    pending = [(name, True) for name in sorted(mandatory)]
    seen: set[str] = set()
    results: list[Doc] = []
    packages = metadata.packages_distributions()
    while pending:
        name, required = pending.pop()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in seen:
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            _require(
                not required and normalized not in mandatory, f"installed validation dependency is missing: {name}"
            )
            continue
        seen.add(normalized)
        roster = distribution.files
        _require(roster is not None, f"installed dependency has no artifact roster: {name}")
        assert roster is not None
        artifacts = [
            (str(item), Path(str(distribution.locate_file(item))))
            for item in roster
            if not str(item).endswith((".pyc", ".pyo")) and "/__pycache__/" not in str(item)
        ]
        digest, size = _tree(artifacts)
        results.append({"name": f"dependency/{normalized}", "digest": digest, "size": size})
        # Distribution metadata can describe a different copy from the package Python imports.
        # Bind the actual module search result as well, including installed shadow packages.
        for module_name, owners in sorted(packages.items()):
            if normalized not in {re.sub(r"[-_.]+", "-", owner).lower() for owner in owners}:
                continue
            spec = importlib.util.find_spec(module_name)
            _require(spec is not None, f"installed dependency module is missing: {module_name}")
            assert spec is not None
            actual: list[tuple[str, Path]] = []
            if spec.submodule_search_locations is not None:
                for index, folder in enumerate(spec.submodule_search_locations):
                    root = Path(folder)
                    actual.extend(
                        (f"{index}/{item.relative_to(root).as_posix()}", item)
                        for item in root.rglob("*")
                        if item.is_file() and "__pycache__" not in item.parts and item.suffix not in (".pyc", ".pyo")
                    )
            else:
                _require(
                    spec.origin is not None and spec.origin not in ("built-in", "frozen"),
                    f"unfingerprintable dependency module: {module_name}",
                )
                assert spec.origin is not None
                actual.append((Path(spec.origin).name, Path(spec.origin)))
            digest, size = _tree(actual)
            results.append({"name": f"dependency-code/{normalized}/{module_name}", "digest": digest, "size": size})
        # Ignoring markers intentionally over-invalidates: every installed declared extra contributes.
        for requirement in distribution.requires or []:
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
            _require(match is not None, f"unsupported installed dependency requirement: {requirement}")
            assert match is not None
            pending.append((match.group(), ";" not in requirement))
    return results


def profile() -> Doc:
    """Identify trusted installed code, schemas, parsers and runtime; never accept a ledger's roster."""
    package = Path(__file__).resolve().parent
    for name in _REQUIRED_MODULES:
        _file(package / f"{name}.py")
    inputs: list[Doc] = []
    code = [(path.relative_to(package).as_posix(), path) for path in package.rglob("*.py")]
    digest, size = _tree(code)
    inputs.append({"name": "exacly/python", "digest": digest, "size": size})
    # Use the same precedence as schema_for. A wheel must carry its schemas; it cannot use a checkout.
    bundled = package / "schemas"
    source_layout = (
        package.parent.name == "src"
        and package.parent.parent.name == "python"
        and (package.parents[2] / "pyproject.toml").is_file()
    )
    roots = core.schema_dirs() if source_layout else [bundled]
    selected: dict[str, Path] = {}
    for root in roots:
        for path in sorted(root.rglob("*.json")):
            selected.setdefault(path.relative_to(root).as_posix(), path)
    for required in ("0.1", "0.2/config", "0.2/state"):
        _require(any(str(Path(name).parent) == required for name in selected), f"installed schemas missing: {required}")
    digest, size = _tree(list(selected.items()))
    inputs.append({"name": "exacly/schemas", "digest": digest, "size": size})
    inputs.extend(_distribution_inputs())
    for name in _STDLIB:
        try:
            module = importlib.import_module(name)
        except ImportError as error:
            raise HistoryError(f"installed validation runtime profile is unsupported: missing {name}") from error
        module_path = getattr(module, "__file__", None)
        if module_path is None:
            _require(name in sys.builtin_module_names, f"unfingerprintable runtime module: {name}")
            continue
        origin = Path(module_path)
        files = (
            [(p.relative_to(origin.parent).as_posix(), p) for p in origin.parent.rglob("*.py")]
            if origin.name == "__init__.py"
            else [(origin.name, origin)]
        )
        digest, size = _tree(files)
        inputs.append({"name": f"runtime/{name}", "digest": digest, "size": size})
    executable = _file(Path(sys.executable).resolve())
    inputs.append({"name": "runtime/python", "digest": _hash(executable), "size": len(executable)})
    checkers: dict[str, Doc] = {}
    for label, checker in (("jsonschema", jsonschema.FormatChecker()), ("state", state_writer.FORMATS)):
        for name, (function, raises) in sorted(checker.checkers.items()):
            try:
                source = inspect.getsource(function).encode()
            except (OSError, TypeError) as error:
                raise HistoryError(f"unfingerprintable registered format checker: {name}") from error
            exceptions = raises if isinstance(raises, tuple) else (raises,)
            checkers[f"{label}/{name}"] = {
                "function": f"{function.__module__}.{function.__qualname__}",
                "digest": _hash(source),
                "raises": [f"{item.__module__}.{item.__qualname__}" for item in exceptions],
            }
    with closing(sqlite3.connect(":memory:")) as runtime_connection:
        sqlite_source = runtime_connection.execute("SELECT sqlite_source_id()").fetchone()[0]
    result: Doc = {
        "format_version": "0.1.0",
        "family": FAMILY,
        "codec": CODEC,
        "inputs": sorted(inputs, key=lambda item: item["name"]),
        "runtime": {
            "python": sys.version,
            "implementation": sys.implementation.name,
            "sqlite": sqlite3.sqlite_version,
            "sqlite_source": sqlite_source,
        },
        "format_checkers": checkers,
    }
    result["digest"] = planning.digest(result)
    return result


def _cell_bounds(
    connection: sqlite3.Connection,
    table: str,
    fields: tuple[tuple[str, str, int, bool], ...],
    deadline: store.Deadline | None,
) -> None:
    # Check before fetching payloads. CAST alone cannot distinguish identical TEXT/BLOB octets.
    expressions = [f"typeof({name}),length(CAST({name} AS BLOB))" for name, _, _, _ in fields]
    for sizes in _execute(connection, f"SELECT {','.join(expressions)} FROM {table}", deadline=deadline):
        for index, (name, kind, limit, nullable) in enumerate(fields):
            actual, size = sizes[index * 2 : index * 2 + 2]
            _require(
                (actual == "null" and nullable and size is None)
                or (actual == kind and type(size) is int and 0 <= size <= limit),
                f"history cell has invalid type or size: {table}.{name}",
            )


def _rows(connection: sqlite3.Connection, deadline: store.Deadline | None) -> list[Doc]:
    rows: list[Doc] = []
    for table, fields in sorted(_FIELDS.items()):
        names = [item[0] for item in fields]
        _cell_bounds(connection, table, fields, deadline)
        expression = ",".join(f"CAST({name} AS BLOB)" for name in names)
        for values in _execute(
            connection, f"SELECT {expression} FROM {table} ORDER BY {_KEYS[table]}", deadline=deadline
        ):
            key = values[names.index(_KEYS[table])]
            _require(isinstance(key, bytes), "history row identity is missing")
            try:
                identity = key.decode("utf-8")
                for (_, kind, _, _), value in zip(fields, values, strict=True):
                    if kind == "text" and value is not None:
                        value.decode("utf-8")
            except UnicodeError as error:
                raise HistoryError("history TEXT cell is invalid UTF-8") from error
            transcript = _transcript(
                "source-row-v1",
                [("table", "text", table.encode())]
                + [(name, kind, value) for (name, kind, _, _), value in zip(fields, values, strict=True)],
            )
            rows.append(
                {
                    "table_name": table,
                    "row_key": identity,
                    "row_digest": _hash(transcript),
                    "byte_count": sum(len(value) for value in values if value is not None),
                    "cell_count": len(fields),
                }
            )
    return sorted(rows, key=lambda row: (row["table_name"], row["row_key"]))


def _roster(rows: list[Doc]) -> str:
    return _hash(
        _transcript(
            "required-roster-v1", [(str(index), "row", store.canonical(row).encode()) for index, row in enumerate(rows)]
        )
    )


def _dependencies(lookup: Mapping[tuple[str, str], Doc], identity: str, verdict: Doc) -> str:
    # Payload interpretation depends on these exact rows, including all nested model/policy/proof
    # bytes. Supersession, active membership, watermarks and the complete roster stay fresh global
    # checks; unrelated historical appends need not invalidate this terminal payload association.
    binding_id = verdict["binding_id"]
    intent = lookup.get(("completion_intents", identity))
    binding = lookup.get(("bindings", binding_id))
    receipt = lookup.get(("completion_receipts", identity))
    _require(intent is not None and binding is not None, "history terminal dependency is missing")
    _require(
        (receipt is not None) == (verdict["receipt_digest"] is not None),
        "history terminal receipt association is inconsistent",
    )
    return _hash(
        _transcript(
            "terminal-dependencies-v1",
            [
                ("intent", "text", identity.encode()),
                ("phase", "text", verdict["phase"].encode()),
                ("intent-row", "source-row", store.canonical(intent).encode()),
                ("binding-row", "source-row", store.canonical(binding).encode()),
                ("receipt-row", "source-row", store.canonical(receipt).encode() if receipt is not None else None),
            ],
        )
    )


def _fresh(connection: sqlite3.Connection, deadline: store.Deadline | None) -> tuple[int, dict[str, Doc]]:
    records, active, watermarks = store._base(connection, deadline=deadline)
    for binding in records.values():
        executions.validate_record(binding)
    completion_store.validate(connection, records, active, watermarks, deadline=deadline)
    verdicts: dict[str, Doc] = {}
    for identity, binding_id, pending, request, phase in _execute(
        connection,
        "SELECT id,binding_id,pending_key,request_digest,phase FROM completion_intents ORDER BY id",
        deadline=deadline,
    ):
        _require(
            phase != "prepared", "audit-only history cannot contain prepared intents; recover the v2 source explicitly"
        )
        envelope = completion_store._load(connection, identity, deadline)
        metadata_doc = envelope["intent"]
        verdicts[identity] = {
            "schema_version": "0.1.0",
            "intent_id": identity,
            "binding_id": binding_id,
            "pending_key": pending,
            "request_digest": request,
            "phase": phase,
            "state": metadata_doc["state"],
            "evidence": metadata_doc["evidence"],
            "receipt_digest": planning.digest(envelope["receipt"]) if envelope["receipt"] is not None else None,
            "valid": True,
        }
    return len(records), verdicts


def _facts(installed: Doc, rows: list[Doc], bindings: int, verdicts: Mapping[str, Doc], semantic_rows: int) -> Doc:
    return {
        "format_version": VERSION,
        "profile": installed,
        "integrity": {
            "algorithm": "sha256",
            "roster_digest": _roster(rows),
            "rows": len(rows),
            "cells": sum(row["cell_count"] for row in rows),
            "bytes": sum(row["byte_count"] for row in rows),
            "complete": True,
        },
        "semantics": {"fresh": len(verdicts), "reused": 0, "bindings": bindings},
        "index_rows": {"integrity": len(rows), "semantic": semantic_rows},
    }


def _connection(connection: sqlite3.Connection, deadline: store.Deadline | None) -> None:
    _require(connection.in_transaction, "history validation requires a protected coherent SQLite transaction")
    _require(
        _execute(connection, "PRAGMA encoding", deadline=deadline).fetchone() == ("UTF-8",),
        "history format requires UTF-8 SQLite text encoding",
    )
    databases = _execute(connection, "PRAGMA database_list", deadline=deadline).fetchall()
    _require(
        all(row[1] in ("main", "temp") for row in databases)
        and _execute(connection, "SELECT 1 FROM sqlite_temp_schema LIMIT 1", deadline=deadline).fetchone() is None,
        "history validation requires no attached databases or temporary objects",
    )


def _source(
    connection: sqlite3.Connection, deadline: store.Deadline | None
) -> tuple[Doc, list[Doc], int, dict[str, Doc]]:
    """The identical compatibility and full semantic gate for dry-run and real conversion."""
    _connection(connection, deadline)
    _require(
        _execute(connection, "PRAGMA user_version", deadline=deadline).fetchone() == (store.COMPLETION_VERSION,),
        "history initialization requires an exact v2 ledger",
    )
    store._structure(
        connection, {**store.SCHEMA, **completion_store.SCHEMA}, store.COMPLETION_VERSION, deadline=deadline
    )
    _require(
        _execute(
            connection, "SELECT 1 FROM completion_intents WHERE phase='prepared' LIMIT 1", deadline=deadline
        ).fetchone()
        is None,
        "history initialization rejects prepared intents; recover the v2 source explicitly",
    )
    rows = _rows(connection, deadline)
    bindings, verdicts = _fresh(connection, deadline)
    installed = profile()
    return installed, rows, bindings, verdicts


def _initialize(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    """Initialize only an explicit protected v2 copy, atomically inside its caller's transaction."""
    installed, rows, bindings, verdicts = _source(connection, deadline)
    roster = _roster(rows)
    lookup = {(row["table_name"], row["row_key"]): row for row in rows}
    for statement in SCHEMA.values():
        _execute(connection, statement, deadline=deadline)
    for row in rows:
        _execute(
            connection,
            "INSERT INTO history_integrity VALUES (?,?,?,?,?)",
            tuple(row[key] for key in ("table_name", "row_key", "row_digest", "byte_count", "cell_count")),
            deadline,
        )
    _execute(
        connection,
        "INSERT INTO history_profile VALUES (1,?,?,?,?,?)",
        (
            store.canonical(installed),
            roster,
            len(rows),
            sum(row["cell_count"] for row in rows),
            sum(row["byte_count"] for row in rows),
        ),
        deadline,
    )
    for identity, verdict in verdicts.items():
        _execute(
            connection,
            "INSERT INTO history_semantics VALUES (?,?,?,?,?)",
            (
                identity,
                installed["digest"],
                _dependencies(lookup, identity, verdict),
                store.canonical(verdict),
                planning.digest(verdict),
            ),
            deadline,
        )
    _execute(connection, f"PRAGMA user_version={VERSION}", deadline=deadline)
    return _facts(installed, rows, bindings, verdicts, len(verdicts))


def _validate(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    """Check the required baseline first, then freshly audit all semantics; never write repairs."""
    _connection(connection, deadline)
    schema = {**store.SCHEMA, **completion_store.SCHEMA, **SCHEMA}
    store._structure(connection, schema, VERSION, deadline=deadline)
    for table, fields in _INDEX_FIELDS.items():
        _cell_bounds(connection, table, fields, deadline)
    installed = profile()
    metadata_rows = _execute(
        connection,
        "SELECT singleton,profile_json,roster_digest,row_count,cell_count,byte_count FROM history_profile",
        deadline=deadline,
    ).fetchall()
    _require(
        len(metadata_rows) == 1 and metadata_rows[0][0] == 1, "history integrity metadata is missing or duplicated"
    )
    _, text, roster, count, cells, size = metadata_rows[0]
    _require(isinstance(text, str) and len(text.encode()) <= store.RECORD_LIMIT, "invalid retained history profile")
    try:
        retained = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        _require(isinstance(retained, dict) and store.canonical(retained) == text, "invalid retained history profile")
        declared = dict(retained)
        digest = declared.pop("digest")
        _require(digest == planning.digest(declared), "retained history profile digest is inconsistent")
    except (ValueError, KeyError, TypeError, RecursionError) as error:
        raise HistoryError("invalid retained history profile") from error
    rows = _rows(connection, deadline)
    baseline_rows = _execute(
        connection,
        "SELECT table_name,row_key,row_digest,byte_count,cell_count FROM history_integrity ORDER BY table_name,row_key",
        deadline=deadline,
    ).fetchall()
    expected = [
        tuple(row[key] for key in ("table_name", "row_key", "row_digest", "byte_count", "cell_count")) for row in rows
    ]
    _require(
        baseline_rows == expected, "history integrity baseline does not match exact retained row coverage or bytes"
    )
    _require(
        type(count) is int
        and type(cells) is int
        and type(size) is int
        and (roster, count, cells, size)
        == (_roster(rows), len(rows), sum(row["cell_count"] for row in rows), sum(row["byte_count"] for row in rows)),
        "history integrity roster commitment does not match retained history",
    )
    if retained != installed:
        raise UnsupportedProfileError(
            "unsupported history validator profile; use the matching trusted installation for audit"
        )
    # Baseline verification above MUST precede semantic interpretation: audit cannot reseal corruption.
    bindings, verdicts = _fresh(connection, deadline)
    lookup = {(row["table_name"], row["row_key"]): row for row in rows}
    semantic_rows = _execute(
        connection,
        "SELECT intent_id,profile_digest,dependencies_digest,verdict_json,verdict_digest "
        "FROM history_semantics ORDER BY intent_id",
        deadline=deadline,
    ).fetchall()
    for identity, selected_profile, dependencies, verdict_text, verdict_digest in semantic_rows:
        _require(identity in verdicts, "history semantic verdict has an unknown terminal identity")
        verdict = verdicts[identity]
        _require(
            selected_profile == installed["digest"]
            and dependencies == _dependencies(lookup, identity, verdict)
            and verdict_text == store.canonical(verdict)
            and verdict_digest == planning.digest(verdict),
            "history semantic verdict differs from fresh validation or its profile/dependency association",
        )
    return _facts(installed, rows, bindings, verdicts, len(semantic_rows))


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except (HistoryError, store.LockTimeoutError):
        raise
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from error
        raise HistoryError(f"history validation failed: {error}") from error
    except (store.StoreError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        raise HistoryError(f"history validation failed: {error}") from error


def initialize(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    """Initialize an explicit v2 copy in the caller's transaction; never commit or repair a source."""
    with _errors():
        return _initialize(connection, deadline=deadline)


def source_facts(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    """Fully validate compatible v2 input read-only; no required baseline exists yet."""
    with _errors():
        installed, rows, bindings, verdicts = _source(connection, deadline)
        return {
            "format_version": store.COMPLETION_VERSION,
            "profile": installed,
            "coverage": {
                "rows": len(rows),
                "cells": sum(row["cell_count"] for row in rows),
                "bytes": sum(row["byte_count"] for row in rows),
                "complete": True,
            },
            "semantics": {"fresh": len(verdicts), "reused": 0, "bindings": bindings},
            "integrity_baseline_checked": False,
        }


def validate(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    """Check the exact baseline before re-deriving every semantic result, without writing."""
    with _errors():
        return _validate(connection, deadline=deadline)


@contextmanager
def database(path: Path, *, deadline: store.Deadline | None = None) -> Iterator[sqlite3.Connection]:
    """Open an existing v3 copy read-only under the caller's directory lock and coherent snapshot."""
    budget = deadline if deadline is not None else store.Deadline()
    store.auxiliary_files(path)
    store.check_file_info(path.lstat())
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    connection: sqlite3.Connection | None = None
    try:
        original = store.file_info(fd)
        _require(store._header(fd) == VERSION, "history audit requires a version 3 audit-only ledger")
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=budget.remaining(), isolation_level=None
        )
        current = path.lstat()
        _require((current.st_dev, current.st_ino) == (original.st_dev, original.st_ino), "history ledger file changed")
        _execute(connection, "PRAGMA query_only=ON", deadline=budget)
        _execute(connection, "BEGIN", deadline=budget)
        yield connection
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        os.close(fd)
