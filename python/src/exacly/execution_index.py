"""Operational history indexing with mandatory byte checks and profile-bound terminal reuse.

Only the installed current profile can reuse a terminal result or write this format. A recognized
older profile receives fresh read-only validation until explicit copy-only activation refreshes it.
Global membership, phases, receipts and watermarks remain freshly checked on every operation.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from exacly import completion_store, executions, planning
from exacly import execution_history as history
from exacly import execution_store as store

Doc = dict[str, Any]
RowKey = tuple[str, str]
SqlRow = tuple[Any, ...]
VERSION = 4
FAMILY = "exacly-ledger-operational-v1"
CODEC = history.CODEC
SCHEMA = dict(history.SCHEMA)
HistoryError = history.HistoryError
UnsupportedProfileError = history.UnsupportedProfileError
_CAPABILITY = object()
_FACT_KEYS = {
    "schema_version",
    "intent_id",
    "binding_id",
    "pending_key",
    "request_digest",
    "phase",
    "state",
    "evidence",
    "receipt_digest",
    "valid",
    "before_snapshot",
    "after_snapshot",
}


@dataclass(frozen=True)
class Change:
    """Exact authorized SQL row values, constructed from the operation's actual input arguments."""

    before: SqlRow | None
    after: SqlRow


class Checked:
    """A private validated view bound to one protected connection and its prior exact baseline."""

    def __init__(
        self,
        capability: object,
        connection: sqlite3.Connection,
        *,
        rows: dict[RowKey, SqlRow | None],
        required: list[Doc],
        index_rows: dict[str, list[SqlRow]],
        snapshot: tuple[dict[str, Doc], dict[str, str], dict[str, Doc]],
        facts: Doc,
        terminal_facts: dict[str, Doc],
        installed: Doc,
        retained: Doc,
        deadline: store.Deadline | None = None,
    ) -> None:
        history._require(capability is _CAPABILITY, "indexed views must come from a protected validation")
        self._connection = connection
        self._rows = rows
        self._required = required
        self._index_rows = index_rows
        self._snapshot = snapshot
        self._facts = facts
        self._terminal_facts = terminal_facts
        self._installed = installed
        self._retained = retained
        self._deadline = deadline

    @property
    def rows(self) -> Mapping[RowKey, SqlRow | None]:
        """Only explicitly captured selected rows, never the historical proof collection."""
        return MappingProxyType(self._rows)

    def capture(self, keys: Iterable[RowKey]) -> Mapping[RowKey, SqlRow | None]:
        """Capture exact selected before-values while their saved required baseline still matches."""
        with history._errors():
            history._require(self._connection.in_transaction, "index row capture requires its protected transaction")
            lookup = {(row["table_name"], row["row_key"]): row for row in self._required}
            result: dict[RowKey, SqlRow | None] = {}
            for key in keys:
                history._require(
                    isinstance(key, tuple) and len(key) == 2 and key[0] in history._FIELDS,
                    "unknown source row capture identity",
                )
                value = _selected(self._connection, key, self._deadline)
                metadata = _row_metadata(key[0], value) if value is not None else None
                history._require(metadata == lookup.get(key), "selected source row changed before protected capture")
                if key in self._rows:
                    history._require(self._rows[key] == value, "selected before-values changed after protected capture")
                self._rows[key] = value
                result[key] = value
            return MappingProxyType(result)

    @property
    def snapshot(self) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
        return copy.deepcopy(self._snapshot)

    @property
    def records(self) -> dict[str, Doc]:
        return copy.deepcopy(self._snapshot[0])

    @property
    def active(self) -> dict[str, str]:
        return dict(self._snapshot[1])

    @property
    def watermarks(self) -> dict[str, Doc]:
        return copy.deepcopy(self._snapshot[2])

    @property
    def facts(self) -> Doc:
        return copy.deepcopy(self._facts)

    @property
    def current_profile(self) -> bool:
        return self._retained == self._installed


def profile() -> Doc:
    result = history.profile()
    result.pop("digest")
    result["family"] = FAMILY
    result["digest"] = planning.digest(result)
    return result


def _json(text: Any) -> Doc:
    history._require(isinstance(text, str) and len(text.encode()) <= store.RECORD_LIMIT, "invalid indexed JSON")
    value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
    history._require(isinstance(value, dict) and store.canonical(value) == text, "indexed JSON is not canonical")
    return dict(value)


def _retained_profile(text: Any) -> Doc:
    result = _json(text)
    history._require(
        set(result) == {"format_version", "family", "codec", "inputs", "runtime", "format_checkers", "digest"},
        "malformed retained validator profile",
    )
    payload = dict(result)
    digest = payload.pop("digest")
    history._require(
        isinstance(digest, str) and store.DIGEST.fullmatch(digest) and planning.digest(payload) == digest,
        "retained validator profile digest is inconsistent",
    )
    history._require(
        isinstance(result["format_version"], str)
        and bool(result["format_version"])
        and isinstance(result["family"], str)
        and isinstance(result["codec"], str),
        "malformed retained validator profile identity",
    )
    inputs = result["inputs"]
    history._require(isinstance(inputs, list) and bool(inputs), "malformed retained validator profile inputs")
    names: list[str] = []
    for item in inputs:
        history._require(
            isinstance(item, dict)
            and set(item) == {"name", "digest", "size"}
            and isinstance(item["name"], str)
            and bool(item["name"])
            and isinstance(item["digest"], str)
            and store.DIGEST.fullmatch(item["digest"])
            and type(item["size"]) is int
            and item["size"] > 0,
            "malformed retained validator profile artifact",
        )
        names.append(item["name"])
    history._require(names == sorted(set(names)), "retained validator profile artifacts are not unique and ordered")
    runtime = result["runtime"]
    history._require(
        isinstance(runtime, dict)
        and set(runtime) == {"python", "implementation", "sqlite", "sqlite_source"}
        and all(isinstance(value, str) and value for value in runtime.values()),
        "malformed retained validator runtime profile",
    )
    checkers = result["format_checkers"]
    history._require(isinstance(checkers, dict), "malformed retained validator checker profile")
    for name, checker in checkers.items():
        history._require(
            isinstance(name, str)
            and isinstance(checker, dict)
            and set(checker) == {"function", "digest", "raises"}
            and isinstance(checker["function"], str)
            and isinstance(checker["digest"], str)
            and store.DIGEST.fullmatch(checker["digest"])
            and isinstance(checker["raises"], list)
            and all(isinstance(item, str) for item in checker["raises"]),
            "malformed retained format checker profile",
        )
    return result


def _schema(connection: sqlite3.Connection, version: int, deadline: store.Deadline | None) -> None:
    history._connection(connection, deadline)
    store._structure(connection, {**store.SCHEMA, **completion_store.SCHEMA, **SCHEMA}, version, deadline=deadline)
    for table, fields in history._INDEX_FIELDS.items():
        history._cell_bounds(connection, table, fields, deadline)


def _selected(connection: sqlite3.Connection, key: RowKey, deadline: store.Deadline | None) -> SqlRow | None:
    table, identity = key
    history._require(table in history._FIELDS, "unknown selected source table")
    fields = history._FIELDS[table]
    expressions = [f"typeof({name}),length(CAST({name} AS BLOB))" for name, _, _, _ in fields]
    where = f" FROM {table} WHERE {history._KEYS[table]}=?"
    sizes = history._execute(connection, f"SELECT {','.join(expressions)}" + where, (identity,), deadline).fetchone()
    if sizes is None:
        return None
    for offset, (name, kind, limit, nullable) in enumerate(fields):
        actual, size = sizes[offset * 2 : offset * 2 + 2]
        history._require(
            (actual == "null" and nullable and size is None)
            or (actual == kind and type(size) is int and 0 <= size <= limit),
            f"selected history cell has invalid type or size: {table}.{name}",
        )
    names = ",".join(field[0] for field in fields)
    row = history._execute(connection, f"SELECT {names}" + where, (identity,), deadline).fetchone()
    return tuple(row) if row is not None else None


def _row_metadata(table: str, row: SqlRow) -> Doc:
    raw = [
        None if value is None else value if isinstance(value, bytes) else str(value).encode("utf-8") for value in row
    ]
    fields = history._FIELDS[table]
    names = [item[0] for item in fields]
    transcript = history._transcript(
        "source-row-v1",
        [("table", "text", table.encode())]
        + [(name, kind, value) for (name, kind, _, _), value in zip(fields, raw, strict=True)],
    )
    return {
        "table_name": table,
        "row_key": row[names.index(history._KEYS[table])],
        "row_digest": history._hash(transcript),
        "byte_count": sum(len(value) for value in raw if value is not None),
        "cell_count": len(fields),
    }


def _index_rows(connection: sqlite3.Connection, deadline: store.Deadline | None) -> dict[str, list[SqlRow]]:
    return {
        table: history._execute(connection, f"SELECT * FROM {table} ORDER BY 1,2", deadline=deadline).fetchall()
        for table in SCHEMA
    }


def _baseline(connection: sqlite3.Connection, version: int, deadline: store.Deadline | None) -> tuple[list[Doc], Doc]:
    _schema(connection, version, deadline)
    metadata = history._execute(
        connection,
        "SELECT singleton,profile_json,roster_digest,row_count,cell_count,byte_count FROM history_profile",
        deadline=deadline,
    ).fetchall()
    history._require(
        len(metadata) == 1 and metadata[0][0] == 1, "required index integrity metadata is missing or duplicated"
    )
    _, text, roster, count, cells, size = metadata[0]
    rows = history._rows(connection, deadline)
    expected = [
        tuple(row[key] for key in ("table_name", "row_key", "row_digest", "byte_count", "cell_count")) for row in rows
    ]
    actual = history._execute(
        connection,
        "SELECT table_name,row_key,row_digest,byte_count,cell_count FROM history_integrity ORDER BY table_name,row_key",
        deadline=deadline,
    ).fetchall()
    history._require(actual == expected, "required index baseline differs from exact retained bytes or row coverage")
    history._require(
        (roster, count, cells, size)
        == (
            history._roster(rows),
            len(rows),
            sum(row["cell_count"] for row in rows),
            sum(row["byte_count"] for row in rows),
        ),
        "required index roster commitment differs from retained history",
    )
    retained = _retained_profile(text)
    known_family = FAMILY if version == VERSION else history.FAMILY
    if retained["family"] != known_family or retained["codec"] != CODEC or retained["format_version"] != "0.1.0":
        raise UnsupportedProfileError(
            "unsupported indexed validator family or codec; no trusted compatibility rule exists"
        )
    return rows, retained


def _snapshot(value: Any) -> None:
    history._require(
        isinstance(value, dict)
        and set(value) == {"serial", "digest"}
        and type(value["serial"]) is int
        and value["serial"] >= 0
        and isinstance(value["digest"], str)
        and store.DIGEST.fullmatch(value["digest"]),
        "malformed terminal snapshot facts",
    )


def _fact(value: Doc) -> Doc:
    history._require(
        set(value) == _FACT_KEYS
        and value["schema_version"] == "0.1.0"
        and value["valid"] is True
        and value["phase"] in ("aborted", "committed"),
        "malformed current-profile terminal facts",
    )
    history._require(
        isinstance(value["intent_id"], str) and completion_store.ID.fullmatch(value["intent_id"]),
        "malformed terminal fact identity",
    )
    for key in ("binding_id", "pending_key", "request_digest"):
        history._require(
            isinstance(value[key], str) and store.DIGEST.fullmatch(value[key]),
            "malformed terminal fact dependency identity",
        )
    history._require(
        isinstance(value["state"], dict) and isinstance(value["evidence"], dict), "malformed terminal proof facts"
    )
    history._require(
        value["receipt_digest"] is None
        or (isinstance(value["receipt_digest"], str) and store.DIGEST.fullmatch(value["receipt_digest"])),
        "malformed terminal receipt facts",
    )
    _snapshot(value["before_snapshot"])
    _snapshot(value["after_snapshot"])
    return value


def _verdicts(
    connection: sqlite3.Connection, rows: list[Doc], retained: Doc, version: int, deadline: store.Deadline | None
) -> dict[str, Doc]:
    lookup = {(row["table_name"], row["row_key"]): row for row in rows}
    result: dict[str, Doc] = {}
    for identity, selected, dependencies, text, digest in history._execute(
        connection,
        "SELECT intent_id,profile_digest,dependencies_digest,verdict_json,verdict_digest FROM history_semantics",
        deadline=deadline,
    ):
        value = _json(text)
        if version == VERSION:
            _fact(value)
        history._require(
            value.get("intent_id") == identity and selected == retained["digest"] and digest == planning.digest(value),
            "terminal verdict profile, identity or digest association is inconsistent",
        )
        history._require(
            dependencies == history._dependencies(lookup, identity, value),
            "terminal verdict dependency association is inconsistent",
        )
        history._require(identity not in result, "duplicate terminal verdict identity")
        result[identity] = value
    return result


def _validated(
    connection: sqlite3.Connection,
    rows: list[Doc],
    retained: Doc,
    installed: Doc,
    *,
    fresh: bool,
    deadline: store.Deadline | None,
) -> tuple[tuple[dict[str, Doc], dict[str, str], dict[str, Doc]], dict[str, Doc], int, int]:
    records, active, watermarks = store._base(connection, deadline=deadline)
    for binding in records.values():
        executions.validate_record(binding)
    stored = _verdicts(connection, rows, retained, VERSION, deadline)
    matching = retained == installed
    terminals: dict[str, Doc] = {}
    fresh_count = reused_count = 0
    prepared = 0
    for identity, binding_id, phase in history._execute(
        connection, "SELECT id,binding_id,phase FROM completion_intents", deadline=deadline
    ):
        history._require(binding_id in records, "terminal binding is missing")
        if phase == "prepared":
            prepared += 1
            history._require(identity not in stored, "prepared intent cannot have a cached terminal verdict")
            continue
        history._require(phase in ("aborted", "committed"), "invalid indexed completion phase")
        if matching and not fresh and identity in stored:
            terminals[identity] = stored[identity]
            reused_count += 1
        else:
            envelope = completion_store._load(connection, identity, deadline)
            value = completion_store.semantic_facts(envelope, records[binding_id])
            _fact(value)
            if matching and identity in stored:
                history._require(value == stored[identity], "terminal verdict differs from fresh semantic validation")
            terminals[identity] = value
            fresh_count += 1
    history._require(set(stored) <= set(terminals), "terminal verdict references unknown or nonterminal history")
    completion_store._validate_indexed(connection, records, active, watermarks, terminals, deadline=deadline)
    return (records, active, watermarks), terminals, fresh_count, reused_count


def _facts(
    installed: Doc,
    retained: Doc,
    rows: list[Doc],
    snapshot: tuple[dict[str, Doc], dict[str, str], dict[str, Doc]],
    *,
    fresh: int,
    reused: int,
    prepared: int,
    semantic_rows: int,
) -> Doc:
    return {
        "format_version": VERSION,
        "profile": installed,
        "retained_profile": retained,
        "profile_status": "current" if retained == installed else "known_stale",
        "integrity": {
            "algorithm": "sha256",
            "roster_digest": history._roster(rows),
            "rows": len(rows),
            "cells": sum(row["cell_count"] for row in rows),
            "bytes": sum(row["byte_count"] for row in rows),
            "complete": True,
        },
        "semantics": {"bindings": len(snapshot[0]), "fresh": fresh, "reused": reused, "prepared": prepared},
        "index_rows": {"integrity": len(rows), "semantic": semantic_rows},
    }


def _checked(connection: sqlite3.Connection, *, fresh: bool, deadline: store.Deadline | None) -> Checked:
    rows, retained = _baseline(connection, VERSION, deadline)
    installed = profile()
    snapshot, terminals, fresh_count, reused_count = _validated(
        connection, rows, retained, installed, fresh=fresh, deadline=deadline
    )
    prepared = history._execute(
        connection, "SELECT count(*) FROM completion_intents WHERE phase='prepared'", deadline=deadline
    ).fetchone()[0]
    indexes = _index_rows(connection, deadline)
    facts = _facts(
        installed,
        retained,
        rows,
        snapshot,
        fresh=fresh_count,
        reused=reused_count,
        prepared=prepared,
        semantic_rows=len(indexes["history_semantics"]),
    )
    return Checked(
        _CAPABILITY,
        connection,
        rows={},
        required=rows,
        index_rows=indexes,
        snapshot=snapshot,
        facts=facts,
        terminal_facts=terminals,
        installed=installed,
        retained=retained,
        deadline=deadline,
    )


def check(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Checked:
    with history._errors():
        return _checked(connection, fresh=False, deadline=deadline)


def validate(
    connection: sqlite3.Connection, *, deadline: store.Deadline | None = None
) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
    return check(connection, deadline=deadline).snapshot


def audit(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    with history._errors():
        return _checked(connection, fresh=True, deadline=deadline).facts


def require_writable(checked: Checked) -> None:
    history._require(
        isinstance(checked, Checked) and checked.current_profile,
        "indexed ledger requires explicit copy-only activate refresh under the current installed profile before writes",
    )


def _write_baseline(
    connection: sqlite3.Connection, rows: list[Doc], installed: Doc, deadline: store.Deadline | None
) -> None:
    for row in rows:
        history._execute(
            connection,
            "INSERT INTO history_integrity VALUES (?,?,?,?,?)",
            tuple(row[key] for key in ("table_name", "row_key", "row_digest", "byte_count", "cell_count")),
            deadline,
        )
    history._execute(
        connection,
        "INSERT INTO history_profile VALUES (1,?,?,?,?,?)",
        (
            store.canonical(installed),
            history._roster(rows),
            len(rows),
            sum(row["cell_count"] for row in rows),
            sum(row["byte_count"] for row in rows),
        ),
        deadline,
    )


def _write_verdict(
    connection: sqlite3.Connection,
    identity: str,
    value: Doc,
    lookup: Mapping[RowKey, Doc],
    installed: Doc,
    deadline: store.Deadline | None,
) -> None:
    history._execute(
        connection,
        "INSERT INTO history_semantics VALUES (?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET "
        "profile_digest=excluded.profile_digest,dependencies_digest=excluded.dependencies_digest,"
        "verdict_json=excluded.verdict_json,verdict_digest=excluded.verdict_digest",
        _verdict_row(identity, value, lookup, installed),
        deadline,
    )


def _verdict_row(identity: str, value: Doc, lookup: Mapping[RowKey, Doc], installed: Doc) -> SqlRow:
    return (
        identity,
        installed["digest"],
        history._dependencies(lookup, identity, value),
        store.canonical(value),
        planning.digest(value),
    )


def _source(
    connection: sqlite3.Connection, deadline: store.Deadline | None
) -> tuple[list[Doc], tuple[dict[str, Doc], dict[str, str], dict[str, Doc]], dict[str, Doc], Doc, Doc]:
    history._connection(connection, deadline)
    version = history._execute(connection, "PRAGMA user_version", deadline=deadline).fetchone()[0]
    installed = profile()
    if version == store.COMPLETION_VERSION:
        store._structure(connection, {**store.SCHEMA, **completion_store.SCHEMA}, version, deadline=deadline)
        rows = history._rows(connection, deadline)
        retained = installed
        stored: dict[str, Doc] = {}
    else:
        history._require(version in (history.VERSION, VERSION), "activation requires an exact v2, v3 or v4 ledger")
        rows, retained = _baseline(connection, version, deadline)
        stored = _verdicts(connection, rows, retained, version, deadline)
    history._require(
        version == VERSION
        or history._execute(
            connection, "SELECT 1 FROM completion_intents WHERE phase='prepared' LIMIT 1", deadline=deadline
        ).fetchone()
        is None,
        "activation requires explicit recovery of unresolved prepared intents first",
    )
    snapshot = store._base(connection, deadline=deadline)
    for binding in snapshot[0].values():
        executions.validate_record(binding)
    terminals: dict[str, Doc] = {}
    for identity, binding_id in history._execute(
        connection, "SELECT id,binding_id FROM completion_intents WHERE phase!='prepared'", deadline=deadline
    ):
        terminals[identity] = completion_store.semantic_facts(
            completion_store._load(connection, identity, deadline), snapshot[0][binding_id]
        )
    history._require(set(stored) <= set(terminals), "terminal verdict references unknown or prepared history")
    if version == VERSION and retained == installed:
        for identity, value in stored.items():
            history._require(
                value == terminals[identity], "current-profile terminal verdict differs from fresh validation"
            )
    if version == history.VERSION and retained == history.profile():
        for identity, value in stored.items():
            expected = {
                key: field
                for key, field in terminals[identity].items()
                if key not in ("before_snapshot", "after_snapshot")
            }
            history._require(value == expected, "current-profile audit verdict differs from fresh validation")
    completion_store._validate_indexed(connection, *snapshot, terminals, deadline=deadline)
    return rows, snapshot, terminals, installed, retained


def source_facts(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    with history._errors():
        rows, snapshot, terminals, installed, retained = _source(connection, deadline)
        prepared = history._execute(
            connection, "SELECT count(*) FROM completion_intents WHERE phase='prepared'", deadline=deadline
        ).fetchone()[0]
        facts = _facts(
            installed, retained, rows, snapshot, fresh=len(terminals), reused=0, prepared=prepared, semantic_rows=0
        )
        version = history._execute(connection, "PRAGMA user_version", deadline=deadline).fetchone()[0]
        facts["format_version"] = version
        facts["integrity_baseline_checked"] = version in (history.VERSION, VERSION)
        return facts


def initialize(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    """Build/refresh only a caller-owned maintenance copy after fresh checks and existing baselines."""
    with history._errors():
        rows, snapshot, terminals, installed, _retained = _source(connection, deadline)
        version = history._execute(connection, "PRAGMA user_version", deadline=deadline).fetchone()[0]
        if version == store.COMPLETION_VERSION:
            for statement in SCHEMA.values():
                history._execute(connection, statement, deadline=deadline)
        else:
            for table in ("history_semantics", "history_integrity", "history_profile"):
                history._execute(connection, f"DELETE FROM {table}", deadline=deadline)
        _write_baseline(connection, rows, installed, deadline)
        lookup = {(row["table_name"], row["row_key"]): row for row in rows}
        for identity, value in terminals.items():
            _write_verdict(connection, identity, value, lookup, installed, deadline)
        history._execute(connection, f"PRAGMA user_version={VERSION}", deadline=deadline)
        return _facts(
            installed,
            installed,
            rows,
            snapshot,
            fresh=len(terminals),
            reused=0,
            prepared=history._execute(
                connection, "SELECT count(*) FROM completion_intents WHERE phase='prepared'", deadline=deadline
            ).fetchone()[0],
            semantic_rows=len(terminals),
        )


def refresh(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
    return initialize(connection, deadline=deadline)


def _allowed(key: RowKey, change: Change) -> None:
    table, identity = key
    before, after = change.before, change.after
    history._require(
        table in history._FIELDS and isinstance(after, tuple) and len(after) == len(history._FIELDS[table]),
        "invalid authorized index row change",
    )
    columns = [field[0] for field in history._FIELDS[table]]
    history._require(
        after[columns.index(history._KEYS[table])] == identity, "authorized row identity differs from its key"
    )
    if table in ("bindings", "completion_receipts"):
        history._require(before is None, "immutable historical row cannot be changed")
    elif table == "completion_intents":
        if before is None:
            history._require(after[4] == "prepared", "new completion intent must begin prepared")
        else:
            history._require(
                before[4] == "prepared"
                and after[4] in ("aborted", "committed")
                and before[:4] + before[5:] == after[:4] + after[5:],
                "only a prepared intent phase may change",
            )
    elif table == "watermarks" and before is not None:
        history._require(
            after[1] >= before[1] and (after[1] != before[1] or after[2] == before[2]),
            "authorized state watermark would roll back or change its registered digest",
        )


def maintain(
    connection: sqlite3.Connection,
    checked: Checked,
    *,
    changes: Mapping[RowKey, Change],
    deadline: store.Deadline | None = None,
) -> tuple[int, Checked]:
    """Maintain only exact authorized rows after fresh semantics, in their caller's same transaction."""
    with history._errors():
        history._require(
            isinstance(checked, Checked) and checked._connection is connection and connection.in_transaction,
            "index mutation requires its protected same-connection checked transaction",
        )
        require_writable(checked)
        history._require(profile() == checked._installed, "installed validator profile changed during index mutation")
        _schema(connection, VERSION, deadline)
        history._require(
            _index_rows(connection, deadline) == checked._index_rows,
            "index baseline or verdict changed outside the owned mutation",
        )
        required = history._rows(connection, deadline)
        old_lookup = {(row["table_name"], row["row_key"]): row for row in checked._required}
        lookup = {(row["table_name"], row["row_key"]): row for row in required}
        history._require(set(old_lookup) <= set(lookup), "historical source rows cannot be deleted")
        actual_changes = {key for key in lookup if old_lookup.get(key) != lookup[key]}
        declared_changes: set[RowKey] = set()
        for key, change in changes.items():
            history._require(isinstance(change, Change), "index mutations require exact authorized row values")
            _allowed(key, change)
            history._require(
                key in checked._rows
                and checked._rows[key] == change.before
                and _selected(connection, key, deadline) == change.after,
                "actual source row differs from exact authorized before/after values",
            )
            if change.before != change.after:
                declared_changes.add(key)
        history._require(
            actual_changes == declared_changes, "source rows changed outside the exact authorized index mutation"
        )
        snapshot = store._base(connection, deadline=deadline)
        for binding in snapshot[0].values():
            executions.validate_record(binding)
        terminals: dict[str, Doc] = {}
        affected: set[str] = set()
        fresh_count = reused_count = prepared_count = 0
        for identity, binding_id, phase in history._execute(
            connection, "SELECT id,binding_id,phase FROM completion_intents", deadline=deadline
        ):
            if phase == "prepared":
                prepared_count += 1
                continue
            old = checked._terminal_facts.get(identity)
            if old is not None and history._dependencies(old_lookup, identity, old) == history._dependencies(
                lookup, identity, old
            ):
                terminals[identity] = old
                reused_count += 1
            else:
                terminals[identity] = completion_store.semantic_facts(
                    completion_store._load(connection, identity, deadline), snapshot[0][binding_id]
                )
                affected.add(identity)
                fresh_count += 1
        completion_store._validate_indexed(connection, *snapshot, terminals, deadline=deadline)
        changed_required = [row for row in required if old_lookup.get((row["table_name"], row["row_key"])) != row]
        planned_verdicts = {row[0]: row for row in checked._index_rows["history_semantics"]}
        for identity in affected:
            planned_verdicts[identity] = _verdict_row(identity, terminals[identity], lookup, checked._installed)
        expected_indexes = {
            "history_integrity": [
                tuple(row[field] for field in ("table_name", "row_key", "row_digest", "byte_count", "cell_count"))
                for row in required
            ],
            "history_profile": [
                (
                    1,
                    store.canonical(checked._retained),
                    history._roster(required),
                    len(required),
                    sum(row["cell_count"] for row in required),
                    sum(row["byte_count"] for row in required),
                )
            ],
            "history_semantics": sorted(planned_verdicts.values(), key=lambda row: (row[0], row[1])),
        }
        planned_writes = len(changed_required) + 1 + len(affected)
        before_writes = connection.total_changes
        for row in changed_required:
            history._execute(
                connection,
                "INSERT INTO history_integrity VALUES (?,?,?,?,?) ON CONFLICT(table_name,row_key) DO UPDATE SET "
                "row_digest=excluded.row_digest,byte_count=excluded.byte_count,cell_count=excluded.cell_count",
                tuple(row[field] for field in ("table_name", "row_key", "row_digest", "byte_count", "cell_count")),
                deadline,
            )
        history._execute(
            connection,
            "UPDATE history_profile SET roster_digest=?,row_count=?,cell_count=?,byte_count=? WHERE singleton=1",
            (
                history._roster(required),
                len(required),
                sum(row["cell_count"] for row in required),
                sum(row["byte_count"] for row in required),
            ),
            deadline,
        )
        for identity in affected:
            _write_verdict(connection, identity, terminals[identity], lookup, checked._installed, deadline)
        history._require(
            connection.total_changes - before_writes == planned_writes,
            "index mutation performed an unexpected number of writes",
        )
        history._require(
            _index_rows(connection, deadline) == expected_indexes,
            "stored index rows differ from the exact planned mutation",
        )
        # Validate the durable candidate before its caller commits. A changed source row after
        # the initial validation, or an internally self-consistent substituted verdict, cannot
        # be absorbed as an index write or become the next trusted baseline.
        final = _checked(connection, fresh=False, deadline=deadline)
        history._require(
            final._required == required and final._snapshot == snapshot and final._terminal_facts == terminals,
            "indexed source or semantic facts changed during index writes",
        )
        history._require(
            final._installed == checked._installed and final.current_profile and final._index_rows == expected_indexes,
            "installed profile or exact index changed before commit",
        )
        history._require(
            connection.total_changes - before_writes == planned_writes,
            "index mutation performed an unexpected write during final verification",
        )
        facts = _facts(
            checked._installed,
            checked._retained,
            required,
            snapshot,
            fresh=fresh_count,
            reused=reused_count,
            prepared=prepared_count,
            semantic_rows=len(expected_indexes["history_semantics"]),
        )
        updated = Checked(
            _CAPABILITY,
            connection,
            rows={},
            required=required,
            index_rows=expected_indexes,
            snapshot=snapshot,
            facts=facts,
            terminal_facts=terminals,
            installed=checked._installed,
            retained=checked._retained,
            deadline=deadline,
        )
        return planned_writes, updated
