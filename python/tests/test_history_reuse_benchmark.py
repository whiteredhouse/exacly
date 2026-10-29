"""Benchmark evidence must distinguish cached terminal work, fresh audit and actual byte coverage."""

from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from exacly import completion, execution_index, history_maintenance, state_writer
from exacly import execution_store as store
from test_completion import prepare_scenario

ROOT = Path(__file__).resolve().parents[2]
Doc = dict[str, Any]


def invoke(tmp_path: Path, label: str) -> Doc:
    output = tmp_path / "measurement.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks/ledger_history_reuse.py"),
            "--workloads",
            label,
            "--counts",
            "2",
            "--out",
            str(output),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / path) for path in ("python/src", "python/tests"))},
        umask=0o022,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return dict(json.loads(output.read_text()))


def test_terminal_work_counts_and_byte_coverage_match_each_validation_mode(tmp_path: Path) -> None:
    document = invoke(tmp_path, "completed-sequential-small")
    assert document["complete"] is True
    assert len(document["results"]) == 1
    measured = document["results"][0]
    assert measured["protected_input_digests_unchanged"] is True
    assert measured["compared_source_format"] == 2 and measured["index_format"] == 4
    modes = {name: value["repetitions"][0] for name, value in measured["modes"].items()}
    assert modes["v2_full"]["facts"]["semantics"] == {"bindings": 2, "fresh": 2, "reused": 0, "prepared": 0}
    assert modes["v4_normal"]["facts"]["semantics"] == {"bindings": 2, "fresh": 0, "reused": 2, "prepared": 0}
    assert modes["v4_audit"]["facts"]["semantics"] == {"bindings": 2, "fresh": 2, "reused": 0, "prepared": 0}
    assert modes["v2_full"]["calls"]["observation_rederivation_seconds"] == 2
    assert modes["v4_normal"]["calls"]["observation_rederivation_seconds"] == 0
    assert modes["v4_audit"]["calls"]["observation_rederivation_seconds"] == 2
    for name, mode in modes.items():
        assert mode["coverage"] == measured["retained_cells"]
        if name != "v2_full":
            assert mode["facts"]["integrity"]["complete"] is True
            assert mode["facts"]["integrity"]["bytes"] == measured["retained_cells"]["bytes"]
            assert mode["calls"]["installed_profile_seconds"] > 0


def test_open_binding_workload_explicitly_upgrades_v1_before_comparing(tmp_path: Path) -> None:
    measured = invoke(tmp_path, "open-bindings")["results"][0]
    assert measured["published_source_format"] == 1
    assert measured["compared_source_format"] == 2
    assert measured["index_format"] == 4
    assert measured["protected_input_digests_unchanged"] is True
    for mode in measured["modes"].values():
        semantics = mode["repetitions"][0]["facts"]["semantics"]
        assert semantics == {"bindings": 2, "fresh": 0, "reused": 0, "prepared": 0}


@pytest.fixture
def benchmark(monkeypatch: pytest.MonkeyPatch) -> Doc:
    monkeypatch.syspath_prepend(str(ROOT / "benchmarks"))
    return runpy.run_path(str(ROOT / "benchmarks/ledger_history_reuse.py"))


def test_default_matrix_covers_every_published_workload(benchmark: Doc) -> None:
    published = json.loads((ROOT / "benchmarks/results/ledger_history_baseline.json").read_text())
    expected = {(group["label"], row["tasks"]) for group in published["runs"] for row in group["results"]}
    actual = {(row.label, row.tasks) for row in benchmark["workloads"]()}
    assert actual == expected
    assert len(actual) == 16


def test_prepared_payload_is_fresh_in_both_normal_and_full_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, benchmark: Doc
) -> None:
    case = prepare_scenario(tmp_path)
    output = tmp_path / "active"
    assert history_maintenance.activate(case.ledger, output)["status"] == "activated"
    case.ledger = output / "operational/ledger.sqlite3"

    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise OSError("retain prepared intent without publishing state")

    with monkeypatch.context() as altered:
        altered.setattr(state_writer, "publish_locked", interrupt)
        with pytest.raises(completion.CompletionInterruptedError):
            case.complete()
    profile = execution_index.profile()["digest"]
    for mode in ("v4_normal", "v4_audit"):
        result = benchmark["measure"](case.ledger, mode, profile)
        assert result["facts"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 0, "prepared": 1}
        assert result["fresh_prepared_payloads_observed"] == 1
        assert result["calls"]["observation_rederivation_seconds"] == 1


def test_corrupted_terminal_bytes_are_rejected_before_semantic_reuse(tmp_path: Path, benchmark: Doc) -> None:
    case = prepare_scenario(tmp_path)
    case.complete()
    output = tmp_path / "active"
    assert history_maintenance.activate(case.ledger, output)["status"] == "activated"
    indexed = output / "operational/ledger.sqlite3"
    with sqlite3.connect(indexed) as connection:
        content = connection.execute("SELECT report FROM completion_intents").fetchone()[0]
        connection.execute("UPDATE completion_intents SET report=?", (content + b"\n",))
    before = indexed.read_bytes()
    with pytest.raises(store.StoreError):
        benchmark["measure"](indexed, "v4_normal", execution_index.profile()["digest"])
    assert indexed.read_bytes() == before
