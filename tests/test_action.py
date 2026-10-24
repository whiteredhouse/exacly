"""Checks the GitHub Action other repositories use (action.yml), and runs its validate and plan scripts as the
runner would, on a model in a temporary Git repository.

Run with `mise run test`, or from the repository root: uv run pytest tests
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION: dict[str, Any] = yaml.safe_load((ROOT / "action.yml").read_text())
STEPS: dict[str, Any] = {step["name"]: step for step in ACTION["runs"]["steps"]}
EXAMPLE = ROOT / "spec" / "examples" / "0.2" / "invoice-reminders"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True
    )


def run(step: str, repo: Path, temp: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """One step's script, with the environment the runner gives it."""
    summary = temp / "summary.md"
    environment = {
        **os.environ,
        "ACTION_PATH": str(ROOT),
        "RUNNER_TEMP": str(temp),
        "GITHUB_STEP_SUMMARY": str(summary),
        **env,
    }
    return subprocess.run(["bash", "-c", STEPS[step]["run"]], cwd=repo, env=environment, capture_output=True, text=True)


def consumer(folder: Path) -> Path:
    """A repository with the example model on main, pushed to an origin, and a branch that changes it."""
    origin, repo = folder / "origin.git", folder / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    shutil.copytree(EXAMPLE, repo / "model")
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "model")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", "change")
    billing = repo / "model" / "billing.yaml"
    billing.write_text(billing.read_text().replace("name: Remind before due date", "name: Remind ahead", 1))
    return repo


def test_inputs_and_modes() -> None:
    assert ACTION["runs"]["using"] == "composite"
    assert set(ACTION["inputs"]) == {"mode", "model", "base", "token"}
    assert ACTION["inputs"]["mode"]["default"] == "check"
    assert STEPS["Validate the model"]["if"] == "inputs.mode == 'check'"
    assert STEPS["Write the plan"]["if"] == "inputs.mode == 'plan'"
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in (STEPS["Comment the plan, replacing the earlier one"]["if"])
    )


def test_inputs_reach_scripts_only_through_the_environment() -> None:
    for step in ACTION["runs"]["steps"]:
        assert "${{" not in step["run"], step["name"]
        assert step["shell"] == "bash" and step["run"].startswith("set -euo pipefail\n")


def test_the_validator_is_installed_from_the_lock_without_development_tools() -> None:
    script = STEPS["Install the validator"]["run"]
    assert 'uv sync --project "$ACTION_PATH" --locked --no-dev' in script
    assert "check | plan" in script


def test_validating_a_model_reports_to_the_summary_and_fails_when_it_does_not_conform() -> None:
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        repo = consumer(temp)
        done = run("Validate the model", repo, temp, MODEL="model")
        assert done.returncode == 0, done.stderr
        assert "conforms" in done.stdout
        assert (temp / "summary.md").read_text().startswith("### Exacly validation\n")
        (repo / "model" / "billing.yaml").write_text("exacly: 0.2.0\nrecords: [{id: OUT-001}]\n")
        assert run("Validate the model", repo, temp, MODEL="model").returncode == 1


def test_the_plan_is_written_against_the_base_branch_even_when_it_was_not_fetched() -> None:
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        repo = consumer(temp)
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")
        done = run("Write the plan", repo, temp, MODEL="model\n", BASE="main")
        assert done.returncode == 0, done.stderr
        plan = (temp / "exacly-plan.md").read_text()
        assert plan.startswith("<!-- exacly-plan -->")
        assert "Against `origin/main`" in plan and "`REQ-001`" in plan
        assert (temp / "summary.md").read_text() == plan
        assert run("Write the plan", repo, temp, MODEL="model", BASE="").returncode == 1
