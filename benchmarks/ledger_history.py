"""Profile validation of independent completed tasks, queued work and supersession chains.

Development-only, controlled offline fixtures. Run from the repository root:

    PYTHONPATH=python/src:python/tests python benchmarks/ledger_history.py --counts 10 100 300

The existing ledger_validation.py benchmark remains unchanged. This benchmark measures full schema,
binding and semantic proof validation, and separately reads/hashes the retained bytes. Hash-only work
is diagnostic and establishes none of the authority or semantics of full validation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from exacly import admission, completion_store, executions, query, state_writer
from exacly import execution_store as store
from ledger_history_fixtures import History, build

Doc = dict[str, Any]
PAYLOAD = (
    "intent_json",
    "before_state",
    "after_state",
    "archive",
    "report",
    "admission_json",
    "model_json",
    "admitted_state_json",
)


@contextmanager
def database(history: History) -> Iterator[sqlite3.Connection]:
    with store.locked(history.ledger, write=False) as (path, _), store.database(path, create=False) as (connection, _):
        yield connection


def sizes(history: History) -> Doc:
    with database(history) as connection:
        result: Doc = {
            "ledger_bytes": history.ledger.stat().st_size,
            "binding_json_bytes": connection.execute(
                "SELECT sum(length(CAST(record_json AS BLOB))) FROM bindings"
            ).fetchone()[0],
        }
        if not history.completion_ids:
            return result
        expressions = ",".join(f"sum(length(CAST({column} AS BLOB)))" for column in PAYLOAD)
        values = connection.execute(f"SELECT {expressions} FROM completion_intents").fetchone()
        result["retained_bytes_by_column"] = dict(zip(PAYLOAD, values, strict=True))
        result["retained_payload_bytes"] = sum(values)
        result["receipt_json_bytes"] = connection.execute(
            "SELECT sum(length(CAST(receipt_json AS BLOB))) FROM completion_receipts"
        ).fetchone()[0]
        return result


def hash_payloads(connection: sqlite3.Connection, completed: bool) -> tuple[int, str]:
    """Actually read and SHA256 the stored bytes, without claiming proof validation."""
    digest = hashlib.sha256()
    total = 0
    queries = ["SELECT CAST(record_json AS BLOB) FROM bindings ORDER BY id"]
    if completed:
        columns = ",".join(f"CAST({column} AS BLOB)" for column in PAYLOAD)
        queries += [
            f"SELECT {columns} FROM completion_intents ORDER BY id",
            "SELECT CAST(receipt_json AS BLOB) FROM completion_receipts ORDER BY binding_id",
        ]
    for statement in queries:
        for row in connection.execute(statement):
            for content in row:
                total += len(content)
                digest.update(content)
    return total, digest.hexdigest()


def profile(history: History) -> Doc:
    """Measure one complete validation; nested timings are inclusive, explicitly separated."""
    timings: dict[str, float] = {}
    calls: dict[str, int] = {}

    def instrument(stack: ExitStack, owner: Any, attribute: str, label: str) -> None:
        original = getattr(owner, attribute)
        timings[label] = 0.0
        calls[label] = 0

        def timed(*args: Any, **kwargs: Any) -> Any:
            calls[label] += 1
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings[label] += time.perf_counter() - started

        stack.enter_context(patch.object(owner, attribute, timed))

    with database(history) as connection:
        with ExitStack() as stack:
            for owner, attribute, label in (
                (completion_store, "validate", "completion_history_seconds"),
                (completion_store, "_payload", "payload_semantics_seconds"),
                (completion_store, "_load", "proof_load_decode_seconds"),
                (admission, "observe_report", "observation_rederivation_seconds"),
                (admission, "unpack", "archive_unpack_seconds"),
                (store, "decoded", "binding_decode_seconds"),
                (executions, "validate_record", "binding_schema_seconds"),
                (state_writer, "decode", "state_schema_decode_seconds"),
                (query, "load", "model_load_seconds"),
            ):
                instrument(stack, owner, attribute, label)
            started = time.perf_counter()
            store.validate(connection)
            total = time.perf_counter() - started
        started = time.perf_counter()
        hashed_bytes, digest = hash_payloads(connection, bool(history.completion_ids))
        hashed = time.perf_counter() - started
        started = time.perf_counter()
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        quick_check = time.perf_counter() - started
    return {
        **timings,
        "total_seconds": total,
        "base_and_sqlite_seconds": total - timings["completion_history_seconds"],
        "proof_rederivations": calls["observation_rederivation_seconds"],
        "calls_by_component": calls,
        "read_and_hash_seconds": hashed,
        "read_and_hash_bytes": hashed_bytes,
        "read_and_hash_digest": digest,
        "sqlite_quick_check_seconds": quick_check,
    }


def measure(
    counts: Sequence[int],
    *,
    depth: int = 1,
    cases: int = 1,
    requirements: int = 1,
    repeats: int = 1,
    backlog: bool = False,
    bindings_only: bool = False,
) -> list[Doc]:
    results: list[Doc] = []
    with tempfile.TemporaryDirectory(prefix="exacly-ledger-history-") as directory:
        for count in counts:
            root = Path(directory) / str(count)
            root.mkdir()
            history = build(
                root,
                count,
                depth=depth,
                cases=cases,
                requirements=requirements,
                backlog=backlog,
                bindings_only=bindings_only,
            )
            repetitions = [profile(history) for _ in range(repeats)]
            median = {
                key: statistics.median(value[key] for value in repetitions)
                for key in repetitions[0]
                if key.endswith("_seconds")
            }
            results.append(
                {
                    "tasks": count,
                    "bindings": history.bindings,
                    "supersession_depth": depth,
                    "completed_proofs": len(history.completion_ids),
                    "testcases_per_report": cases,
                    "model_requirements": requirements,
                    "state_history": "initial-backlog" if backlog or bindings_only else "sequential-queue-and-complete",
                    "schema_version": 1 if bindings_only else 2,
                    "current_binding_group_comparisons": count * history.bindings,
                    "sizes": sizes(history),
                    "median": median,
                    "repetitions": repetitions,
                }
            )
    return results


def metadata() -> Doc:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
    changed = subprocess.run(
        ["git", "status", "--porcelain"], check=True, text=True, capture_output=True
    ).stdout.splitlines()
    return {
        "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "revision": revision,
        "working_tree_changes": changed,
        "clock": "time.perf_counter",
        "cache_conditions": (
            "warm OS/SQLite caches after full fixture validation; hashes and quick_check are separate subsequent passes"
        ),
        "timing_relationship": (
            "completion_history includes payload_semantics and proof_load_decode; "
            "payload_semantics includes observation_rederivation"
        ),
        "fixture_authority": (
            "controlled offline test fixtures; byte hashing is a cost comparison, "
            "not validation or a proposed trust boundary"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[10, 100, 300])
    parser.add_argument("--supersession-depth", type=int, default=1)
    parser.add_argument("--cases", type=int, default=1)
    parser.add_argument("--requirements", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--backlog", action="store_true")
    parser.add_argument("--bindings-only", action="store_true")
    args = parser.parse_args()
    if min(*args.counts, args.supersession_depth, args.cases, args.requirements, args.repeats) < 1:
        parser.error("counts, depth, cases, requirements and repeats must be positive")
    if len(set(args.counts)) != len(args.counts) or args.requirements > args.cases:
        parser.error("counts must be distinct and requirements cannot exceed cases")
    print(
        json.dumps(
            {
                "metadata": metadata(),
                "parameters": vars(args),
                "results": measure(
                    args.counts,
                    depth=args.supersession_depth,
                    cases=args.cases,
                    requirements=args.requirements,
                    repeats=args.repeats,
                    backlog=args.backlog,
                    bindings_only=args.bindings_only,
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
