"""Planning saves derived artifacts while preserving every input and model directory."""

import json
from pathlib import Path
from typing import Any

import pytest
from exacly.cli import exacly_main
from test_applicability import WHEN, documents, target

POLICY: dict[str, Any] = {
    "name": "verification",
    "version": "1",
    "requirements": [{"record": "REQ-1", "dimensions": ["verification"]}],
}


def setup(tmp_path: Path, docs: dict[str, Any] | None = None) -> tuple[list[str], Path, Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    for name, document in (documents() if docs is None else docs).items():
        (model / f"{name}.json").write_text(json.dumps(document))
    policy, selection = tmp_path / "policy.json", tmp_path / "target.json"
    policy.write_text(json.dumps(POLICY))
    selection.write_text(json.dumps(target().data))
    args = ["plan", "-m", str(model), "--policy", str(policy), "--target", str(selection), "--date", str(WHEN)]
    return args, model, policy, selection


def test_saved_json_equals_stdout_and_repeated_planning_preserves_every_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, _, _ = setup(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    output = tmp_path / "reports" / "plan.json"
    assert exacly_main([*args, "--json", "--out", str(output)]) == 0
    printed = capsys.readouterr().out
    assert output.read_text() == printed
    plan = json.loads(printed)
    assert plan["status"] == "satisfied"
    assert plan["actions"] == []
    assert plan["target"] == target().data
    assert plan["as_of"] == str(WHEN)
    assert exacly_main([*args, "--json", "--out", str(output)]) == 0
    assert capsys.readouterr().out == printed
    assert output.read_text() == printed
    assert all(path.read_bytes() == content for path, content in before.items())


def test_human_output_explains_work_without_using_readiness_gate_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][0].update(state="failed", reason="Invoice total is incorrect")
    args, _, _, _ = setup(tmp_path, docs)
    output = tmp_path / "reports" / "plan.json"
    assert exacly_main([*args, "--out", str(output)]) == 0
    shown = capsys.readouterr().out
    saved = json.loads(output.read_text())
    assert saved["status"] == "work_proposed"
    assert saved["actions"]
    assert "REQ-1" in shown
    assert "Invoice total is incorrect" in shown
    assert exacly_main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == saved


def test_nonconforming_model_returns_a_blocked_plan_without_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = documents()
    docs["config"]["relationships"] = [r for r in docs["config"]["relationships"] if r["type"] != "implements"]
    args, _, _, _ = setup(tmp_path, docs)
    assert exacly_main([*args, "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["status"] == "blocked"
    assert saved["actions"] == []


@pytest.mark.parametrize("date", [None, "2026-99-99"])
def test_explicit_valid_evaluation_date_is_required(tmp_path: Path, date: str | None) -> None:
    args, _, _, _ = setup(tmp_path)
    args = args[:-2]
    if date is not None:
        args += ["--date", date]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("bad_input", ["policy", "delivery", "target", "syntax", "missing"])
def test_invalid_inputs_exit_two_without_creating_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_input: str
) -> None:
    args, _, policy, selection = setup(tmp_path)
    if bad_input == "policy":
        policy.write_text("{}")
    elif bad_input == "delivery":
        policy.write_text(json.dumps({**POLICY, "requirements": [{"record": "REQ-1", "dimensions": ["delivery"]}]}))
    elif bad_input == "target":
        selection.write_text("{}")
    elif bad_input == "syntax":
        policy.write_text("[broken")
    else:
        policy.unlink()
    output = tmp_path / "not-created" / "plan.json"
    assert exacly_main([*args, "--out", str(output)]) == 2
    failure = capsys.readouterr()
    assert "exacly plan:" in failure.err
    assert not failure.out
    assert not output.parent.exists()


@pytest.mark.parametrize("which", ["config", "state", "policy", "target", "input_alias", "parent_alias", "nested"])
def test_output_cannot_overwrite_inputs_or_contaminate_model_folders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], which: str
) -> None:
    args, model, policy, selection = setup(tmp_path)
    paths = {"config": model / "config.json", "state": model / "state.json", "policy": policy, "target": selection}
    if which == "input_alias":
        output = tmp_path / "alias.json"
        output.symlink_to(policy)
    elif which == "parent_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(model, target_is_directory=True)
        output = alias / "plan.json"
    elif which == "nested":
        output = model / "reports" / "plan.json"
    else:
        output = paths[which]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "--out" in capsys.readouterr().err
    assert all(path.read_bytes() == content for path, content in before.items())
    if which in {"nested", "parent_alias"}:
        assert not output.exists()


def test_output_stays_outside_a_model_glob_prefix_even_when_suffix_is_not_selected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, model, _, _ = setup(tmp_path)
    for path in model.glob("*.json"):
        path.rename(path.with_suffix(".yaml"))
    args[2] = str(model / "*.yaml")
    output = model / "reports" / "plan.json"
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "glob prefixes" in capsys.readouterr().err
    assert not output.parent.exists()
    outside = tmp_path / "reports" / "plan.json"
    assert exacly_main([*args, "--out", str(outside)]) == 0
    assert json.loads(outside.read_text())["status"] == "satisfied"


def test_glob_traversing_symlink_cannot_write_into_real_model_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, model, _, _ = setup(tmp_path)
    aliases = tmp_path / "aliases"
    aliases.mkdir()
    (aliases / "selected").symlink_to(model, target_is_directory=True)
    args[2] = str(aliases / "*" / "*.json")
    output = model / "plan.json"
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "glob prefixes" in capsys.readouterr().err
    assert not output.exists()


def test_dangling_output_symlink_cannot_be_replaced_with_an_artifact_inside_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, model, _, _ = setup(tmp_path)
    outside = tmp_path / "outside.json"
    output = model / "plan.json"
    output.symlink_to(outside)
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "--out" in capsys.readouterr().err
    assert output.is_symlink()
    assert output.readlink() == outside
    assert not outside.exists()


def test_output_symlink_loop_is_an_output_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args, _, _, _ = setup(tmp_path)
    output = tmp_path / "plan.json"
    output.symlink_to(output.name)
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "--out" in capsys.readouterr().err
    assert output.is_symlink()


def test_explicit_model_files_allow_a_sibling_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args, model, _, _ = setup(tmp_path)
    args[2] = str(model / "config.json")
    args += ["-m", str(model / "state.json")]
    output = model / "plan.json"
    assert exacly_main([*args, "--out", str(output)]) == 0
    assert json.loads(output.read_text())["status"] == "satisfied"
    assert not capsys.readouterr().err


def test_output_error_has_no_traceback_and_preserves_existing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, _, _ = setup(tmp_path)
    output = tmp_path / "reports"
    output.mkdir()
    assert exacly_main([*args, "--out", str(output)]) == 2
    assert "exacly plan:" in capsys.readouterr().err
    assert output.is_dir()
    assert not list(output.iterdir())
