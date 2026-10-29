"""Explicit copy-only history initialization, operational activation and fresh audits.

The authoritative v2 source is preserved. A v3 copy is a maintenance artifact and cannot be selected
by operational execution or completion commands. Activation produces an explicitly selected v4 copy;
no command repairs changed retained bytes or automatically switches the canonical ledger.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from exacly import execution_history as history
from exacly import execution_index as index
from exacly import execution_maintenance as copying
from exacly import execution_store as store
from exacly import planning

Doc = dict[str, Any]
VERSION = "0.1.0"
OPERATIONAL_VERSION = "0.2.0"
CLAIMS = {
    "source_modified": False,
    "eligible_operational": False,
    "automatic_cutover": False,
    "task_completion_authorized": False,
    "self_authenticating": False,
    "latest_state_verified": False,
    "semantic_results_reused": False,
}
COUNTS = planning.fields(
    {
        "bindings": {"type": "integer", "minimum": 0},
        "fresh": {"type": "integer", "minimum": 0},
        "reused": {"const": 0},
    }
)
LEGACY_SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Exacly ledger history maintenance 0.1.0",
    **planning.fields(
        {
            "schema_version": {"const": VERSION},
            "operation": {"enum": ["index", "audit"]},
            "status": {"enum": ["ready", "indexed", "passed", "corrupt", "unsupported", "recovery_required"]},
            "ledger": {"type": "string"},
            "as_of": {"type": "string", "format": "date-time"},
            "dry_run": {"type": "boolean"},
            "format_version": {"const": history.VERSION},
            "profile": {"type": "object", "required": ["digest"], "properties": {"digest": planning.DIGEST}},
            "observed_store": {"type": ["object", "null"]},
            "source": {"type": "array", "items": {"type": "object"}},
            "output": {"type": ["string", "null"]},
            "files": {"type": "array", "items": {"type": "object"}},
            "validation": planning.fields(
                {
                    "status": {"enum": ["not_performed", "passed", "failed"]},
                    "scope": {"const": "full_history"},
                    "all_retained_bytes_checked": {"type": "boolean"},
                    "integrity_baseline_checked": {"type": "boolean"},
                    "semantics": COUNTS,
                    "facts": {"type": ["object", "null"]},
                }
            ),
            "diagnostics": {"type": "array", "items": {"type": "string"}},
            "claims": planning.fields({key: {"const": value} for key, value in CLAIMS.items()}),
            "digest": planning.DIGEST,
        }
    ),
}
OPERATIONAL_SCHEMA: Doc = copy.deepcopy(LEGACY_SCHEMA)
OPERATIONAL_SCHEMA["title"] = "Exacly operational ledger history maintenance 0.2.0"
OPERATIONAL_SCHEMA["properties"]["schema_version"] = {"const": OPERATIONAL_VERSION}
OPERATIONAL_SCHEMA["properties"]["operation"] = {"enum": ["activate", "audit"]}
OPERATIONAL_SCHEMA["properties"]["status"] = {
    "enum": ["ready", "activated", "passed", "corrupt", "unsupported", "recovery_required"]
}
OPERATIONAL_SCHEMA["properties"]["format_version"] = {"const": index.VERSION}
OPERATIONAL_SCHEMA["properties"]["claims"]["properties"]["eligible_operational"] = {"const": True}
OPERATIONAL_SCHEMA["properties"]["validation"]["properties"]["semantics"] = planning.fields(
    {
        "bindings": {"type": "integer", "minimum": 0},
        "fresh": {"type": "integer", "minimum": 0},
        "reused": {"const": 0},
        "prepared": {"type": "integer", "minimum": 0},
    }
)
SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [LEGACY_SCHEMA, OPERATIONAL_SCHEMA],
}


def _operational(report: Doc) -> None:
    report.update(schema_version=OPERATIONAL_VERSION, format_version=index.VERSION, profile=index.profile())
    report["claims"] = {**CLAIMS, "eligible_operational": True}
    report["validation"]["semantics"].setdefault("prepared", 0)


def _report(path: Path, operation: str, *, dry_run: bool = False) -> Doc:
    report: Doc = {
        "schema_version": VERSION,
        "operation": operation,
        "status": "unsupported",
        "ledger": str(path),
        "as_of": dt.datetime.now(dt.UTC).isoformat(),
        "dry_run": dry_run,
        "format_version": history.VERSION,
        "profile": history.profile(),
        "observed_store": None,
        "source": [],
        "output": None,
        "files": [],
        "validation": {
            "status": "not_performed",
            "scope": "full_history",
            "all_retained_bytes_checked": False,
            "integrity_baseline_checked": False,
            "semantics": {"bindings": 0, "fresh": 0, "reused": 0},
            "facts": None,
        },
        "diagnostics": [],
        "claims": dict(CLAIMS),
    }
    if operation == "activate":
        _operational(report)
    return report


def _finish(report: Doc) -> Doc:
    report["digest"] = planning.digest(report)
    return report


def _full_audit(path: Path, budget: store.Deadline, *, version: int = history.VERSION) -> Doc:
    if version == history.VERSION:
        with history.database(path, deadline=budget) as connection:
            return history.validate(connection, deadline=budget)
    with store.database(path, create=False, deadline=budget) as (connection, _):
        store.busy_timeout(connection, budget)
        connection.execute("BEGIN")
        return index.audit(connection, deadline=budget)


def _validated(report: Doc, facts: Doc) -> None:
    report["validation"].update(
        status="passed",
        all_retained_bytes_checked=True,
        integrity_baseline_checked=True,
        semantics=facts["semantics"],
        facts=facts,
    )


def _source_validation(path: Path, budget: store.Deadline, report: Doc) -> None:
    with store.database(path, create=False, deadline=budget) as (connection, _):
        store.busy_timeout(connection, budget)
        connection.execute("BEGIN")
        facts = history.source_facts(connection, deadline=budget)
    report["validation"].update(
        status="passed",
        all_retained_bytes_checked=True,
        semantics=facts["semantics"],
        facts=facts,
    )


def convert(
    path: Path,
    out: Path,
    *,
    dry_run: bool = False,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Validate v2, then publish a separate fully audited v3 artifact without an operational cutover."""
    budget = store.Deadline(lock_timeout)
    try:
        with copying._locks(path, out, budget) as source, ExitStack() as stack:
            report = _report(source, "index", dry_run=dry_run)
            report["output"] = str(out.absolute())
            found = copying._capture(stack, source, report)
            has_journal = copying._supported(found, {**report, "store": {}})
            store.require(not has_journal, "history indexing requires a clean ledger; use explicit copy recovery first")
            store.require(
                report["observed_store"]["user_version"] == store.COMPLETION_VERSION,
                "history indexing requires a version 2 ledger",
            )
            _source_validation(source, budget, report)
            copying._stable(found, source)
            if dry_run:
                report["status"] = "ready"
                report["diagnostics"].append("Full source validation passed; no files were written.")
                return _finish(report)
            with tempfile.TemporaryDirectory(prefix=".execution-history-", dir=out.parent) as temporary:
                folder = Path(temporary)
                raw, indexed = folder / "raw", folder / "indexed"
                raw.mkdir(mode=0o700)
                indexed.mkdir(mode=0o700)
                source_fd, captured = found["ledger.sqlite3"]
                copying._copy(source_fd, raw / "ledger.sqlite3")
                store.require(
                    copying._file(raw / "ledger.sqlite3", folder)["digest"] == captured["digest"],
                    "raw ledger backup does not match the captured source",
                )
                result = indexed / "ledger.sqlite3"
                copying._copy(source_fd, result)
                with store.database(result, create=True, deadline=budget) as (connection, _):
                    store.configure(connection, deadline=budget)
                    connection.execute("PRAGMA synchronous=EXTRA")
                    store.busy_timeout(connection, budget)
                    connection.execute("BEGIN IMMEDIATE")
                    history.initialize(connection, deadline=budget)
                    connection.commit()
                store.auxiliary_files(result)
                _validated(report, _full_audit(result, budget))
                report["status"] = "indexed"
                report["files"] = [copying._file(item, folder) for item in sorted(folder.rglob("*")) if item.is_file()]
                copying._stable(found, source)
                _finish(report)
                copying._write(folder / "manifest.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
                copying._sync(folder)
                copying._stable(found, source)
                copying._publish(folder, out)
            return report
    except store.StoreError:
        raise
    except (OSError, sqlite3.Error) as error:
        if isinstance(error, sqlite3.Error) and store.sqlite_lock_error(error):
            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from None
        raise store.StoreError("ledger history indexing failed; source files were preserved") from error


@contextmanager
def _activation_source(path: Path, version: int, budget: store.Deadline) -> Iterator[sqlite3.Connection]:
    if version == history.VERSION:
        with history.database(path, deadline=budget) as connection:
            yield connection
    else:
        with store.database(path, create=False, deadline=budget) as (connection, _):
            store.busy_timeout(connection, budget)
            connection.execute("BEGIN")
            yield connection


@contextmanager
def _activation_copy(path: Path, budget: store.Deadline) -> Iterator[sqlite3.Connection]:
    """Open only a private, newly copied v2/v3/v4 file for the explicit conversion transaction.

    Ordinary store opens deliberately reject v3. This narrow maintenance opener does not change
    that boundary: its caller has just copied and validated the protected source in a private folder.
    """
    store.auxiliary_files(path)
    store.check_file_info(path.lstat())
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    connection: sqlite3.Connection | None = None
    try:
        original = store.file_info(fd)
        store.require(
            store._header(fd) in (store.COMPLETION_VERSION, history.VERSION, index.VERSION),
            "activation requires a version 2, 3 or 4 ledger",
        )
        connection = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, timeout=budget.remaining(), isolation_level=None
        )
        current = path.lstat()
        store.require(
            (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino),
            "activation copy changed while opening",
        )
        store.configure(connection, deadline=budget)
        connection.execute("PRAGMA synchronous=EXTRA")
        store.busy_timeout(connection, budget)
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        os.close(fd)


def _supported_activation(found: dict[str, tuple[int, Doc]], report: Doc) -> int:
    observed = report["observed_store"]
    store.require(observed["sqlite_header"], "execution store is not a recognized SQLite database")
    store.require(observed["journal_mode"] == "delete", "activation requires DELETE journaling")
    store.require(observed["application_id"] == store.APPLICATION_ID, "unrecognized execution store application")
    version = observed["user_version"]
    store.require(
        version in (store.COMPLETION_VERSION, history.VERSION, index.VERSION),
        "activation requires a version 2, 3 or 4 ledger",
    )
    store.require(
        set(found) == {"ledger.sqlite3"},
        "activation requires a clean ledger without SQLite residue; use explicit copy recovery first",
    )
    return int(version)


def activate(
    path: Path,
    out: Path,
    *,
    dry_run: bool = False,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Publish a fully validated operational v4 copy, including an explicit stale-profile refresh.

    The exact source and existing history are preserved. The operator must select one canonical
    ledger path explicitly; activation never switches another command's ledger or state path.
    """
    budget = store.Deadline(lock_timeout)
    destination = out.absolute()
    try:
        with copying._locks(path, destination, budget) as source, ExitStack() as stack:
            report = _report(source, "activate", dry_run=dry_run)
            report["output"] = str(destination)
            found = copying._capture(stack, source, report)
            version = _supported_activation(found, report)
            with _activation_source(source, version, budget) as connection:
                facts = index.source_facts(connection, deadline=budget)
            report["validation"].update(
                status="passed",
                all_retained_bytes_checked=True,
                integrity_baseline_checked=version in (history.VERSION, index.VERSION),
                semantics=facts["semantics"],
                facts=facts,
            )
            copying._stable(found, source)
            if dry_run:
                report["status"] = "ready"
                report["diagnostics"].append("Full source validation passed; no files were written or selected.")
                return _finish(report)
            with tempfile.TemporaryDirectory(prefix=".execution-activation-", dir=destination.parent) as temporary:
                folder = Path(temporary)
                raw, operational = folder / "raw", folder / "operational"
                raw.mkdir(mode=0o700)
                operational.mkdir(mode=0o700)
                source_fd, captured = found["ledger.sqlite3"]
                copying._copy(source_fd, raw / "ledger.sqlite3")
                store.require(
                    copying._file(raw / "ledger.sqlite3", folder)["digest"] == captured["digest"],
                    "raw ledger backup does not match the captured source",
                )
                result = operational / "ledger.sqlite3"
                copying._copy(source_fd, result)
                with _activation_copy(result, budget) as connection:
                    index.initialize(connection, deadline=budget)
                    connection.commit()
                store.auxiliary_files(result)
                _validated(report, _full_audit(result, budget, version=index.VERSION))
                report["status"] = "activated"
                report["files"] = [copying._file(item, folder) for item in sorted(folder.rglob("*")) if item.is_file()]
                report["diagnostics"].append(
                    "Operational format validated. Explicitly select one canonical ledger path before using the copy."
                )
                copying._stable(found, source)
                _finish(report)
                copying._write(folder / "manifest.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
                copying._sync(folder)
                copying._stable(found, source)
                copying._publish(folder, destination)
            return report
    except store.StoreError:
        raise
    except (OSError, sqlite3.Error) as error:
        if isinstance(error, sqlite3.Error) and store.sqlite_lock_error(error):
            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from None
        raise store.StoreError("ledger activation failed; source files were preserved") from error


def _supported_audit(found: dict[str, tuple[int, Doc]], report: Doc) -> bool:
    observed = report["observed_store"]
    store.require(observed["sqlite_header"], "execution store is not a recognized SQLite database")
    store.require(observed["journal_mode"] == "delete", "history audit requires DELETE journaling")
    store.require(observed["application_id"] == store.APPLICATION_ID, "unrecognized execution store application")
    store.require(
        observed["user_version"] in (history.VERSION, index.VERSION),
        "history audit requires a version 3 or 4 ledger",
    )
    store.require(not any(name.endswith(("-wal", "-shm")) for name in found), "WAL and SHM residue are unsupported")
    journal = found.get("ledger.sqlite3-journal")
    if journal is not None:
        copying._journal(journal[0], found["ledger.sqlite3"][0])
    return journal is not None


def audit(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Check the required baseline and every retained proof freshly in a read-only v3/v4 view."""
    budget = store.Deadline(lock_timeout)
    try:
        with copying._locks(path, None, budget) as source, ExitStack() as stack:
            report = _report(source, "audit")
            found = copying._capture(stack, source, report)
            if report["observed_store"]["user_version"] == index.VERSION:
                _operational(report)
            try:
                has_journal = _supported_audit(found, report)
            except store.StoreError as error:
                report["diagnostics"].append(str(error))
            else:
                if has_journal:
                    report["status"] = "recovery_required"
                    report["diagnostics"].append("Recognized rollback journal; audit will not recover or change it.")
                else:
                    try:
                        _validated(report, _full_audit(source, budget, version=report["format_version"]))
                        report["status"] = "passed"
                    except store.LockTimeoutError:
                        raise
                    except history.UnsupportedProfileError as error:
                        report["diagnostics"].append(str(error))
                    except (store.StoreError, sqlite3.Error) as error:
                        if isinstance(error, sqlite3.Error) and store.sqlite_lock_error(error):
                            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from None
                        report["status"] = "corrupt"
                        report["validation"]["status"] = "failed"
                        report["diagnostics"].append(str(error))
            copying._stable(found, source)
            return _finish(report)
    except store.StoreError:
        raise
    except OSError as error:
        raise store.StoreError("ledger history audit failed; source files were preserved") from error


def as_text(report: Doc) -> str:
    lines = [
        f"Ledger history {report['operation']}: {report['status']}",
        f"Source: {planning.canonical(report['ledger'])}",
    ]
    if report["output"] is not None:
        lines.append(f"Artifact: {planning.canonical(report['output'])}")
    lines.append(f"Installed profile: {planning.canonical(report['profile']['digest'])}")
    validation = report["validation"]
    semantics = validation["semantics"]
    lines.append(
        f"Full-history validation: {validation['status']}; fresh terminal proofs: {semantics['fresh']}; "
        f"fresh bindings: {semantics['bindings']}; reused: {semantics['reused']}."
    )
    facts = validation["facts"]
    if report["format_version"] == index.VERSION and isinstance(facts, dict):
        lines.append(
            f"Retained profile: {planning.canonical(facts['retained_profile']['digest'])}; "
            f"compatibility: {facts['profile_status']}; fresh prepared proofs: {semantics['prepared']}."
        )
    lines += report["diagnostics"]
    if report["format_version"] == history.VERSION:
        lines.append("Source preserved. Version 3 is audit-only and cannot be used by operational commands.")
    else:
        lines.append(
            "Source preserved. Version 4 supports operational commands; explicitly select one canonical ledger path. "
            "No automatic cutover or current state verification is performed."
        )
    return "\n".join(lines)
