"""Compare v2 full validation, operational v4 reuse and v4 full audit on identical history.

Run from the repository root with development dependencies installed:

    PYTHONPATH=python/src:python/tests python benchmarks/ledger_history_reuse.py --out results.json

By default all 16 previously published workload dimensions run once. Fixture construction, explicit
v1-to-v2 upgrade, copy-only activation, locks and connection setup are outside the measured engine.
The normal and full-audit timers include their actual installed-profile and whole-byte checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from exacly import admission, completion_store, execution_index, execution_maintenance, executions, history_maintenance
from exacly import execution_history as history
from exacly import execution_store as store
from ledger_history import metadata, sizes
from ledger_history_fixtures import build

Doc = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = ROOT / "benchmarks/results/ledger_history_baseline.json"
KEYS = {
    "bindings": "id",
    "active": "pending_key",
    "watermarks": "pending_key",
    "completion_intents": "id",
    "completion_receipts": "binding_id",
}
MODES = ("v2_full", "v4_normal", "v4_audit")


@dataclass(frozen=True)
class Workload:
    label: str
    tasks: int
    depth: int = 1
    cases: int = 1
    requirements: int = 1
    backlog: bool = False
    bindings_only: bool = False


def workloads(labels: Sequence[str] | None = None, counts: Sequence[int] | None = None) -> list[Workload]:
    published = json.loads(PUBLISHED.read_text())
    selected: list[Workload] = []
    known = {run["label"] for run in published["runs"]}
    if labels is not None and not set(labels) <= known:
        raise ValueError("unknown published workload label")
    if counts is not None and (not counts or min(counts) < 1 or len(set(counts)) != len(counts)):
        raise ValueError("task counts must be distinct positive integers")
    for run in published["runs"]:
        if labels is not None and run["label"] not in labels:
            continue
        params = run["parameters"]
        for count in counts if counts is not None else [row["tasks"] for row in run["results"]]:
            selected.append(
                Workload(
                    run["label"],
                    count,
                    params["supersession_depth"],
                    params["cases"],
                    params["requirements"],
                    params["backlog"],
                    params["bindings_only"],
                )
            )
    return selected


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    with store.locked(path, write=False) as (absolute, _), store.database(absolute, create=False) as (connection, _):
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()


def coverage(connection: sqlite3.Connection) -> Doc:
    """Independently compare exact typed legacy cells; this digest grants no authority."""
    digest = hashlib.sha256(b"exacly-benchmark-legacy-cells-v1\0")
    rows = cells = byte_count = 0
    for table in sorted(KEYS):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        expressions = ",".join(f"typeof({name}),CAST({name} AS BLOB)" for name in columns)
        for row in connection.execute(f"SELECT {expressions} FROM {table} ORDER BY {KEYS[table]}"):
            rows += 1
            for index, name in enumerate(columns):
                kind, raw = row[index * 2 : index * 2 + 2]
                cells += 1
                byte_count += len(raw) if raw is not None else 0
                for value in (table.encode(), name.encode(), kind.encode(), raw):
                    digest.update(b"\x00" if value is None else b"\x01" + len(value).to_bytes(8, "big") + value)
    return {"rows": rows, "cells": cells, "bytes": byte_count, "diagnostic_digest": digest.hexdigest()}


def measure(path: Path, mode: str, expected_profile: str) -> Doc:
    if mode not in MODES:
        raise ValueError("unknown validation mode")
    timings: dict[str, float] = {}
    calls: dict[str, int] = {}
    terminal_ids: set[str] = set()
    prepared_ids: set[str] = set()

    def instrument(stack: ExitStack, owner: Any, attribute: str, label: str) -> None:
        original = getattr(owner, attribute)
        timings[label] = 0.0
        calls[label] = 0

        def timed(*args: Any, **kwargs: Any) -> Any:
            calls[label] += 1
            if label == "semantic_facts_seconds":
                envelope = args[0]
                selected = prepared_ids if envelope["phase"] == "prepared" else terminal_ids
                selected.add(envelope["intent"]["id"])
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings[label] += time.perf_counter() - started

        stack.enter_context(patch.object(owner, attribute, timed))

    with database(path) as connection:
        expected_coverage = coverage(connection)
        with ExitStack() as stack:
            for owner, attribute, label in (
                (history, "profile", "installed_profile_seconds"),
                (execution_index, "_baseline", "integrity_baseline_seconds"),
                (completion_store, "semantic_facts", "semantic_facts_seconds"),
                (admission, "observe_report", "observation_rederivation_seconds"),
                (executions, "validate_record", "binding_schema_seconds"),
                (store, "decoded", "binding_decode_seconds"),
            ):
                instrument(stack, owner, attribute, label)
            started = time.perf_counter()
            if mode == "v2_full":
                snapshot = store.validate(connection)
                facts: Doc = {
                    "format_version": 2,
                    "semantics": {
                        "bindings": len(snapshot[0]),
                        "fresh": len(terminal_ids),
                        "reused": 0,
                        "prepared": len(prepared_ids),
                    },
                    "integrity_baseline_checked": False,
                }
            else:
                actual = (
                    execution_index.check(connection).facts
                    if mode == "v4_normal"
                    else execution_index.audit(connection)
                )
                facts = {
                    key: actual[key]
                    for key in ("format_version", "profile_status", "integrity", "semantics", "index_rows")
                }
                facts["profile_digest"] = actual["profile"]["digest"]
                assert facts["profile_digest"] == expected_profile, "installed profile changed during measurement"
            elapsed = time.perf_counter() - started
        if mode != "v2_full":
            assert facts["integrity"]["complete"] is True
            assert all(facts["integrity"][key] == expected_coverage[key] for key in ("rows", "cells", "bytes"))
            assert facts["semantics"]["fresh"] == len(terminal_ids)
            assert facts["semantics"]["prepared"] == len(prepared_ids)
    return {
        "elapsed_seconds": elapsed,
        "component_seconds": timings,
        "calls": calls,
        "fresh_terminal_payloads_observed": len(terminal_ids),
        "fresh_prepared_payloads_observed": len(prepared_ids),
        "coverage": expected_coverage,
        "facts": facts,
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(root: Path, workload: Workload, *, repeats: int, expected_profile: str) -> Doc:
    if repeats < 1:
        raise ValueError("repetitions must be positive")
    fixture = root / "fixture"
    fixture.mkdir(mode=0o700)
    source = build(
        fixture,
        workload.tasks,
        depth=workload.depth,
        cases=workload.cases,
        requirements=workload.requirements,
        backlog=workload.backlog,
        bindings_only=workload.bindings_only,
    )
    v1_upgrade = 0.0
    if workload.bindings_only:
        started = time.perf_counter()
        execution_maintenance.upgrade(source.ledger, root / "version2")
        v1_upgrade = time.perf_counter() - started
        source = replace(source, ledger=root / "version2/upgraded/ledger.sqlite3")
    original = {path: _digest(path) for path in (source.ledger, source.state)}
    with database(source.ledger) as connection:
        source_cells = coverage(connection)
    started = time.perf_counter()
    converted = history_maintenance.activate(source.ledger, root / "version4")
    activation = time.perf_counter() - started
    assert converted["status"] == "activated", converted
    indexed = root / "version4/operational/ledger.sqlite3"
    original[indexed] = _digest(indexed)
    with database(indexed) as connection:
        assert coverage(connection) == source_cells, "activation changed retained legacy cells"
    output: Doc = {}
    for mode in MODES:
        path = source.ledger if mode == "v2_full" else indexed
        repetitions = [measure(path, mode, expected_profile) for _ in range(repeats)]
        output[mode] = {
            "median_seconds": statistics.median(row["elapsed_seconds"] for row in repetitions),
            "repetitions": repetitions,
        }
    assert {path: _digest(path) for path in original} == original, "benchmark operation modified protected inputs"
    assert execution_index.profile()["digest"] == expected_profile, "installed profile changed while benchmarking"
    normal = output["v4_normal"]["median_seconds"]
    full = output["v2_full"]["median_seconds"]
    return {
        "label": workload.label,
        "tasks": workload.tasks,
        "bindings": source.bindings,
        "supersession_depth": workload.depth,
        "testcases_per_report": workload.cases,
        "model_requirements": workload.requirements,
        "backlog": workload.backlog,
        "bindings_only": workload.bindings_only,
        "published_source_format": 1 if workload.bindings_only else 2,
        "compared_source_format": 2,
        "index_format": 4,
        "repeats": repeats,
        "v1_upgrade_seconds_excluded": v1_upgrade,
        "activation_seconds_excluded": activation,
        "source_sizes": sizes(source),
        "indexed_sizes": sizes(replace(source, ledger=indexed)),
        "retained_cells": source_cells,
        "protected_input_digests_unchanged": True,
        "v4_normal_divided_by_v2_full": normal / full,
        "modes": output,
    }


def document(expected_profile: Doc) -> Doc:
    return {
        "schema_version": "0.1.0",
        "benchmark": "benchmarks/ledger_history_reuse.py",
        "complete": False,
        "metadata": {
            **metadata(),
            "cache_conditions": (
                "warm after fixture validation, activation and independent typed-cell comparison; "
                "indexed engine timing includes its own profile and integrity checks"
            ),
            "timing_relationship": (
                "component timers are inclusive; installed profile and integrity baseline run inside "
                "the indexed elapsed timer; no separate hash-only or quick_check timing pass"
            ),
            "installed_operational_profile": expected_profile,
            "harness_digest": _digest(Path(__file__)),
            "published_harness_digest": _digest(Path(__file__)),
            "fixture_digest": _digest(ROOT / "python/tests/ledger_history_fixtures.py"),
            "published_workloads_digest": _digest(PUBLISHED),
        },
        "method": {
            "comparison": "same canonical retained cells; v2 full semantics vs v4 normal engine vs v4 full audit",
            "baseline": (
                "both engines share grouping and compiled schema helpers, but v4 freshly checks every binding "
                "while v2 checks full binding schemas through retained payload semantics; "
                "historical v1 metadata-only and old schema-construction timings are separate"
            ),
            "timed_scope": (
                "protected coherent read transaction; locks/open/setup/upgrade/activation excluded; "
                "profile+baseline+semantic work included"
            ),
            "timers": (
                "inclusive component timers; do not sum them; returned facts are cross-checked against "
                "observed semantic calls and independent typed-cell coverage"
            ),
            "coverage": (
                "source-volume counters cover original retained typed cells; index metadata and installed-profile "
                "reads are additional work included in elapsed time, not a measured total physical IO volume"
            ),
            "cache": (
                "warm after fixture full validation, explicit activation and independent retained-byte comparison; "
                "no cold-cache claim"
            ),
            "authority": (
                "controlled offline fixtures; no live GitHub, candidate execution, dispatch, task closure "
                "or authority granted by benchmark digests"
            ),
            "repetitions": (
                "one per workload by default; elapsed results are diagnostic, "
                "not machine-dependent acceptance thresholds"
            ),
            "growth": (
                "normal checks still read all retained bytes, recheck global structure and fresh binding schemas; "
                "storage/latency are not bounded"
            ),
        },
        "results": [],
    }


def write(path: Path, result: Doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(result, indent=2) + "\n")
    pending.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workloads", nargs="+")
    parser.add_argument("--counts", nargs="+", type=int)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        selected = workloads(args.workloads, args.counts)
        if args.repeats < 1:
            raise ValueError("repetitions must be positive")
    except ValueError as error:
        parser.error(str(error))
    installed = execution_index.profile()
    result = document(installed)
    result["parameters"] = vars(args) | {"out": str(args.out) if args.out is not None else None}
    with tempfile.TemporaryDirectory(prefix="exacly-history-reuse-") as folder:
        for number, workload in enumerate(selected):
            root = Path(folder) / str(number)
            root.mkdir(mode=0o700)
            print(f"starting {workload.label}/{workload.tasks}", file=sys.stderr)
            measured = compare(root, workload, repeats=args.repeats, expected_profile=installed["digest"])
            result["results"].append(measured)
            if args.out is not None:
                write(args.out, result)
            print(
                f"{workload.label}/{workload.tasks}: v2={measured['modes']['v2_full']['median_seconds']:.3f}s "
                f"v4={measured['modes']['v4_normal']['median_seconds']:.3f}s "
                f"audit={measured['modes']['v4_audit']['median_seconds']:.3f}s",
                file=sys.stderr,
            )
    result["complete"] = True
    if args.out is not None:
        write(args.out, result)
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
