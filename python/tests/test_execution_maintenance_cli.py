"""Ledger maintenance is explicit, offline and disjoint from preserved source files."""

import json
from pathlib import Path
from typing import Any

import pytest
from exacly.cli import exacly_main
from test_executions_cli import BINDING_ID, TOKEN, preview_args, registration


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import exacly.execution_maintenance as maintenance
    import exacly.executions as executions
    from exacly import github_evidence, reconciliation

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake: dict[str, Any] = {
        "calls": [],
        "registers": [],
        "reads": [],
        "tokens": [],
        "previews": [],
        "inspection": {"status": "clean", "source_unchanged": True},
        "copy": {"status": "complete", "source_unchanged": True},
        "binding": {"id": BINDING_ID},
        "preview": {"status": "ready"},
        "source": object(),
    }

    def inspect(*args: Any, **kwargs: Any) -> Any:
        fake["calls"].append(("inspect", args, kwargs))
        return fake["inspection"]

    def backup(*args: Any, **kwargs: Any) -> Any:
        fake["calls"].append(("backup", args, kwargs))
        fake["copy"]["status"] = "backed_up"
        return fake["copy"]

    def recover(*args: Any, **kwargs: Any) -> Any:
        fake["calls"].append(("recover", args, kwargs))
        fake["copy"]["status"] = "recovered"
        return fake["copy"]

    def register(*args: Any, **kwargs: Any) -> Any:
        fake["registers"].append((args, kwargs))
        return fake["binding"]

    def get(*args: Any, **kwargs: Any) -> Any:
        fake["reads"].append((args, kwargs))
        return {"binding": fake["binding"], "active": True}

    def client(token: str) -> Any:
        fake["tokens"].append(token)
        return fake["source"]

    def preview(*args: Any, **kwargs: Any) -> Any:
        fake["previews"].append((args, kwargs))
        return fake["preview"]

    monkeypatch.setattr(maintenance, "inspect", inspect)
    monkeypatch.setattr(maintenance, "backup", backup)
    monkeypatch.setattr(maintenance, "recover", recover)
    monkeypatch.setattr(maintenance, "as_text", lambda report: f"Ledger maintenance: {report['status']}")
    monkeypatch.setattr(executions, "register", register)
    monkeypatch.setattr(executions, "get", get)
    monkeypatch.setattr(executions, "as_text", lambda report: "Execution binding")
    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    monkeypatch.setattr(reconciliation, "preview", preview)
    monkeypatch.setattr(reconciliation, "as_text", lambda report: f"Completion preview: {report['status']}")
    return fake


def source_files(tmp_path: Path) -> tuple[Path, dict[Path, bytes]]:
    ledger = tmp_path / "ledger.sqlite"
    files = {
        ledger: b"original ledger bytes",
        ledger.with_name(ledger.name + "-journal"): b"original rollback journal",
        ledger.with_name(ledger.name + "-wal"): b"original WAL bytes",
        ledger.with_name(ledger.name + "-shm"): b"original shared memory",
    }
    for path, value in files.items():
        path.write_bytes(value)
    return ledger, files


@pytest.mark.parametrize(
    "status,expected", [("clean", 0), ("recovery_required", 1), ("unsupported", 1), ("corrupt", 1)]
)
def test_inspect_reports_diagnosis_without_credentials_or_source_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], status: str, expected: int
) -> None:
    ledger, before = source_files(tmp_path)
    backend["inspection"]["status"] = status
    assert exacly_main(["executions", "inspect", "--ledger", str(ledger), "--json"]) == expected
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["inspection"] and not captured.err
    assert backend["calls"] == [("inspect", (ledger,), {})]
    assert not backend["tokens"] and not backend["registers"] and not backend["previews"]
    assert all(path.read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("operation", ["backup", "recover"])
def test_copy_operations_forward_a_new_output_without_rewriting_the_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], operation: str
) -> None:
    ledger, before = source_files(tmp_path)
    output = tmp_path / "preserved-copy"
    assert exacly_main(["executions", operation, "--ledger", str(ledger), "--out", str(output), "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["copy"] and not captured.err
    assert backend["calls"] == [(operation, (ledger, output), {})]
    assert not backend["tokens"] and not backend["registers"] and not backend["previews"]
    assert all(path.read_bytes() == value for path, value in before.items())


def test_human_maintenance_output_renders_the_same_diagnosis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    ledger, _ = source_files(tmp_path)
    assert exacly_main(["executions", "inspect", "--ledger", str(ledger)]) == 0
    assert capsys.readouterr().out == "Ledger maintenance: clean\n"


@pytest.mark.parametrize("operation", ["inspect", "backup", "recover"])
@pytest.mark.parametrize("seconds", ["0", "0.25", "7"])
def test_maintenance_lock_timeout_is_explicit_and_forwarded(
    tmp_path: Path, backend: dict[str, Any], operation: str, seconds: str
) -> None:
    ledger, _ = source_files(tmp_path)
    args = ["executions", operation, "--ledger", str(ledger), "--lock-timeout", seconds]
    if operation != "inspect":
        args.extend(["--out", str(tmp_path / "copy")])
    assert exacly_main(args) == 0
    assert backend["calls"][0][2] == {"lock_timeout": float(seconds)}


@pytest.mark.parametrize("operation", ["inspect", "backup", "recover", "register", "show", "reconcile"])
@pytest.mark.parametrize("seconds", ["-1", "nan", "inf", "-inf", "not-a-number"])
def test_invalid_lock_timeouts_fail_before_any_operation(
    tmp_path: Path, backend: dict[str, Any], operation: str, seconds: str
) -> None:
    if operation == "register":
        args, _, _ = registration(tmp_path)
    elif operation == "reconcile":
        args, _, _ = preview_args(tmp_path)
    else:
        ledger, _ = source_files(tmp_path)
        args = ["executions", operation, "--ledger", str(ledger)]
        if operation == "show":
            args.extend(["--binding", BINDING_ID])
        elif operation != "inspect":
            args.extend(["--out", str(tmp_path / "copy")])
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, f"--lock-timeout={seconds}"])
    assert error.value.code == 2
    assert not any(backend[key] for key in ("calls", "registers", "reads", "tokens", "previews"))


@pytest.mark.parametrize("operation", ["backup", "recover"])
@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_copy_never_replaces_an_existing_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], operation: str, kind: str
) -> None:
    ledger, before = source_files(tmp_path)
    output = tmp_path / "copy"
    if kind == "directory":
        output.mkdir()
        (output / "keep").write_bytes(b"preserve directory")
    elif kind == "file":
        output.write_bytes(b"preserve file")
    elif kind == "symlink":
        output.symlink_to(ledger)
    else:
        output.symlink_to(tmp_path / "absent")
    assert exacly_main(["executions", operation, "--ledger", str(ledger), "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "new directory" in captured.err and not captured.out
    assert not backend["calls"]
    assert all(path.read_bytes() == value for path, value in before.items())
    if kind == "directory":
        assert (output / "keep").read_bytes() == b"preserve directory"
    elif kind == "file":
        assert output.read_bytes() == b"preserve file"
    else:
        assert output.is_symlink()


@pytest.mark.parametrize("operation", ["backup", "recover"])
@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_output_cannot_overlap_source_or_even_currently_absent_sqlite_sidecars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], operation: str, suffix: str
) -> None:
    ledger = tmp_path / "ledger.sqlite"
    if suffix:
        ledger.write_bytes(b"source ledger")
    output = ledger.with_name(ledger.name + suffix)
    assert exacly_main(["executions", operation, "--ledger", str(ledger), "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "overlap" in captured.err and not captured.out
    assert not backend["calls"] and not output.exists()
    if suffix:
        assert ledger.read_bytes() == b"source ledger"


def test_copy_source_alias_cannot_make_a_sidecar_destination_look_unrelated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    operator = tmp_path / "operator"
    operator.mkdir()
    ledger = operator / "ledger.sqlite"
    ledger.write_bytes(b"source ledger")
    alias = tmp_path / "alias"
    alias.symlink_to(operator, target_is_directory=True)
    output = alias / "ledger.sqlite-wal"
    assert exacly_main(["executions", "recover", "--ledger", str(ledger), "--out", str(output)]) == 2
    assert "overlap" in capsys.readouterr().err
    assert not backend["calls"] and not output.exists()
    assert ledger.read_bytes() == b"source ledger"


def test_copy_path_loops_are_reported_without_core_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    ledger = tmp_path / "ledger.sqlite"
    ledger.write_bytes(b"source ledger")
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name, target_is_directory=True)
    assert exacly_main(["executions", "backup", "--ledger", str(ledger), "--out", str(loop / "copy")]) == 2
    captured = capsys.readouterr()
    assert "cannot be resolved" in captured.err and "Traceback" not in captured.err
    assert not backend["calls"]


@pytest.mark.parametrize("operation", ["inspect", "backup", "recover"])
@pytest.mark.parametrize("failure", [ValueError("unsupported ledger schema"), OSError("ledger is unavailable")])
def test_maintenance_errors_are_clean_and_do_not_create_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    operation: str,
    failure: Exception,
) -> None:
    import exacly.execution_maintenance as maintenance

    ledger, before = source_files(tmp_path)
    output = tmp_path / "copy"

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(maintenance, operation, fail)
    args = ["executions", operation, "--ledger", str(ledger)]
    if operation != "inspect":
        args.extend(["--out", str(output)])
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "exacly executions:" in captured.err and not captured.out
    assert "Traceback" not in captured.err and not output.exists()
    assert all(path.read_bytes() == value for path, value in before.items())


def test_invalid_inspection_status_is_not_presented_as_a_clean_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    ledger, _ = source_files(tmp_path)
    backend["inspection"]["status"] = "guessed"
    assert exacly_main(["executions", "inspect", "--ledger", str(ledger), "--json"]) == 2
    captured = capsys.readouterr()
    assert "invalid status" in captured.err and not captured.out


@pytest.mark.parametrize("operation", ["backup", "recover"])
def test_invalid_copy_status_is_not_presented_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    operation: str,
) -> None:
    import exacly.execution_maintenance as maintenance

    ledger, _ = source_files(tmp_path)
    monkeypatch.setattr(maintenance, operation, lambda *args, **kwargs: {"status": "corrupt"})
    assert exacly_main(["executions", operation, "--ledger", str(ledger), "--out", str(tmp_path / "copy")]) == 2
    captured = capsys.readouterr()
    assert "invalid status" in captured.err and not captured.out


@pytest.mark.parametrize("operation", ["backup", "recover"])
def test_copy_requires_an_explicit_destination(tmp_path: Path, backend: dict[str, Any], operation: str) -> None:
    ledger, _ = source_files(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main(["executions", operation, "--ledger", str(ledger)])
    assert error.value.code == 2 and not backend["calls"]


def test_inspect_accepts_no_recovery_or_replacement_flag(tmp_path: Path, backend: dict[str, Any]) -> None:
    ledger, _ = source_files(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main(["executions", "inspect", "--ledger", str(ledger), "--out", str(tmp_path / "copy")])
    assert error.value.code == 2 and not backend["calls"]


def test_existing_registration_forwards_only_an_explicit_lock_timeout(tmp_path: Path, backend: dict[str, Any]) -> None:
    args, _, _ = registration(tmp_path)
    assert exacly_main(args) == 0
    assert backend["registers"][0][1] == {}
    assert exacly_main([*args, "--lock-timeout", "0.5"]) == 0
    assert backend["registers"][1][1] == {"lock_timeout": 0.5}
    assert not backend["tokens"]


def test_existing_show_forwards_only_an_explicit_lock_timeout(tmp_path: Path, backend: dict[str, Any]) -> None:
    ledger, _ = source_files(tmp_path)
    args = ["executions", "show", "--ledger", str(ledger), "--binding", BINDING_ID]
    assert exacly_main(args) == 0 and backend["reads"][0][1] == {}
    assert exacly_main([*args, "--lock-timeout", "0"]) == 0
    assert backend["reads"][1][1] == {"lock_timeout": 0.0}
    assert not backend["tokens"]


def test_bound_preview_forwards_only_explicit_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any]
) -> None:
    args, _, ledger = preview_args(tmp_path)
    before = ledger.read_bytes()
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert exacly_main(args) == 0
    assert "lock_timeout" not in backend["previews"][0][1]
    assert exacly_main([*args, "--lock-timeout", "1.25"]) == 0
    assert backend["previews"][1][1]["lock_timeout"] == 1.25
    assert ledger.read_bytes() == before and not backend["registers"]


def test_unbound_preview_rejects_irrelevant_lock_timeout_before_credentials_or_network(
    tmp_path: Path, backend: dict[str, Any]
) -> None:
    args, _, _ = preview_args(tmp_path)
    for flag in ("--ledger", "--binding", "--state"):
        index = args.index(flag)
        del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, "--lock-timeout", "1"])
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["previews"]


def test_real_cli_inspection_and_backup_preserve_history_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import execution_store as store
    from test_execution_maintenance import committed_history, snapshot

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    ledger = operator / "executions.sqlite3"
    first, active = committed_history(ledger)
    before = snapshot(operator)
    assert exacly_main(["executions", "inspect", "--ledger", str(ledger), "--lock-timeout", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    output = tmp_path / "backup"
    assert (
        exacly_main(
            ["executions", "backup", "--ledger", str(ledger), "--out", str(output), "--lock-timeout", "0", "--json"]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "backed_up"
    assert json.loads((output / "manifest.json").read_text()) == report
    saved = output / "ledger.sqlite3"
    assert store.get(saved, active["id"])["active"] is True
    assert store.get(saved, first["id"])["superseded_by"] == active["id"]
    assert store.get(saved, active["id"])["state_watermark"]["serial"] == 5
    assert snapshot(operator) == before


def test_real_cli_recovery_preserves_a_killed_writer_source_and_validates_the_new_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import execution_store as store
    from test_execution_maintenance import hot_journal, snapshot

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    ledger = operator / "executions.sqlite3"
    _, active, incomplete = hot_journal(ledger)
    before = snapshot(operator)
    assert exacly_main(["executions", "inspect", "--ledger", str(ledger), "--lock-timeout", "0", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "recovery_required"
    output = tmp_path / "recovery"
    assert (
        exacly_main(
            ["executions", "recover", "--ledger", str(ledger), "--out", str(output), "--lock-timeout", "0", "--json"]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "recovered"
    assert json.loads((output / "manifest.json").read_text()) == report
    saved = output / "recovered" / "ledger.sqlite3"
    assert store.get(saved, active["id"])["active"] is True
    with pytest.raises(store.StoreError):
        store.get(saved, incomplete["id"])
    assert snapshot(operator) == before


def test_real_cli_diagnoses_invalid_domain_records_and_refuses_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import execution_store as store
    from test_execution_store import binding

    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    ledger = operator / "executions.sqlite3"
    # This satisfies the storage layer's record envelope but lacks a valid execution contract.
    store.put(ledger, binding())
    before = ledger.read_bytes()
    assert exacly_main(["executions", "inspect", "--ledger", str(ledger), "--json"]) == 1
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["status"] == "corrupt"
    output = tmp_path / "invalid-backup"
    assert exacly_main(["executions", "backup", "--ledger", str(ledger), "--out", str(output), "--json"]) == 2
    captured = capsys.readouterr()
    assert "exacly executions:" in captured.err and not captured.out
    assert not output.exists() and ledger.read_bytes() == before
