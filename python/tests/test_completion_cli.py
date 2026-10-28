"""Explicit local completion uses live evidence; replay and recovery stay offline."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from exacly.cli import exacly_main
from test_executions_cli import (
    ACTION,
    BINDING_ID,
    PLAN,
    POLICY,
    TARGET,
    TOKEN,
    TRUST,
    preview_args,
    replace_arg,
)

if TYPE_CHECKING:
    from test_completion_faults import FaultScenario

COMPLETION_ID = "3" * 32
STATUSES = ("completed", "pending", "stale", "prepared", "aborted", "conflict")


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from exacly import github_evidence

    fake: dict[str, Any] = {
        "complete": [], "show": [], "recover": [], "upgrade": [], "tokens": [], "reads": [],
        "report": {"status": "completed", "completion_id": COMPLETION_ID},
        "live": True,
    }  # fmt: skip
    module = ModuleType("exacly.completion")
    maintenance = ModuleType("exacly.execution_maintenance")

    class Client:
        def __init__(self, token: str) -> None:
            fake["tokens"].append(token)

        def get_json(self, path: str) -> dict[str, Any]:
            fake["reads"].append(("json", path))
            return {"live": True}

        def download_artifact(self, repository: str, artifact_id: int) -> bytes:
            fake["reads"].append(("artifact", repository, artifact_id))
            return b"retained evidence"

    def complete(*args: Any, **kwargs: Any) -> Any:
        fake["complete"].append((args, kwargs))
        if fake["live"]:
            assert args[0].get_json("/repos/acme/demo/actions/runs/123") == {"live": True}
            assert args[0].download_artifact("acme/demo", 999) == b"retained evidence"
        return fake["report"]

    def show(*args: Any, **kwargs: Any) -> Any:
        fake["show"].append((args, kwargs))
        return fake["report"]

    def recover(*args: Any, **kwargs: Any) -> Any:
        fake["recover"].append((args, kwargs))
        return fake["report"]

    def upgrade(*args: Any, **kwargs: Any) -> dict[str, Any]:
        fake["upgrade"].append((args, kwargs))
        return {"operation": "upgrade", "status": "upgraded", "source_unchanged": True}

    for name, operation in (("complete", complete), ("show", show), ("recover", recover)):
        monkeypatch.setattr(module, name, operation, raising=False)
    monkeypatch.setattr(module, "as_text", lambda report: f"Local completion: {report['status']}", raising=False)
    monkeypatch.setitem(sys.modules, "exacly.completion", module)
    monkeypatch.setitem(sys.modules, "exacly.execution_maintenance", maintenance)
    import exacly

    monkeypatch.setattr(exacly, "completion", module, raising=False)
    monkeypatch.setattr(exacly, "execution_maintenance", maintenance, raising=False)
    monkeypatch.setattr(github_evidence, "GitHubClient", Client)
    monkeypatch.setattr(maintenance, "upgrade", upgrade, raising=False)
    monkeypatch.setattr(maintenance, "as_text", lambda report: "Ledger upgraded", raising=False)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return fake


def invocation(tmp_path: Path) -> tuple[list[str], dict[str, Path], Path]:
    args, inputs, ledger = preview_args(tmp_path)
    args[0] = "complete"
    args.remove("--dry-run")
    return args, inputs, ledger


def retained(operation: str, ledger: Path, state: Path | None = None) -> list[str]:
    args = ["completions", operation, "--ledger", str(ledger), "--completion", COMPLETION_ID]
    if state is not None:
        args.extend(["--state", str(state)])
    return args


@pytest.mark.parametrize("status,code", [(status, 0 if status == "completed" else 1) for status in STATUSES])
def test_complete_routes_exact_selection_and_status_without_an_extra_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], status: str, code: int
) -> None:
    args, inputs, ledger = invocation(tmp_path)
    before = {path: path.read_bytes() for path in [*inputs.values(), ledger]}
    backend["report"]["status"] = status
    assert exacly_main([*args, "--json"]) == code
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["report"] and not captured.err
    (source, plan, action, policy, trust, target, run, attempt, date), kwargs = backend["complete"][0]
    assert source is not None
    assert (plan, action, policy, trust, target.data) == (PLAN, ACTION, POLICY, TRUST, TARGET)
    assert (run, attempt, date) == (123, 2, dt.date(2026, 10, 28))
    assert kwargs == {"ledger": ledger, "binding_id": BINDING_ID, "state_path": inputs["state"]}
    assert backend["tokens"] == [TOKEN] and len(backend["reads"]) == 2
    assert all(path.read_bytes() == content for path, content in before.items())


def test_completed_replay_does_not_require_credentials_or_construct_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any]
) -> None:
    args, _, _ = invocation(tmp_path)
    backend["live"] = False
    monkeypatch.delenv("GH_TOKEN")
    assert exacly_main(args) == 0
    assert exacly_main(args) == 0
    assert len(backend["complete"]) == 2 and not backend["tokens"] and not backend["reads"]


@pytest.mark.parametrize("primary", [None, "", " \t"])
def test_live_client_uses_fallback_token_once_for_json_and_artifact_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any], primary: str | None
) -> None:
    args, _, _ = invocation(tmp_path)
    if primary is None:
        monkeypatch.delenv("GH_TOKEN")
    else:
        monkeypatch.setenv("GH_TOKEN", primary)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    assert exacly_main(args) == 0
    assert backend["tokens"] == ["fallback-token"]


def test_missing_live_credentials_are_an_operational_error_without_report_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, _ = invocation(tmp_path)
    monkeypatch.delenv("GH_TOKEN")
    output = tmp_path / "reports" / "result.json"
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "GH_TOKEN or GITHUB_TOKEN" in captured.err and not captured.out
    assert not backend["tokens"] and not output.parent.exists()


def test_invalid_live_credentials_are_redacted_before_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    from exacly import github_evidence

    args, _, _ = invocation(tmp_path)

    def invalid(token: str) -> Any:
        raise github_evidence.GitHubError(f"invalid credential {token}")

    monkeypatch.setattr(github_evidence, "GitHubClient", invalid)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "credentials are invalid" in captured.err and TOKEN not in captured.err and not captured.out


def test_error_after_live_source_access_is_redacted_and_does_not_save_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    import exacly.completion as completion

    args, _, _ = invocation(tmp_path)
    output = tmp_path / "reports" / "result.json"

    def fail(source: Any, *args: Any, **kwargs: Any) -> Any:
        source.get_json("/repos/acme/demo/actions/runs/123")
        raise ValueError(f"invalid plan {TOKEN}")

    monkeypatch.setattr(completion, "complete", fail)
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "invalid plan" in captured.err and TOKEN not in captured.err and not captured.out
    assert not output.parent.exists()


@pytest.mark.parametrize("human", [False, True])
def test_optional_report_uses_the_same_result_as_stdout_and_preserves_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    human: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, inputs, ledger = invocation(tmp_path)
    before = {path: path.read_bytes() for path in [*inputs.values(), ledger]}
    output = tmp_path / "reports" / "result.json"
    assert exacly_main([*args, "--out", str(output), *([] if human else ["--json"])]) == 0
    captured = capsys.readouterr()
    assert json.loads(output.read_text()) == backend["report"]
    assert captured.out == ("Local completion: completed\n" if human else output.read_text())
    assert not captured.err and all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize(
    "flag",
    [
        "--plan",
        "--action",
        "--policy",
        "--trust-policy",
        "--target",
        "--run",
        "--attempt",
        "--date",
        "--ledger",
        "--binding",
        "--state",
    ],
)
def test_every_completion_selection_is_required(tmp_path: Path, backend: dict[str, Any], flag: str) -> None:
    args, _, _ = invocation(tmp_path)
    index = args.index(flag)
    del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2 and not backend["complete"] and not backend["tokens"]


@pytest.mark.parametrize("flag", ["--receipt", "--archive", "--metadata", "--token", "--state-doc", "--dry-run"])
def test_imported_evidence_and_implicit_state_documents_are_not_completion_inputs(
    tmp_path: Path, backend: dict[str, Any], flag: str
) -> None:
    args, _, _ = invocation(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, flag, "untrusted.json"])
    assert error.value.code == 2 and not backend["complete"] and not backend["tokens"]


@pytest.mark.parametrize("which", ["plan", "policy", "trust", "target", "state", "ledger", "journal", "wal", "shm"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_report_cannot_replace_any_input_or_sqlite_auxiliary_alias_before_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
    alias: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, inputs, ledger = invocation(tmp_path)
    if which in {"journal", "wal", "shm"}:
        source = ledger.with_name(ledger.name + "-" + which)
        source.write_bytes(b"retained SQLite auxiliary")
    else:
        source = ledger if which == "ledger" else inputs[which]
    before = source.read_bytes()
    output = tmp_path / "report-alias.json"
    if alias == "direct":
        output = source
    elif alias == "symlink":
        output.symlink_to(source)
    else:
        os.link(source, output)
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "cannot overwrite" in capsys.readouterr().err
    assert not backend["complete"] and not backend["tokens"] and source.read_bytes() == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_report_cannot_create_an_absent_sidecar_or_a_child_inside_it(
    tmp_path: Path, backend: dict[str, Any], suffix: str
) -> None:
    args, _, ledger = invocation(tmp_path)
    sidecar = ledger.with_name(ledger.name + suffix)
    for output in (sidecar, sidecar / "report.json"):
        assert exacly_main([*args, "--out", str(output)]) == 2
    assert not sidecar.exists() and not backend["complete"] and not backend["tokens"]


@pytest.mark.parametrize("existing", [False, True])
def test_report_cannot_enter_declared_model_parent_even_if_missing_or_aliased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any], existing: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _ = invocation(tmp_path)
    parent = tmp_path / "models"
    if existing:
        parent.mkdir()
        alias = tmp_path / "models-alias"
        alias.symlink_to(parent, target_is_directory=True)
        output = alias / "result.json"
    else:
        output = parent / "new" / "result.json"
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert not backend["complete"] and not backend["tokens"] and not output.exists()


def test_report_has_no_hardcoded_default_model_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _ = invocation(tmp_path)
    output = tmp_path / "spec" / "exacly" / "result.json"
    assert exacly_main([*args, "--out", str(output)]) == 0
    assert output.exists()


@pytest.mark.parametrize("kind", ["directory", "parent_file", "loop"])
def test_bad_report_paths_are_rejected_before_core_or_client_calls(
    tmp_path: Path, backend: dict[str, Any], kind: str
) -> None:
    args, _, _ = invocation(tmp_path)
    path = tmp_path / "blocked"
    if kind == "directory":
        path.mkdir()
        output = path
    elif kind == "parent_file":
        path.write_bytes(b"preserve")
        output = path / "result.json"
    else:
        path.symlink_to(path.name, target_is_directory=True)
        output = path / "result.json"
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert not backend["complete"] and not backend["tokens"]


@pytest.mark.parametrize("which", ["plan", "policy", "trust", "target"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_state_destination_cannot_replace_other_operator_inputs(
    tmp_path: Path, backend: dict[str, Any], which: str, alias: str
) -> None:
    args, inputs, _ = invocation(tmp_path)
    state = tmp_path / "state-alias.json"
    source = inputs[which]
    if alias == "direct":
        state = source
    elif alias == "symlink":
        state.symlink_to(source)
    else:
        os.link(source, state)
    replace_arg(args, "--state", state)
    before = source.read_bytes()
    assert exacly_main(args) == 2
    assert not backend["complete"] and not backend["tokens"] and source.read_bytes() == before


@pytest.mark.parametrize("operation", ["show", "recover"])
@pytest.mark.parametrize("status", STATUSES)
def test_show_and_recovery_are_offline_with_resolved_and_unresolved_status_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    operation: str,
    status: str,
) -> None:
    _, inputs, ledger = invocation(tmp_path)
    monkeypatch.delenv("GH_TOKEN")
    backend["report"]["status"] = status
    state = inputs["state"] if operation == "recover" else None
    expected = 0 if operation == "show" or status in {"completed", "aborted"} else 1
    assert exacly_main([*retained(operation, ledger, state), "--json"]) == expected
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["report"] and not captured.err
    assert backend[operation] == [((ledger, COMPLETION_ID), {"state_path": state} if state else {})]
    assert not backend["tokens"] and not backend["reads"] and not backend["complete"]


@pytest.mark.parametrize("operation", ["complete", "show", "recover", "upgrade"])
@pytest.mark.parametrize("seconds", ["0", "0.25", "7"])
def test_shared_lock_budget_is_forwarded_only_when_explicit(
    tmp_path: Path, backend: dict[str, Any], operation: str, seconds: str
) -> None:
    args, inputs, ledger = invocation(tmp_path)
    if operation in {"show", "recover"}:
        args = retained(operation, ledger, inputs["state"] if operation == "recover" else None)
    elif operation == "upgrade":
        args = ["executions", "upgrade", "--ledger", str(ledger), "--out", str(tmp_path / "copy")]
    assert exacly_main([*args, "--lock-timeout", seconds]) == 0
    assert backend[operation][0][1]["lock_timeout"] == float(seconds)


@pytest.mark.parametrize("operation", ["complete", "show", "recover", "upgrade"])
@pytest.mark.parametrize("seconds", ["-1", "nan", "inf", "-inf", "invalid"])
def test_invalid_lock_budget_fails_before_any_backend_operation(
    tmp_path: Path, backend: dict[str, Any], operation: str, seconds: str
) -> None:
    args, inputs, ledger = invocation(tmp_path)
    if operation in {"show", "recover"}:
        args = retained(operation, ledger, inputs["state"] if operation == "recover" else None)
    elif operation == "upgrade":
        args = ["executions", "upgrade", "--ledger", str(ledger), "--out", str(tmp_path / "copy")]
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, f"--lock-timeout={seconds}"])
    assert error.value.code == 2 and not any(
        backend[key] for key in ("complete", "show", "recover", "upgrade", "tokens")
    )


@pytest.mark.parametrize(
    "operation,flag",
    [
        ("show", "--ledger"),
        ("show", "--completion"),
        ("recover", "--ledger"),
        ("recover", "--completion"),
        ("recover", "--state"),
    ],
)
def test_retained_commands_require_their_exact_local_selection(
    tmp_path: Path, backend: dict[str, Any], operation: str, flag: str
) -> None:
    _, inputs, ledger = invocation(tmp_path)
    args = retained(operation, ledger, inputs["state"] if operation == "recover" else None)
    index = args.index(flag)
    del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2 and not backend[operation]


@pytest.mark.parametrize("operation", ["show", "recover"])
def test_retained_receipts_do_not_accept_imported_json_or_output_files(
    tmp_path: Path, backend: dict[str, Any], operation: str
) -> None:
    _, inputs, ledger = invocation(tmp_path)
    args = retained(operation, ledger, inputs["state"] if operation == "recover" else None)
    for flag in ("--receipt", "--out"):
        with pytest.raises(SystemExit) as error:
            exacly_main([*args, flag, str(tmp_path / "injected.json")])
        assert error.value.code == 2 and not backend[operation]


@pytest.mark.parametrize("operation", ["complete", "show", "recover"])
@pytest.mark.parametrize("failure", [ValueError("ledger is busy or requires recovery"), OSError("file unavailable")])
def test_operational_failures_are_clean_errors_without_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    operation: str,
    failure: Exception,
) -> None:
    import exacly.completion as completion

    args, inputs, ledger = invocation(tmp_path)
    if operation != "complete":
        args = retained(operation, ledger, inputs["state"] if operation == "recover" else None)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(completion, operation, fail)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert str(failure) in captured.err and not captured.out and "Traceback" not in captured.err


@pytest.mark.parametrize("operation", ["complete", "show", "recover"])
def test_unknown_backend_status_is_an_operational_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], operation: str
) -> None:
    args, inputs, ledger = invocation(tmp_path)
    if operation != "complete":
        args = retained(operation, ledger, inputs["state"] if operation == "recover" else None)
    backend["report"]["status"] = "unexpected"
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "invalid status" in captured.err and not captured.out


@pytest.mark.parametrize("kind", ["new", "existing", "symlink", "sidecar", "sidecar_child"])
def test_upgrade_reuses_copy_guards_and_never_changes_the_selected_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], kind: str
) -> None:
    _, _, ledger = invocation(tmp_path)
    before = ledger.read_bytes()
    output = tmp_path / "upgraded"
    if kind == "existing":
        output.mkdir()
    elif kind == "symlink":
        output.symlink_to(tmp_path / "absent", target_is_directory=True)
    elif kind == "sidecar":
        output = ledger.with_name(ledger.name + "-journal")
    elif kind == "sidecar_child":
        output = ledger.with_name(ledger.name + "-wal") / "nested"
    args = ["executions", "upgrade", "--ledger", str(ledger), "--out", str(output), "--json"]
    assert exacly_main(args) == (0 if kind == "new" else 2)
    captured = capsys.readouterr()
    assert ledger.read_bytes() == before
    if kind == "new":
        assert json.loads(captured.out)["status"] == "upgraded" and not captured.err
        assert backend["upgrade"] == [((ledger, output), {})]
    else:
        assert not backend["upgrade"] and not captured.out


@pytest.mark.parametrize("document", [None, {}, {"model": {}}, {"model": {"files": []}}, {"model": {"files": [1]}}])
def test_invalid_trust_model_declarations_fail_cleanly_before_completion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], document: Any
) -> None:
    args, inputs, _ = invocation(tmp_path)
    inputs["trust"].write_text(json.dumps(document))
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "declare model files" in captured.err and not captured.out
    assert not backend["complete"] and not backend["tokens"]


def test_command_help_does_not_import_the_optional_completion_storage_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "exacly.completion", None)
    monkeypatch.setitem(sys.modules, "exacly.completion_store", None)
    with pytest.raises(SystemExit) as error:
        exacly_main(["--help"])
    assert error.value.code == 0
    assert "complete" in capsys.readouterr().out


def real_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], FaultScenario, list[str]]:
    from exacly import github_evidence
    from test_admission import ATTEMPT, READINESS, RUN_ID, Source
    from test_applicability import WHEN
    from test_completion_faults import prepare_case
    from test_reconciliation import selected

    case = prepare_case(tmp_path)
    documents = {
        "plan": case.plan, "policy": READINESS, "trust": case.source.policy, "target": selected().data,
    }  # fmt: skip
    inputs = {name: tmp_path / f"{name}.json" for name in documents}
    for name, path in inputs.items():
        path.write_text(json.dumps(documents[name]))
    tokens: list[str] = []

    def client(token: str) -> Source:
        tokens.append(token)
        return case.source

    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    args = [
        "complete", "github-actions", "--plan", str(inputs["plan"]), "--action", case.plan["actions"][0]["id"],
        "--policy", str(inputs["policy"]), "--trust-policy", str(inputs["trust"]), "--target", str(inputs["target"]),
        "--run", str(RUN_ID), "--attempt", str(ATTEMPT), "--date", WHEN.isoformat(),
        "--ledger", str(case.ledger), "--binding", case.binding["id"], "--state", str(case.state_path),
        "--lock-timeout", "0", "--json",
    ]  # fmt: skip
    return args, case, tokens


def test_real_completion_retains_a_receipt_and_replays_offline_without_another_state_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_completion_faults import remaining_work

    args, case, tokens = real_invocation(tmp_path, monkeypatch)
    previous = json.loads(case.state_path.read_bytes())
    assert exacly_main(args) == 0
    completed = json.loads(capsys.readouterr().out)
    identity = completed["completion_id"]
    assert completed["status"] == "completed" and completed["receipt"]["id"] == identity
    after = case.state_path.read_bytes()
    current = json.loads(after)
    assert current["serial"] == previous["serial"] + 1
    remaining_work(current, previous)
    monkeypatch.delenv("GH_TOKEN")
    assert exacly_main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["receipt"] == completed["receipt"] and case.state_path.read_bytes() == after
    assert tokens == [TOKEN]
    for operation in ("show", "recover"):
        selected = retained(operation, case.ledger, case.state_path if operation == "recover" else None)
        replace_arg(selected, "--completion", identity)
        assert exacly_main([*selected, "--json", "--lock-timeout", "0"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "completed" and result["receipt"] == completed["receipt"]
        assert case.state_path.read_bytes() == after and tokens == [TOKEN]


@pytest.mark.parametrize("status", ["pending", "stale"])
def test_real_unfulfilled_completion_does_not_change_state_or_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str
) -> None:
    from test_reconciliation import change_live_outcome

    args, case, tokens = real_invocation(tmp_path, monkeypatch)
    if status == "pending":
        change_live_outcome(case.source, "failure")
    else:
        state = json.loads(case.state_path.read_bytes())
        state["pending"] = []
        case.state_path.write_text(json.dumps(state))
    before = {path: path.read_bytes() for path in (case.state_path, case.ledger)}
    assert exacly_main(args) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == status and report["receipt"] is None and not captured.err
    assert all(path.read_bytes() == content for path, content in before.items())
    assert tokens == ([TOKEN] if status == "pending" else [])


@pytest.mark.parametrize(
    "phase,expected", [("after_prepare", "aborted"), ("after_replace", "completed"), ("conflict", "conflict")]
)
def test_real_prepared_completion_recovers_before_after_or_preserves_a_third_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], phase: str, expected: str
) -> None:
    from test_completion_faults import kill_at

    _, case, tokens = real_invocation(tmp_path, monkeypatch)
    original = case.state_path.read_bytes()
    identity = kill_at(case, "after_prepare" if phase == "conflict" else phase)
    if phase == "conflict":
        changed = json.loads(case.state_path.read_bytes())
        changed["serial"] += 1
        case.state_path.write_text(json.dumps(changed))
    retained_state = case.state_path.read_bytes()
    monkeypatch.delenv("GH_TOKEN")
    args = retained("recover", case.ledger, case.state_path)
    replace_arg(args, "--completion", identity)
    assert exacly_main([*args, "--json", "--lock-timeout", "0"]) == (1 if expected == "conflict" else 0)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == expected and report["completion_id"] == identity
    assert case.state_path.read_bytes() == retained_state and not tokens
    if phase == "after_prepare":
        assert retained_state == original
    elif phase == "after_replace":
        assert json.loads(retained_state)["serial"] == json.loads(original)["serial"] + 1


def test_report_write_failure_after_real_completion_preserves_state_and_retained_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import cli

    args, case, tokens = real_invocation(tmp_path, monkeypatch)
    before_serial = json.loads(case.state_path.read_bytes())["serial"]
    output = tmp_path / "reports" / "completion.json"

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("report directory unavailable")

    monkeypatch.setattr(cli, "write_reconciliation", fail)
    assert exacly_main([*args, "--out", str(output)]) == 2
    failed = capsys.readouterr()
    assert "already recorded" in failed.err and "report could not be written" in failed.err
    assert "exacly completions show" in failed.err and not failed.out and not output.exists()
    after = case.state_path.read_bytes()
    assert json.loads(after)["serial"] == before_serial + 1
    monkeypatch.delenv("GH_TOKEN")
    assert exacly_main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    identity = replay["completion_id"]
    assert identity in failed.err and replay["receipt"]["id"] == identity
    assert case.state_path.read_bytes() == after and tokens == [TOKEN]
    args = retained("show", case.ledger)
    replace_arg(args, "--completion", identity)
    assert exacly_main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["receipt"] == replay["receipt"]


def test_real_upgrade_preserves_the_raw_v1_source_and_creates_a_separate_v2_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sqlite3

    from test_execution_maintenance import committed_history

    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    ledger = private / "legacy.sqlite3"
    committed_history(ledger)
    before = ledger.read_bytes()
    output = tmp_path / "upgrade-bundle"
    assert (
        exacly_main(
            ["executions", "upgrade", "--ledger", str(ledger), "--out", str(output), "--lock-timeout", "0", "--json"]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "upgraded" and ledger.read_bytes() == before
    assert (output / "raw" / "ledger.sqlite3").read_bytes() == before
    with sqlite3.connect(output / "upgraded" / "ledger.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
