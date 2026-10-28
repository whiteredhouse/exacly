"""Explicit copy-only initialization and fresh audit of the audit-only history format.

The authoritative v2 source is preserved. A v3 copy is a maintenance artifact and cannot be selected
by operational execution or completion commands. Neither conversion nor audit repairs history.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from exacly import execution_history as history
from exacly import execution_maintenance as copying
from exacly import execution_store as store
from exacly import planning

Doc = dict[str, Any]
VERSION = "0.1.0"
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
SCHEMA: Doc = {
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


def _report(path: Path, operation: str, *, dry_run: bool = False) -> Doc:
    return {
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


def _finish(report: Doc) -> Doc:
    report["digest"] = planning.digest(report)
    return report


def _full_audit(path: Path, budget: store.Deadline) -> Doc:
    with history.database(path, deadline=budget) as connection:
        return history.validate(connection, deadline=budget)


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


def _supported_audit(found: dict[str, tuple[int, Doc]], report: Doc) -> bool:
    observed = report["observed_store"]
    store.require(observed["sqlite_header"], "execution store is not a recognized SQLite database")
    store.require(observed["journal_mode"] == "delete", "history audit requires DELETE journaling")
    store.require(observed["application_id"] == store.APPLICATION_ID, "unrecognized execution store application")
    store.require(observed["user_version"] == history.VERSION, "history audit requires a version 3 ledger")
    store.require(not any(name.endswith(("-wal", "-shm")) for name in found), "WAL and SHM residue are unsupported")
    journal = found.get("ledger.sqlite3-journal")
    if journal is not None:
        copying._journal(journal[0], found["ledger.sqlite3"][0])
    return journal is not None


def audit(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Check the required baseline and every retained semantic proof in a read-only v3 view."""
    budget = store.Deadline(lock_timeout)
    try:
        with copying._locks(path, None, budget) as source, ExitStack() as stack:
            report = _report(source, "audit")
            found = copying._capture(stack, source, report)
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
                        _validated(report, _full_audit(source, budget))
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
    lines += report["diagnostics"]
    lines.append("Source preserved. Version 3 is audit-only and cannot be used by operational commands.")
    return "\n".join(lines)
