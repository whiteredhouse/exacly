"""History copies are explicit offline audit artifacts, never operational ledger replacements."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest
from exacly.cli import exacly_main
from test_completion import prepare_scenario
from test_execution_store import process_lock

Doc = dict[str, Any]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from exacly import github_evidence

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("history commands must not construct a network client")

    monkeypatch.setattr(github_evidence, "GitHubClient", forbidden)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Doc:
    from exacly import history_maintenance

    result: Doc = {"calls": [], "answer": None}

    def convert(ledger: Path, out: Path, **kwargs: Any) -> Any:
        result["calls"].append(("index", (ledger, out), kwargs))
        return result["answer"] or {
            "operation": "index",
            "status": "ready" if kwargs["dry_run"] else "indexed",
            "source_unchanged": True,
        }

    def audit(ledger: Path, **kwargs: Any) -> Any:
        result["calls"].append(("audit", (ledger,), kwargs))
        return result["answer"] or {"operation": "audit", "status": "passed", "source_unchanged": True}

    # The fake backend has its own closed report contract; real reports are checked below as well.
    monkeypatch.setattr(
        history_maintenance,
        "SCHEMA",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation", "status", "source_unchanged"],
            "properties": {
                "operation": {"enum": ["index", "audit"]},
                "status": {"enum": ["ready", "indexed", "passed", "corrupt", "unsupported", "recovery_required"]},
                "source_unchanged": {"const": True},
            },
        },
    )
    monkeypatch.setattr(history_maintenance, "convert", convert)
    monkeypatch.setattr(history_maintenance, "audit", audit)
    monkeypatch.setattr(history_maintenance, "as_text", lambda report: f"History {report['status']}")
    return result


def arguments(tmp_path: Path, operation: str, *, dry_run: bool = False) -> list[str]:
    args = ["executions", operation, "--ledger", str(tmp_path / "source.sqlite3")]
    if operation == "index":
        args.extend(["--out", str(tmp_path / "indexed-copy")])
        if dry_run:
            args.append("--dry-run")
    return args


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("seconds", [None, "0", "0.125", "7"])
def test_index_forwards_only_explicit_options_and_needs_no_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, dry_run: bool, seconds: str | None
) -> None:
    args = arguments(tmp_path, "index", dry_run=dry_run)
    if seconds is not None:
        args.extend(["--lock-timeout", seconds])
    assert exacly_main([*args, "--json"]) == 0
    captured = capsys.readouterr()
    expected = {"operation": "index", "status": "ready" if dry_run else "indexed", "source_unchanged": True}
    assert json.loads(captured.out) == expected and not captured.err
    options: Doc = {"dry_run": dry_run}
    if seconds is not None:
        options["lock_timeout"] = float(seconds)
    assert backend["calls"] == [("index", (tmp_path / "source.sqlite3", tmp_path / "indexed-copy"), options)]
    assert not (tmp_path / "indexed-copy").exists()


@pytest.mark.parametrize("status,code", [("passed", 0), ("corrupt", 1), ("unsupported", 1), ("recovery_required", 1)])
def test_audit_diagnostics_have_distinct_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, status: str, code: int
) -> None:
    report = {"operation": "audit", "status": status, "source_unchanged": True}
    backend["answer"] = report
    assert exacly_main([*arguments(tmp_path, "audit"), "--lock-timeout", "0.25", "--json"]) == code
    captured = capsys.readouterr()
    assert json.loads(captured.out) == report and not captured.err
    assert backend["calls"] == [("audit", (tmp_path / "source.sqlite3",), {"lock_timeout": 0.25})]


@pytest.mark.parametrize("operation,status", [("index", "indexed"), ("audit", "passed")])
def test_human_output_uses_the_validated_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, operation: str, status: str
) -> None:
    assert exacly_main(arguments(tmp_path, operation)) == 0
    assert capsys.readouterr().out == f"History {status}\n"


@pytest.mark.parametrize("operation", ["index", "audit"])
@pytest.mark.parametrize("seconds", ["-1", "nan", "inf", "-inf", "not-a-number"])
def test_invalid_wait_budget_is_rejected_before_backend_calls(
    tmp_path: Path, backend: Doc, operation: str, seconds: str
) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main([*arguments(tmp_path, operation), f"--lock-timeout={seconds}"])
    assert error.value.code == 2 and not backend["calls"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_even_dry_run_requires_a_new_disjoint_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, dry_run: bool, kind: str
) -> None:
    output = tmp_path / "indexed-copy"
    if kind == "directory":
        output.mkdir()
        (output / "keep").write_bytes(b"retain directory")
    elif kind == "file":
        output.write_bytes(b"retain file")
    else:
        target = tmp_path / "source.sqlite3"
        if kind == "symlink":
            target.write_bytes(b"retain source")
        output.symlink_to(target)
    assert exacly_main(arguments(tmp_path, "index", dry_run=dry_run)) == 2
    captured = capsys.readouterr()
    assert "new directory" in captured.err and not captured.out and not backend["calls"]
    if kind == "directory":
        assert (output / "keep").read_bytes() == b"retain directory"
    elif kind == "file":
        assert output.read_bytes() == b"retain file"
    else:
        assert output.is_symlink()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_index_rejects_source_and_auxiliary_path_overlap_before_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, dry_run: bool, suffix: str
) -> None:
    ledger = tmp_path / "source.sqlite3"
    output = ledger.with_name(ledger.name + suffix)
    args = ["executions", "index", "--ledger", str(ledger), "--out", str(output)]
    if dry_run:
        args.append("--dry-run")
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "overlap" in captured.err and not captured.out
    assert not backend["calls"] and not output.exists()


@pytest.mark.parametrize("operation", ["index", "audit"])
@pytest.mark.parametrize("failure", [ValueError("unsafe history source"), OSError("source unavailable")])
def test_backend_errors_are_clean_and_do_not_create_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: Doc,
    operation: str,
    failure: Exception,
) -> None:
    from exacly import history_maintenance

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(history_maintenance, "convert" if operation == "index" else "audit", fail)
    assert exacly_main([*arguments(tmp_path, operation), "--json"]) == 2
    captured = capsys.readouterr()
    assert "exacly executions:" in captured.err and "Traceback" not in captured.err and not captured.out
    assert not (tmp_path / "indexed-copy").exists()


@pytest.mark.parametrize("operation", ["index", "audit"])
@pytest.mark.parametrize("changed", [{"status": "guessed"}, {"source_unchanged": False}, {"unexpected": True}])
def test_invalid_report_schema_is_never_printed_as_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, operation: str, changed: Doc
) -> None:
    report = {
        "operation": operation,
        "status": "indexed" if operation == "index" else "passed",
        "source_unchanged": True,
    }
    backend["answer"] = {**report, **changed}
    assert exacly_main([*arguments(tmp_path, operation), "--json"]) == 2
    captured = capsys.readouterr()
    assert "invalid report" in captured.err and not captured.out


@pytest.mark.parametrize("operation", ["index", "audit"])
@pytest.mark.parametrize("report", [None, [], "passed"])
def test_nonobject_backend_reports_are_clean_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: Doc,
    operation: str,
    report: Any,
) -> None:
    from exacly import history_maintenance

    monkeypatch.setattr(history_maintenance, "convert" if operation == "index" else "audit", lambda *a, **kw: report)
    assert exacly_main([*arguments(tmp_path, operation), "--json"]) == 2
    captured = capsys.readouterr()
    assert "invalid report" in captured.err and "Traceback" not in captured.err and not captured.out


@pytest.mark.parametrize(
    "operation,dry_run,reported_operation,status",
    [("index", False, "index", "ready"), ("index", True, "index", "indexed"), ("audit", False, "index", "passed")],
)
def test_other_valid_report_variants_cannot_claim_the_requested_operation_succeeded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    backend: Doc,
    operation: str,
    dry_run: bool,
    reported_operation: str,
    status: str,
) -> None:
    backend["answer"] = {"operation": reported_operation, "status": status, "source_unchanged": True}
    assert exacly_main([*arguments(tmp_path, operation, dry_run=dry_run), "--json"]) == 2
    captured = capsys.readouterr()
    assert "invalid operation or status" in captured.err and not captured.out


@pytest.mark.parametrize(
    "args",
    [
        ["executions", "index", "--ledger", "source"],
        ["executions", "index", "--out", "copy"],
        ["executions", "audit", "--ledger", "source", "--out", "copy"],
        ["executions", "audit", "--ledger", "source", "--dry-run"],
    ],
)
def test_destination_is_explicit_and_audit_accepts_no_mutation_options(backend: Doc, args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2 and not backend["calls"]


def test_index_help_states_the_audit_only_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main(["executions", "index", "--help"])
    assert error.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "audit-only" in help_text and "no operational cutover" in help_text
    assert "cannot be used by ordinary execution or completion commands" in help_text


def files(directory: Path) -> dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_real_index_dry_run_conversion_and_fresh_audit_preserve_the_operational_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import admission, history_maintenance

    case = prepare_scenario(tmp_path)
    completed = case.complete()
    source_bytes, state_bytes = case.ledger.read_bytes(), case.state_path.read_bytes()
    before = files(tmp_path)
    output = tmp_path / "audit-copy"
    args = ["executions", "index", "--ledger", str(case.ledger), "--out", str(output), "--json"]
    assert exacly_main([*args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    jsonschema.validate(preview, history_maintenance.SCHEMA)
    assert preview["status"] == "ready" and files(tmp_path) == before and not output.exists()
    assert exacly_main(args) == 0
    indexed = json.loads(capsys.readouterr().out)
    jsonschema.validate(indexed, history_maintenance.SCHEMA)
    assert indexed["status"] == "indexed"
    assert json.loads((output / "manifest.json").read_text()) == indexed
    assert (output / "raw" / "ledger.sqlite3").read_bytes() == source_bytes
    ledger = output / "indexed" / "ledger.sqlite3"
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    copied_bytes = ledger.read_bytes()
    with patch.object(admission, "observe_report", wraps=admission.observe_report) as observed:
        for _ in range(2):
            assert exacly_main(["executions", "audit", "--ledger", str(ledger), "--json"]) == 0
            audited = json.loads(capsys.readouterr().out)
            jsonschema.validate(audited, history_maintenance.SCHEMA)
            assert audited["status"] == "passed" and audited["validation"]["semantics"]["reused"] == 0
        assert observed.call_count == 2
    assert ledger.read_bytes() == copied_bytes
    for command in (
        ["executions", "show", "--ledger", str(ledger), "--binding", case.binding["id"]],
        ["completions", "show", "--ledger", str(ledger), "--completion", completed["completion_id"]],
    ):
        assert exacly_main(command) == 2
        captured = capsys.readouterr()
        assert "version" in captured.err and "Traceback" not in captured.err and not captured.out
    assert case.ledger.read_bytes() == source_bytes and case.state_path.read_bytes() == state_bytes
    assert {name: value for name, value in files(tmp_path).items() if not name.startswith("audit-copy/")} == before


def test_real_fresh_audit_rejects_changed_retained_proof_without_repairing_the_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import history_maintenance

    case = prepare_scenario(tmp_path)
    case.complete()
    output = tmp_path / "audit-copy"
    history_maintenance.convert(case.ledger, output)
    ledger = output / "indexed" / "ledger.sqlite3"
    assert exacly_main(["executions", "audit", "--ledger", str(ledger), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    with sqlite3.connect(ledger) as connection:
        identity, report = connection.execute("SELECT id,report FROM completion_intents").fetchone()
        connection.execute("UPDATE completion_intents SET report=? WHERE id=?", (report + b"\n", identity))
    changed = files(tmp_path)
    assert exacly_main(["executions", "audit", "--ledger", str(ledger), "--json"]) == 1
    captured = capsys.readouterr()
    audited = json.loads(captured.out)
    jsonschema.validate(audited, history_maintenance.SCHEMA)
    assert audited["status"] == "corrupt" and audited["diagnostics"] and not captured.err
    assert files(tmp_path) == changed


def test_real_audit_reports_unsupported_ordinary_ledger_without_changing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import history_maintenance

    case = prepare_scenario(tmp_path)
    before = files(tmp_path)
    assert exacly_main(["executions", "audit", "--ledger", str(case.ledger), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    jsonschema.validate(report, history_maintenance.SCHEMA)
    assert report["status"] == "unsupported" and not captured.err and files(tmp_path) == before


@pytest.mark.parametrize("operation", ["index", "audit"])
def test_real_directory_lock_timeout_is_clean_and_leaves_all_files_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    from exacly import history_maintenance

    case = prepare_scenario(tmp_path)
    ledger = case.ledger
    if operation == "audit":
        output = tmp_path / "indexed-input"
        history_maintenance.convert(ledger, output)
        ledger = output / "indexed" / "ledger.sqlite3"
    before = files(tmp_path)
    args = ["executions", operation, "--ledger", str(ledger), "--lock-timeout", "0.05", "--json"]
    if operation == "index":
        args.extend(["--out", str(tmp_path / "timed-out-copy")])
    with process_lock(ledger, "directory"):
        started = time.monotonic()
        assert exacly_main(args) == 2
        assert time.monotonic() - started < 1.0
    captured = capsys.readouterr()
    assert "lock timed out" in captured.err and "--lock-timeout" in captured.err and not captured.out
    assert files(tmp_path) == before
