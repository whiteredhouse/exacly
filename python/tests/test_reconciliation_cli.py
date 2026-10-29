"""Completion previews require fresh evidence and never rewrite saved work or observations."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from exacly.cli import exacly_main

TOKEN = "test-reconciliation-token"
TARGET = {"context": "main", "model_revision": "a" * 40, "revision": "b" * 40}
TRUST_POLICY: dict[str, Any] = {"model": {"files": ["models/exacly.yaml"]}}
READINESS_POLICY = {
    "name": "verification",
    "version": "1",
    "requirements": [{"record": "REQ-1", "dimensions": ["verification"]}],
}
PLAN = {"digest": "saved-plan-digest", "actions": [{"id": "action-1"}]}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from exacly import github_evidence, reconciliation

    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake: dict[str, Any] = {
        "source": object(),
        "tokens": [],
        "calls": [],
        "report": {"status": "ready", "action": "action-1", "reason": "All approved verification checks passed"},
    }

    def client(token: str) -> Any:
        fake["tokens"].append(token)
        return fake["source"]

    def preview(*args: Any) -> Any:
        fake["calls"].append(args)
        return fake["report"]

    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    monkeypatch.setattr(reconciliation, "preview", preview)
    monkeypatch.setattr(reconciliation, "as_text", lambda report: f"Completion preview: {report['status']}")
    return fake


def invocation(tmp_path: Path) -> tuple[list[str], dict[str, Path], Path]:
    inputs = {"plan": tmp_path / "plan.json", "policy": tmp_path / "policy.json",
              "trust": tmp_path / "trust.json", "target": tmp_path / "target.json"}  # fmt: skip
    for key, document in (("plan", PLAN), ("policy", READINESS_POLICY), ("trust", TRUST_POLICY), ("target", TARGET)):
        inputs[key].write_text(json.dumps(document))
    args = [
        "reconcile", "github-actions", "--dry-run", "--plan", str(inputs["plan"]), "--action", "action-1",
        "--policy", str(inputs["policy"]), "--trust-policy", str(inputs["trust"]), "--target", str(inputs["target"]),
        "--run", "123", "--attempt", "2", "--date", "2026-10-28",
    ]  # fmt: skip
    return args, inputs, tmp_path / "reports" / "reconciliation.json"


@pytest.mark.parametrize(("status", "exit_code"), [("ready", 0), ("pending", 1), ("stale", 1)])
def test_preview_statuses_and_exact_inputs_are_forwarded_without_changing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], status: str, exit_code: int
) -> None:
    args, inputs, output = invocation(tmp_path)
    before = {path: path.read_bytes() for path in inputs.values()}
    backend["report"]["status"] = status
    assert exacly_main([*args, "--json"]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["report"]
    assert not captured.err
    assert backend["tokens"] == [TOKEN]
    source, plan, action, policy, trust, target, run, attempt, date = backend["calls"][0]
    assert source is backend["source"]
    assert (plan, action, policy, trust) == (PLAN, "action-1", READINESS_POLICY, TRUST_POLICY)
    assert target.data == TARGET
    assert (run, attempt, date) == (123, 2, dt.date(2026, 10, 28))
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not output.parent.exists()
    assert {path.name for path in tmp_path.iterdir()} == {path.name for path in inputs.values()}


def test_optional_saved_report_matches_json_stdout_and_repeated_preview_keeps_inputs_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, inputs, output = invocation(tmp_path)
    pending = tmp_path / "pending-state.json"
    pending.write_text("existing pending work remains untouched\n")
    before = {path: path.read_bytes() for path in [*inputs.values(), pending]}
    assert exacly_main([*args, "--json", "--out", str(output)]) == 0
    first = capsys.readouterr()
    assert output.read_text() == first.out
    assert json.loads(first.out) == backend["report"]
    assert not first.err
    assert exacly_main([*args, "--json", "--out", str(output)]) == 0
    assert capsys.readouterr().out == first.out
    assert output.read_text() == first.out
    assert len(backend["calls"]) == 2
    assert all(path.read_bytes() == content for path, content in before.items())
    assert list(output.parent.iterdir()) == [output]


def test_human_view_uses_same_preview_as_saved_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, output = invocation(tmp_path)
    backend["report"]["status"] = "pending"
    assert exacly_main([*args, "--out", str(output)]) == 1
    assert capsys.readouterr().out == "Completion preview: pending\n"
    assert json.loads(output.read_text()) == backend["report"]


@pytest.mark.parametrize("primary", [None, "", " \t"])
def test_token_fallback_matches_admission_behavior(
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


def test_primary_token_wins_and_neither_environment_token_is_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, _ = invocation(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    assert exacly_main(args) == 0
    captured = capsys.readouterr()
    assert backend["tokens"] == [TOKEN]
    assert TOKEN not in captured.out + captured.err
    assert "fallback-token" not in captured.out + captured.err


def test_missing_credentials_are_an_input_error_without_output_or_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, output = invocation(tmp_path)
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "GH_TOKEN or GITHUB_TOKEN" in captured.err
    assert not captured.out
    assert not backend["calls"] and not backend["tokens"]
    assert not output.parent.exists()


def test_invalid_client_credentials_do_not_expose_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    from exacly import github_evidence

    args, _, output = invocation(tmp_path)

    def invalid_client(token: str) -> Any:
        raise github_evidence.GitHubError(f"invalid credential {token}")

    monkeypatch.setattr(github_evidence, "GitHubClient", invalid_client)
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "credentials are invalid" in captured.err
    assert TOKEN not in captured.err
    assert not captured.out and not backend["calls"]
    assert not output.parent.exists()


def test_core_validation_error_is_redacted_before_display_and_does_not_save_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    from exacly import reconciliation

    args, _, output = invocation(tmp_path)

    def invalid_preview(*args: Any) -> Any:
        raise ValueError(f"invalid plan {TOKEN}")

    monkeypatch.setattr(reconciliation, "preview", invalid_preview)
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "invalid plan" in captured.err
    assert TOKEN not in captured.err
    assert not captured.out
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "field",
    ["--dry-run", "--plan", "--action", "--policy", "--trust-policy", "--target", "--run", "--attempt", "--date"],
)
def test_explicit_dry_run_and_all_evidence_selection_inputs_are_required(
    tmp_path: Path, backend: dict[str, Any], field: str
) -> None:
    args, _, output = invocation(tmp_path)
    index = args.index(field)
    del args[index : index + (1 if field == "--dry-run" else 2)]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["calls"]
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("--run", "0"),
        ("--run", "-1"),
        ("--run", "invalid"),
        ("--attempt", "0"),
        ("--attempt", "-1"),
        ("--attempt", "invalid"),
        ("--date", "2026-99-99"),
    ],
)
def test_positive_run_attempt_and_valid_date_are_checked_before_source_access(
    tmp_path: Path, backend: dict[str, Any], field: str, value: str
) -> None:
    args, _, _ = invocation(tmp_path)
    args[args.index(field) + 1] = value
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["calls"]


@pytest.mark.parametrize("flag", ["--receipt", "--state", "--metadata", "--archive", "--token", "--apply", "--close"])
def test_saved_evidence_and_mutation_options_are_not_accepted(
    tmp_path: Path, backend: dict[str, Any], flag: str
) -> None:
    args, _, _ = invocation(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, flag, "unsupported-input"])
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["calls"]


@pytest.mark.parametrize("which", ["plan", "policy", "trust", "target", "symlink_alias"])
def test_output_cannot_replace_an_input_or_its_symlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, inputs, _ = invocation(tmp_path)
    if which == "symlink_alias":
        output = tmp_path / "alias.json"
        output.symlink_to(inputs["plan"])
    else:
        output = inputs[which]
    before = {path: path.read_bytes() for path in inputs.values()}
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "--out" in capsys.readouterr().err
    assert all(path.read_bytes() == content for path, content in before.items())
    if which == "symlink_alias":
        assert output.is_symlink()


@pytest.mark.parametrize("which", ["nonexistent_parent", "symlink_parent", "dangling_leaf"])
def test_declared_model_folders_remain_protected_through_missing_paths_and_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _ = invocation(tmp_path)
    model = tmp_path / "models"
    outside = tmp_path / "outside.json"
    if which == "nonexistent_parent":
        output = model / "nested" / "preview.json"
    elif which == "symlink_parent":
        model.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(model, target_is_directory=True)
        output = alias / "preview.json"
    else:
        model.mkdir()
        output = model / "preview.json"
        output.symlink_to(outside)
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "declared model folders" in capsys.readouterr().err
    assert not outside.exists()
    if which == "nonexistent_parent":
        assert not model.exists()
    elif which == "dangling_leaf":
        assert output.is_symlink()
    else:
        assert not output.exists()


def test_custom_model_selection_does_not_reserve_an_unrelated_default_model_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _ = invocation(tmp_path)
    output = tmp_path / "spec" / "exacly" / "preview.json"
    assert exacly_main([*args, "--out", str(output)]) == 0
    assert json.loads(output.read_text()) == backend["report"]


@pytest.mark.parametrize("which", ["model", "output"])
def test_symlink_loops_return_a_clean_output_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, output = invocation(tmp_path)
    if which == "model":
        loop = tmp_path / "models"
        loop.symlink_to(loop.name, target_is_directory=True)
    else:
        output.parent.mkdir()
        output.symlink_to(output.name)
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "cannot be resolved" in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out
    assert not output.exists()


@pytest.mark.parametrize("which", ["plan", "policy", "trust", "target"])
def test_unreadable_local_inputs_never_contact_the_source_or_create_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], which: str
) -> None:
    args, inputs, output = invocation(tmp_path)
    inputs[which].write_text("[invalid")
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "exacly reconcile:" in capsys.readouterr().err
    assert not backend["tokens"] and not backend["calls"]
    assert not output.parent.exists()


def test_existing_output_directory_is_preserved_on_output_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, output = invocation(tmp_path)
    output.mkdir(parents=True)
    marker = output / "existing"
    marker.write_bytes(b"preserve this directory")
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "exacly reconcile:" in capsys.readouterr().err
    assert marker.read_bytes() == b"preserve this directory"
    assert list(output.parent.iterdir()) == [output]


def test_failed_atomic_save_keeps_previous_report_and_hides_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    import os

    monkeypatch.chdir(tmp_path)
    args, inputs, output = invocation(tmp_path)
    output.parent.mkdir()
    output.write_bytes(b"previous preview\n")
    before = {path: path.read_bytes() for path in [*inputs.values(), output]}

    def fail_replace(source: Any, destination: Any) -> None:
        raise OSError(f"output unavailable {TOKEN}")

    monkeypatch.setattr(os, "replace", fail_replace)
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "output unavailable" in captured.err
    assert TOKEN not in captured.err
    assert not captured.out
    assert all(path.read_bytes() == content for path, content in before.items())
    assert list(output.parent.iterdir()) == [output]


def test_invalid_preview_status_is_not_saved_or_reported_as_completion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, output = invocation(tmp_path)
    backend["report"]["status"] = "closed"
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "invalid status" in captured.err
    assert not captured.out
    assert not output.parent.exists()
