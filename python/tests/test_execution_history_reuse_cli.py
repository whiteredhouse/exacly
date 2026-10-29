"""Operational history activation preserves its source and requires explicit ledger selection."""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest
from exacly import admission, completion, executions, history_maintenance, planning, state_writer
from exacly import execution_index as index
from exacly import execution_maintenance as maintenance
from exacly import execution_store as store
from exacly.cli import exacly_main
from test_applicability import B
from test_completion import prepare_scenario
from test_execution_history_cli import files
from test_execution_store import process_lock
from test_executions import OPENED
from test_reconciliation import selected

Doc = dict[str, Any]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from exacly import github_evidence

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("history maintenance must not construct a network client")

    monkeypatch.setattr(github_evidence, "GitHubClient", forbidden)


def args(ledger: Path, output: Path) -> list[str]:
    return ["executions", "activate", "--ledger", str(ledger), "--out", str(output), "--json"]


def operational(ledger: Path, output: Path) -> Path:
    history_maintenance.activate(ledger, output)
    return output / "operational" / "ledger.sqlite3"


def changed_profile(monkeypatch: pytest.MonkeyPatch) -> Doc:
    """Represent another complete trusted installation in the same supported validator family."""
    current = copy.deepcopy(index.profile())
    current["inputs"][0]["digest"] = "sha256:" + "a" * 64
    current["digest"] = planning.digest({key: value for key, value in current.items() if key != "digest"})
    monkeypatch.setattr(index, "profile", lambda: copy.deepcopy(current))
    return current


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("timeout", [None, "0", "0.25"])
def test_activation_forwards_explicit_options_and_validates_its_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    timeout: str | None,
) -> None:
    ledger, output = tmp_path / "ledger.sqlite3", tmp_path / "new-copy"
    calls: list[Any] = []

    def activate(path: Path, out: Path, **options: Any) -> Doc:
        calls.append((path, out, options))
        report = history_maintenance._report(path, "activate", dry_run=options["dry_run"])
        report["status"] = "ready" if options["dry_run"] else "activated"
        return history_maintenance._finish(report)

    monkeypatch.setattr(history_maintenance, "activate", activate)
    command = args(ledger, output)
    if dry_run:
        command.append("--dry-run")
    if timeout is not None:
        command.extend(["--lock-timeout", timeout])
    assert exacly_main(command) == 0
    report = json.loads(capsys.readouterr().out)
    jsonschema.validate(report, history_maintenance.OPERATIONAL_SCHEMA)
    assert report["schema_version"] == "0.2.0" and report["claims"]["eligible_operational"] is True
    options: Doc = {"dry_run": dry_run}
    if timeout is not None:
        options["lock_timeout"] = float(timeout)
    assert calls == [(ledger, output, options)] and not output.exists()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_activation_path_guards_apply_before_backend_even_for_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    existing: bool,
) -> None:
    ledger = tmp_path / "ledger.sqlite3"
    ledger.write_bytes(b"preserved source")
    output = tmp_path / "existing" if existing else ledger.with_name(ledger.name + "-journal")
    if existing:
        output.mkdir()
        (output / "keep").write_bytes(b"preserved output")
    calls: list[Any] = []
    monkeypatch.setattr(history_maintenance, "activate", lambda *a, **kw: calls.append((a, kw)))
    command = args(ledger, output)
    if dry_run:
        command.append("--dry-run")
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert ("new directory" if existing else "overlap") in captured.err and not captured.out and not calls
    assert ledger.read_bytes() == b"preserved source"
    if existing:
        assert (output / "keep").read_bytes() == b"preserved output"


@pytest.mark.parametrize("failure", [ValueError("unsupported profile"), OSError("source unavailable")])
def test_activation_backend_failures_are_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    def fail(*a: Any, **kw: Any) -> Doc:
        raise failure

    monkeypatch.setattr(history_maintenance, "activate", fail)
    assert exacly_main(args(tmp_path / "ledger", tmp_path / "new")) == 2
    captured = capsys.readouterr()
    assert "exacly executions:" in captured.err and "Traceback" not in captured.err and not captured.out
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("field,value", [("status", "indexed"), ("operation", "audit"), ("schema_version", "0.1.0")])
def test_activation_does_not_print_another_report_variant_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], field: str, value: str
) -> None:
    report = history_maintenance._report(tmp_path / "ledger", "activate")
    report["status"] = "activated"
    report[field] = value
    monkeypatch.setattr(history_maintenance, "activate", lambda *a, **kw: report)
    assert exacly_main(args(tmp_path / "ledger", tmp_path / "new")) == 2
    captured = capsys.readouterr()
    assert "invalid" in captured.err and not captured.out


def test_activation_help_explains_canonical_selection_and_profile_refresh(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main(["executions", "activate", "--help"])
    assert error.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "v2, v3 or v4" in text and "known stale validator profile" in text
    assert "Select one canonical ledger path explicitly" in text and "no automatic cutover" in text


@pytest.mark.parametrize("version", [2, 3, 4])
def test_real_activation_accepts_each_source_format_without_cutover_or_source_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int
) -> None:
    case = prepare_scenario(tmp_path)
    case.complete()
    source = case.ledger
    if version == 3:
        history_maintenance.convert(source, tmp_path / "v3-source")
        source = tmp_path / "v3-source" / "indexed" / "ledger.sqlite3"
    elif version == 4:
        source = operational(source, tmp_path / "v4-source")
    before, source_bytes = files(tmp_path), source.read_bytes()
    output = tmp_path / "activated"
    command = args(source, output)
    assert exacly_main([*command, "--dry-run"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "ready" and files(tmp_path) == before and not output.exists()
    assert exacly_main(command) == 0
    report = json.loads(capsys.readouterr().out)
    jsonschema.validate(report, history_maintenance.OPERATIONAL_SCHEMA)
    assert report["status"] == "activated" and report["format_version"] == 4
    assert report["validation"]["semantics"] == {"bindings": 1, "fresh": 1, "reused": 0, "prepared": 0}
    assert report["validation"]["all_retained_bytes_checked"] is True
    assert report["validation"]["integrity_baseline_checked"] is True
    assert report["claims"]["automatic_cutover"] is False and report["claims"]["latest_state_verified"] is False
    assert (output / "raw" / "ledger.sqlite3").read_bytes() == source_bytes
    assert json.loads((output / "manifest.json").read_text()) == report
    target = output / "operational" / "ledger.sqlite3"
    with patch.object(admission, "observe_report", wraps=admission.observe_report) as observed:
        assert (
            exacly_main(["executions", "show", "--ledger", str(target), "--binding", case.binding["id"], "--json"]) == 0
        )
        shown = json.loads(capsys.readouterr().out)
        assert shown["binding"]["id"] == case.binding["id"]
        assert shown["validation"]["profile_status"] == "current"
        assert shown["validation"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 1, "prepared": 0}
        assert observed.call_count == 0
    with patch.object(admission, "observe_report", wraps=admission.observe_report) as observed:
        for _ in range(2):
            assert exacly_main(["executions", "audit", "--ledger", str(target), "--json"]) == 0
            audited = json.loads(capsys.readouterr().out)
            assert audited["status"] == "passed" and audited["validation"]["semantics"]["fresh"] == 1
            assert audited["validation"]["semantics"]["reused"] == 0
        assert observed.call_count == 2
    assert source.read_bytes() == source_bytes
    assert {name: content for name, content in files(tmp_path).items() if not name.startswith("activated/")} == before


def test_known_stale_read_only_fallback_and_explicit_copy_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = prepare_scenario(tmp_path)
    case.complete()
    source = operational(case.ledger, tmp_path / "old-profile")
    before = files(tmp_path)
    next_state = case.state
    next_state["serial"] += 1
    next_state["pending"].append(
        {"record": "REQ-1", "task": "issues/new", "opened_at": OPENED, "dimension": "verification"}
    )
    request = executions.Registration(
        case.plan,
        case.plan["actions"][0]["id"],
        next_state,
        "issues/new",
        OPENED,
        case.readiness,
        case.source.policy,
        selected(B),
        executions.timestamp(next_state["written_at"]).date(),
    )
    current = changed_profile(monkeypatch)
    assert exacly_main(["executions", "audit", "--ledger", str(source), "--json"]) == 0
    audited = json.loads(capsys.readouterr().out)
    facts = audited["validation"]["facts"]
    assert facts["profile_status"] == "known_stale" and facts["semantics"]["fresh"] == 1
    text = history_maintenance.as_text(audited)
    assert "known_stale" in text and facts["retained_profile"]["digest"] in text
    assert facts["semantics"]["reused"] == 0 and files(tmp_path) == before
    with pytest.raises(store.StoreError, match=r"profile|activat|refresh"):
        executions.register(source, request)
    assert files(tmp_path) == before
    output = tmp_path / "refreshed"
    assert exacly_main(args(source, output)) == 0
    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["profile"]["digest"] == current["digest"]
    target = output / "operational" / "ledger.sqlite3"
    registered = executions.register(target, request)
    assert store.get(target, registered["id"])["active"] is True
    assert source.read_bytes() == (output / "raw" / "ledger.sqlite3").read_bytes()
    assert {name: content for name, content in files(tmp_path).items() if not name.startswith("refreshed/")} == before


@pytest.mark.parametrize("kind", ["baseline", "profile"])
def test_activation_cannot_reseal_corrupt_baselines_or_unknown_profiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    case = prepare_scenario(tmp_path)
    case.complete()
    source = operational(case.ledger, tmp_path / "source-v4")
    with sqlite3.connect(source) as connection:
        if kind == "baseline":
            identity, report = connection.execute("SELECT id,report FROM completion_intents").fetchone()
            connection.execute("UPDATE completion_intents SET report=? WHERE id=?", (report + b"\n", identity))
        else:
            retained = json.loads(connection.execute("SELECT profile_json FROM history_profile").fetchone()[0])
            retained["family"] = "unknown-future-validator"
            retained["digest"] = planning.digest({key: value for key, value in retained.items() if key != "digest"})
            connection.execute("UPDATE history_profile SET profile_json=?", (store.canonical(retained),))
    before = files(tmp_path)
    output = tmp_path / "must-not-be-created"
    assert exacly_main(args(source, output)) == 2
    captured = capsys.readouterr()
    assert "exacly executions:" in captured.err and not captured.out and not output.exists()
    assert files(tmp_path) == before


@pytest.mark.parametrize("published", [False, True])
def test_stale_prepared_v4_can_be_copy_refreshed_then_explicitly_recovered_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], published: bool
) -> None:
    case = prepare_scenario(tmp_path)
    case.ledger = operational(case.ledger, tmp_path / "operational-source")
    publish = state_writer.publish_locked

    def interrupted(*a: Any, **kw: Any) -> None:
        if published:
            publish(*a, **kw)
        raise OSError("controlled interruption at state publication")

    with (
        patch.object(state_writer, "publish_locked", interrupted),
        pytest.raises(completion.CompletionInterruptedError),
    ):
        case.complete()
    with sqlite3.connect(case.ledger) as connection:
        identity, phase = connection.execute("SELECT id,phase FROM completion_intents").fetchone()
        assert phase == "prepared"
    before, state_bytes = files(tmp_path), case.state_path.read_bytes()
    changed_profile(monkeypatch)
    output = tmp_path / "refreshed-prepared"
    assert exacly_main(args(case.ledger, output)) == 0
    activated = json.loads(capsys.readouterr().out)
    assert activated["validation"]["semantics"]["prepared"] == 1
    assert case.state_path.read_bytes() == state_bytes
    assert {
        name: content for name, content in files(tmp_path).items() if not name.startswith("refreshed-prepared/")
    } == before
    selected = output / "operational" / "ledger.sqlite3"
    command = [
        "completions",
        "recover",
        "--ledger",
        str(selected),
        "--completion",
        identity,
        "--state",
        str(case.state_path),
        "--json",
    ]
    assert exacly_main(command) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == ("completed" if published else "aborted")
    after = files(tmp_path)
    assert exacly_main(command) == 0
    assert json.loads(capsys.readouterr().out)["status"] == recovered["status"] and files(tmp_path) == after
    assert case.ledger.read_bytes() == (output / "raw" / "ledger.sqlite3").read_bytes()


def test_activation_real_directory_lock_timeout_preserves_source_and_creates_no_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = prepare_scenario(tmp_path)
    before = files(tmp_path)
    output = tmp_path / "timed-out"
    with process_lock(case.ledger, "directory"):
        started = time.monotonic()
        assert exacly_main([*args(case.ledger, output), "--lock-timeout", "0.05"]) == 2
        assert time.monotonic() - started < 1.0
    captured = capsys.readouterr()
    assert "lock timed out" in captured.err and "--lock-timeout" in captured.err and not captured.out
    assert files(tmp_path) == before and not output.exists()


def test_v4_maintenance_cli_uses_full_fresh_schema_and_rejects_malformed_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = prepare_scenario(tmp_path)
    case.complete()
    source = operational(case.ledger, tmp_path / "operational")
    command = ["executions", "inspect", "--ledger", str(source), "--json"]
    assert exacly_main(command) == 0
    report = json.loads(capsys.readouterr().out)
    jsonschema.validate(report, maintenance.SCHEMA_V4)
    assert report["schema_version"] == "0.3.0" and report["validation"]["facts"]["semantics"]["reused"] == 0
    before = files(tmp_path)
    for broken in (
        {**report, "validation": {"status": "passed"}},
        {**report, "schema_version": "0.2.0"},
        {**report, "schema_version": "future"},
        {**report, "store": {**report["store"], "user_version": 2}},
    ):
        monkeypatch.setattr(maintenance, "inspect", lambda *a, result=broken, **kw: result)
        assert exacly_main(command) == 2
        captured = capsys.readouterr()
        assert "invalid report" in captured.err and not captured.out and files(tmp_path) == before
