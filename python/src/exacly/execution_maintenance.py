"""Copy-only maintenance for an operator-controlled local execution ledger.

Source files are never opened by SQLite when residue exists. Explicit recovery operates on a working
copy and retains the original bytes. Manifests describe snapshots, not authentication or a cutover.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from exacly import completion_store, executions, operation_locks, planning
from exacly import execution_store as store

Doc = dict[str, Any]
VERSION = "0.2.0"
V4_VERSION = "0.3.0"
INDEX_VERSION = 4
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
CHUNK = 64 * 1024
CLAIMS = {
    "source_modified": False,
    "task_completion_authorized": False,
    "self_authenticating": False,
    "latest_state_verified": False,
    "automatic_cutover": False,
}
LEGACY_SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Exacly ledger maintenance snapshot 0.2.0",
    **planning.fields(
        {
            "schema_version": {"const": VERSION},
            "operation": {"enum": ["inspect", "backup", "recover", "upgrade"]},
            "status": {
                "enum": ["clean", "recovery_required", "unsupported", "corrupt", "backed_up", "recovered", "upgraded"]
            },
            "ledger": {"type": "string"},
            "as_of": {"type": "string", "format": "date-time"},
            "store": planning.fields(
                {
                    "application_id": {"const": store.APPLICATION_ID},
                    "user_version": {"enum": [store.USER_VERSION, store.COMPLETION_VERSION]},
                    "journal_mode": {"const": "delete"},
                }
            ),
            "observed_store": {"type": ["object", "null"]},
            "source": {"type": "array", "items": {"type": "object"}},
            "output": {"type": ["string", "null"]},
            "files": {"type": "array", "items": {"type": "object"}},
            "validation": planning.fields(
                {
                    "status": {"enum": ["not_performed", "passed", "failed"]},
                    "bindings": {"type": "integer", "minimum": 0},
                    "active": {"type": "integer", "minimum": 0},
                }
            ),
            "diagnostics": {"type": "array", "items": {"type": "string"}},
            "claims": planning.fields({key: {"const": value} for key, value in CLAIMS.items()}),
            "digest": planning.DIGEST,
        }
    ),
}
V4_SCHEMA: Doc = copy.deepcopy(LEGACY_SCHEMA)
V4_SCHEMA["title"] = "Exacly operational indexed ledger maintenance snapshot 0.3.0"
V4_SCHEMA["properties"]["schema_version"] = {"const": V4_VERSION}
V4_SCHEMA["properties"]["store"]["properties"]["user_version"] = {"const": INDEX_VERSION}
V4_SCHEMA["properties"]["validation"] = planning.fields(
    {
        **LEGACY_SCHEMA["properties"]["validation"]["properties"],
        "scope": {"const": "full_history"},
        "all_retained_bytes_checked": {"type": "boolean"},
        "integrity_baseline_checked": {"type": "boolean"},
        "profile_status": {"enum": ["not_checked", "current", "known_stale", "unsupported"]},
        "semantics": planning.fields(
            {
                "bindings": {"type": "integer", "minimum": 0},
                "fresh": {"type": "integer", "minimum": 0},
                "reused": {"const": 0},
                "prepared": {"type": "integer", "minimum": 0},
            }
        ),
        "facts": {"type": ["object", "null"]},
    }
)
SCHEMA_V4 = V4_SCHEMA
SCHEMA: Doc = {"$schema": LEGACY_SCHEMA["$schema"], "oneOf": [LEGACY_SCHEMA, V4_SCHEMA]}


def _v4(report: Doc) -> None:
    if report["schema_version"] != V4_VERSION:
        report["schema_version"] = V4_VERSION
        report["validation"] = {
            "status": "not_performed",
            "bindings": 0,
            "active": 0,
            "scope": "full_history",
            "all_retained_bytes_checked": False,
            "integrity_baseline_checked": False,
            "profile_status": "not_checked",
            "semantics": {"bindings": 0, "fresh": 0, "reused": 0, "prepared": 0},
            "facts": None,
        }
    report["store"]["user_version"] = INDEX_VERSION


def _recognized_v4(report: Doc) -> None:
    observed = report["observed_store"]
    if (
        observed["sqlite_header"]
        and observed["application_id"] == store.APPLICATION_ID
        and observed["user_version"] == INDEX_VERSION
    ):
        _v4(report)


def _digest(fd: int) -> str:
    hashed, offset = hashlib.sha256(), 0
    while content := os.pread(fd, CHUNK, offset):
        hashed.update(content)
        offset += len(content)
    return "sha256:" + hashed.hexdigest()


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _source(stack: ExitStack, path: Path, name: str) -> tuple[int, Doc]:
    store.check_file_info(path.lstat())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stack.callback(os.close, fd)
    info = store.file_info(fd)
    store.require(_identity(info) == _identity(path.lstat()), "ledger source changed while opening")
    result = {
        "name": name,
        "path": str(path),
        "digest": _digest(fd),
        "size": info.st_size,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "mode": 0o600,
    }
    return fd, result


def _unchanged(path: Path, fd: int, snapshot: Doc) -> None:
    info = store.file_info(fd)
    expected = tuple(snapshot[key] for key in ("device", "inode", "size", "mtime_ns", "ctime_ns"))
    store.require(_identity(info) == expected == _identity(path.lstat()), "ledger source changed during maintenance")
    store.require(_digest(fd) == snapshot["digest"], "ledger source bytes changed during maintenance")


def _journal(fd: int, database_fd: int) -> None:
    header = os.pread(fd, 28, 0)
    store.require(len(header) == 28 and header[:8] == JOURNAL_MAGIC, "unrecognized rollback journal header")
    sector, page = int.from_bytes(header[20:24], "big"), int.from_bytes(header[24:28], "big")
    db_page = int.from_bytes(os.pread(database_fd, 2, 16), "big")
    if db_page == 1:
        db_page = 65536
    store.require(
        512 <= sector <= 65536
        and not sector & (sector - 1)
        and 512 <= page <= 65536
        and not page & (page - 1)
        and page == db_page,
        "unsupported rollback journal page or sector size",
    )
    size = os.fstat(fd).st_size
    store.require(size >= sector + page + 8, "rollback journal is incomplete")
    store.require(os.pread(fd, 8, size - 8) != JOURNAL_MAGIC, "super-journal recovery is unsupported")


@contextmanager
def _locks(path: Path, out: Path | None, budget: store.Deadline) -> Iterator[Path]:
    source = store.location(path)
    selected = {source.parent}
    if out is not None:
        store.require(isinstance(out, Path) and out.name not in ("", ".", ".."), "invalid maintenance output")
        destination = out.absolute()
        # location validates the parent; this synthetic name is never opened or created.
        store.location(destination.parent / ".execution-maintenance-output")
        selected.add(destination.parent)
        protected = [source, *(Path(str(source) + suffix) for suffix in ("-journal", "-wal", "-shm"))]
        store.require(
            not any(item.is_relative_to(destination) or destination.is_relative_to(item) for item in protected),
            "maintenance output overlaps its source or SQLite sidecar paths",
        )
    with operation_locks.directories(list(selected), write=out is not None, deadline=budget):
        store.require(store.location(path) == source, "ledger source path changed while locking")
        if out is not None:
            store.require(not os.path.lexists(out), "maintenance output must be a new directory")
        yield source


def _report(path: Path, operation: str) -> Doc:
    return {
        "schema_version": VERSION,
        "operation": operation,
        "status": "unsupported",
        "ledger": str(path),
        "as_of": dt.datetime.now(dt.UTC).isoformat(),
        "store": {"application_id": store.APPLICATION_ID, "user_version": store.USER_VERSION, "journal_mode": "delete"},
        "observed_store": None,
        "source": [],
        "output": None,
        "files": [],
        "validation": {"status": "not_performed", "bindings": 0, "active": 0},
        "diagnostics": [],
        "claims": dict(CLAIMS),
    }


def _finish(report: Doc) -> Doc:
    report["digest"] = planning.digest(report)
    return report


def _validate_connection(connection: sqlite3.Connection, budget: store.Deadline, report: Doc) -> Doc:
    store.busy_timeout(connection, budget)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == INDEX_VERSION:
        from exacly import execution_index

        _v4(report)
        facts = execution_index.audit(connection, deadline=budget)
        store.require(facts["format_version"] == INDEX_VERSION, "unexpected indexed ledger audit version")
        store.require(
            facts["integrity"]["complete"] is True, "maintenance requires the complete retained-byte baseline"
        )
        store.require(facts["semantics"]["reused"] == 0, "maintenance requires fresh retained-proof validation")
        store.busy_timeout(connection, budget)
        active = connection.execute("SELECT count(*) FROM active").fetchone()[0]
        return {
            "status": "passed",
            "bindings": facts["semantics"]["bindings"],
            "active": active,
            "scope": "full_history",
            "all_retained_bytes_checked": True,
            "integrity_baseline_checked": True,
            "profile_status": facts["profile_status"],
            "semantics": facts["semantics"],
            "facts": facts,
        }
    store.require(version in (store.USER_VERSION, store.COMPLETION_VERSION), "unsupported ledger version")
    records, active_records, _ = store.validate(connection, deadline=budget)
    try:
        for record in records.values():
            executions.validate_record(record)
    except ValueError:
        raise store.StoreError("ledger contains an invalid execution binding contract") from None
    report["store"]["user_version"] = version
    return {"status": "passed", "bindings": len(records), "active": len(active_records)}


def _validate(path: Path, budget: store.Deadline, report: Doc) -> Doc:
    try:
        with store.database(path, create=False, deadline=budget) as (connection, _):
            store.busy_timeout(connection, budget)
            connection.execute("BEGIN")
            return _validate_connection(connection, budget, report)
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from None
        raise store.StoreError("ledger integrity validation failed") from None


def _capture(stack: ExitStack, path: Path, report: Doc) -> dict[str, tuple[int, Doc]]:
    found = {"ledger.sqlite3": _source(stack, path, "ledger.sqlite3")}
    header = os.pread(found["ledger.sqlite3"][0], 100, 0)
    sqlite_header = len(header) == 100 and header[:16] == b"SQLite format 3\0"
    report["observed_store"] = {
        "sqlite_header": sqlite_header,
        "application_id": int.from_bytes(header[68:72], "big") if sqlite_header else None,
        "user_version": int.from_bytes(header[60:64], "big") if sqlite_header else None,
        "journal_mode": {b"\x01\x01": "delete", b"\x02\x02": "wal"}.get(header[18:20], "unknown"),
    }
    for suffix in ("-journal", "-wal", "-shm"):
        item = Path(str(path) + suffix)
        if os.path.lexists(item):
            found["ledger.sqlite3" + suffix] = _source(stack, item, "ledger.sqlite3" + suffix)
    report["source"] = [snapshot for _, snapshot in found.values()]
    return found


def _supported(found: dict[str, tuple[int, Doc]], report: Doc) -> bool:
    fd = found["ledger.sqlite3"][0]
    store.header(fd)
    report["store"]["user_version"] = int.from_bytes(os.pread(fd, 4, 60), "big")
    store.require(not any(name.endswith(("-wal", "-shm")) for name in found), "WAL and SHM residue are unsupported")
    journal = found.get("ledger.sqlite3-journal")
    if journal is not None:
        _journal(journal[0], found["ledger.sqlite3"][0])
    return journal is not None


def _stable(found: dict[str, tuple[int, Doc]], path: Path) -> None:
    expected = {snapshot["path"] for _, snapshot in found.values()}
    actual = {str(path)} | {
        str(path) + suffix for suffix in ("-journal", "-wal", "-shm") if os.path.lexists(str(path) + suffix)
    }
    store.require(expected == actual, "ledger sidecars changed during maintenance")
    for fd, snapshot in found.values():
        _unchanged(Path(snapshot["path"]), fd, snapshot)


def inspect(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    budget = store.Deadline(lock_timeout)
    try:
        with _locks(path, None, budget) as source, ExitStack() as stack:
            report = _report(source, "inspect")
            found = _capture(stack, source, report)
            _recognized_v4(report)
            try:
                has_journal = _supported(found, report)
            except store.StoreError as error:
                report["diagnostics"].append(str(error))
            else:
                if has_journal:
                    report["status"] = "recovery_required"
                    report["diagnostics"].append("Recognized rollback journal; explicit copy recovery is required.")
                else:
                    try:
                        report["validation"] = _validate(source, budget, report)
                        report["status"] = "clean"
                    except store.StoreError as error:
                        if isinstance(error, store.LockTimeoutError):
                            raise
                        if report["schema_version"] == V4_VERSION:
                            from exacly.execution_history import UnsupportedProfileError

                            if isinstance(error, UnsupportedProfileError):
                                report["validation"]["profile_status"] = "unsupported"
                                report["diagnostics"].append(str(error))
                                _stable(found, source)
                                return _finish(report)
                        report["status"] = "corrupt"
                        report["validation"]["status"] = "failed"
                        report["diagnostics"].append(str(error))
            _stable(found, source)
            return _finish(report)
    except (OSError, sqlite3.Error) as error:
        raise store.StoreError("ledger inspection failed") from error


def _write(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())


def _copy(fd: int, path: Path) -> None:
    dest = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(dest, "wb") as destination:
        offset = 0
        while content := os.pread(fd, CHUNK, offset):
            destination.write(content)
            offset += len(content)
        destination.flush()
        os.fsync(destination.fileno())


def _sync(directory: Path) -> None:
    for path in [*directory.rglob("*"), directory]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if path.is_dir() else 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _file(path: Path, base: Path) -> Doc:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        store.file_info(fd)
        return {"path": str(path.relative_to(base)), "digest": _digest(fd), "size": os.fstat(fd).st_size}
    finally:
        os.close(fd)


def _publish(folder: Path, out: Path) -> None:
    # Reserve exclusively: rename alone could overwrite an existing empty directory.
    out.mkdir(mode=0o700)
    reserved = out.lstat()
    try:
        os.rename(folder, out)
    except BaseException:
        if _identity(out.lstat())[:2] == _identity(reserved)[:2]:
            out.rmdir()
        raise
    fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    except OSError as error:
        raise store.StoreError(
            "maintenance copy was published but durability could not be confirmed; inspect the output before retrying"
        ) from error
    finally:
        os.close(fd)


def _progress(budget: store.Deadline, status: int, remaining: int, total: int) -> None:
    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        budget.check()


def _maintain(path: Path, out: Path, operation: str, lock_timeout: float) -> Doc:
    budget = store.Deadline(lock_timeout)
    try:
        with _locks(path, out, budget) as source, ExitStack() as stack:
            report = _report(source, operation)
            found = _capture(stack, source, report)
            _recognized_v4(report)
            has_journal = _supported(found, report)
            store.require(
                has_journal == (operation == "recover"),
                "use explicit recovery for journals and a clean ledger for backup or upgrade",
            )
            if operation == "upgrade":
                store.require(
                    report["store"]["user_version"] == store.USER_VERSION, "upgrade requires a version 1 ledger"
                )
            with tempfile.TemporaryDirectory(prefix=".execution-maintenance-", dir=out.parent) as temporary:
                folder = Path(temporary)
                if operation == "backup":
                    report["validation"] = _validate(source, budget, report)
                    result = folder / "ledger.sqlite3"
                    _write(result, b"")
                    with (
                        store.database(source, create=False, deadline=budget) as (connection, _),
                        closing(sqlite3.connect(result)) as destination,
                    ):
                        connection.backup(
                            destination, pages=64, progress=lambda s, r, t: _progress(budget, s, r, t), sleep=0.01
                        )
                elif operation == "upgrade":
                    report["validation"] = _validate(source, budget, report)
                    raw, upgraded = folder / "raw", folder / "upgraded"
                    raw.mkdir(mode=0o700)
                    upgraded.mkdir(mode=0o700)
                    source_fd, captured = found["ledger.sqlite3"]
                    _copy(source_fd, raw / "ledger.sqlite3")
                    store.require(
                        _file(raw / "ledger.sqlite3", folder)["digest"] == captured["digest"],
                        "raw ledger backup does not match the captured source",
                    )
                    _copy(source_fd, upgraded / "ledger.sqlite3")
                    result = upgraded / "ledger.sqlite3"
                    with store.database(result, create=True, deadline=budget) as (connection, _):
                        completion_store.upgrade(connection, deadline=budget)
                    store.auxiliary_files(result)
                else:
                    raw, recovered = folder / "raw", folder / "recovered"
                    raw.mkdir(mode=0o700)
                    recovered.mkdir(mode=0o700)
                    for name, (fd, _) in found.items():
                        _copy(fd, raw / name)
                    _sync(raw)
                    for name in found:
                        with (raw / name).open("rb") as saved:
                            _copy(saved.fileno(), recovered / name)
                    result = recovered / "ledger.sqlite3"
                    # Only this checked working copy is opened read/write for SQLite's rollback recovery.
                    with closing(
                        sqlite3.connect(
                            result.as_uri() + "?mode=rw", uri=True, timeout=budget.remaining(), isolation_level=None
                        )
                    ) as connection:
                        store.busy_timeout(connection, budget)
                        connection.execute("BEGIN")
                        report["validation"] = _validate_connection(connection, budget, report)
                    store.auxiliary_files(result)
                report["validation"] = _validate(result, budget, report)
                if report["observed_store"]["user_version"] == INDEX_VERSION:
                    store.require(
                        report["store"]["user_version"] == INDEX_VERSION,
                        "maintenance copy changed the indexed ledger format",
                    )
                if operation == "upgrade":
                    store.require(
                        report["store"]["user_version"] == store.COMPLETION_VERSION,
                        "upgraded ledger does not have the completion schema",
                    )
                report["status"] = {"backup": "backed_up", "recover": "recovered", "upgrade": "upgraded"}[operation]
                report["output"] = str(out.absolute())
                report["files"] = [_file(item, folder) for item in sorted(folder.rglob("*")) if item.is_file()]
                _stable(found, source)
                _finish(report)
                _write(folder / "manifest.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
                _sync(folder)
                _stable(found, source)
                _publish(folder, out)
            return report
    except store.StoreError:
        raise
    except (OSError, sqlite3.Error) as error:
        if isinstance(error, sqlite3.Error) and store.sqlite_lock_error(error):
            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from None
        raise store.StoreError(f"ledger {operation} failed; source files were preserved") from error


def backup(path: Path, out: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    return _maintain(path, out, "backup", lock_timeout)


def recover(path: Path, out: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    return _maintain(path, out, "recover", lock_timeout)


def upgrade(path: Path, out: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Retain an exact clean v1 source and publish a separate validated v2 copy without cutting over."""
    return _maintain(path, out, "upgrade", lock_timeout)


def as_text(report: Doc) -> str:
    lines = [f"Ledger {report['operation']}: {report['status']}", f"Source: {planning.canonical(report['ledger'])}"]
    if report["output"] is not None:
        lines.append(f"Copy: {planning.canonical(report['output'])}")
    if report["observed_store"] is not None:
        lines.append(f"Observed header: {planning.canonical(report['observed_store'])}")
    lines += report["diagnostics"]
    lines.append("Source preserved. Select one canonical ledger explicitly; this report does not authorize a cutover.")
    return "\n".join(lines)
