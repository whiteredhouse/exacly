"""Checks the GitHub Action other repositories use (action.yml), and runs its validate and plan scripts as the
runner would, on a model in a temporary Git repository.

Run with `mise run test`, or from the repository root: uv run pytest tests
"""

import json
import os
import shutil
import subprocess
import sys
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
    for step in STEPS.values():
        if "run" not in step:
            continue
        assert "${{" not in step["run"], step["name"]
        assert step["shell"] == "bash"
    for script in (ROOT / "scripts" / "action").glob("*.sh"):
        assert "${{" not in script.read_text()
        assert script.read_text().startswith("#!/usr/bin/env bash\nset -euo pipefail\n")


def test_the_validator_is_installed_from_the_lock_without_development_tools() -> None:
    script = (ROOT / "scripts" / "action" / "install.sh").read_text()
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
        patterns = run("Validate the model", repo, temp, MODEL="model/*.yaml")
        assert patterns.returncode == 0, patterns.stderr
        assert "Documents: model/billing.yaml, model/model.yaml" in patterns.stdout
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


def test_patterns_are_matched_against_each_revision() -> None:
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        repo = consumer(temp)
        (repo / "model" / "billing.yaml").unlink()
        by_pattern = run("Write the plan", repo, temp, MODEL="model/*.yaml", BASE="main")
        assert by_pattern.returncode == 0, by_pattern.stderr
        plan = (temp / "exacly-plan.md").read_text()
        assert run("Write the plan", repo, temp, MODEL="model", BASE="main").returncode == 0
        assert plan == (temp / "exacly-plan.md").read_text()
        assert "| - | `REQ-001` |" in plan


def test_an_unknown_mode_fails_before_installing_the_validator() -> None:
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        # Execute the real install script while pointing its project at a nonexistent checkout.
        done = subprocess.run(
            ["bash", str(ROOT / "scripts" / "action" / "install.sh")],
            cwd=temp,
            env={**os.environ, "MODE": "unknown", "ACTION_PATH": str(temp)},
            capture_output=True,
            text=True,
        )
        assert done.returncode == 1
        assert "mode is check or plan" in done.stdout


def test_installing_the_validator_does_not_install_development_tools() -> None:
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        venv = temp / "venv"
        done = run("Install the validator", temp, temp, MODE="check", UV_PROJECT_ENVIRONMENT=str(venv))
        assert done.returncode == 0, done.stderr
        installed = subprocess.run(
            [
                str(venv / "bin" / "python"),
                "-c",
                "import exacly_validate, jsonschema, yaml; "
                "from importlib.util import find_spec; assert find_spec('pytest') is None",
            ],
            cwd=temp,
            capture_output=True,
            text=True,
        )
        assert installed.returncode == 0, installed.stderr


def test_comment_lookup_drains_paginated_duplicates_before_updating_the_first() -> None:
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        plan = temp / "exacly-plan.md"
        plan.write_text("<!-- exacly-plan -->\nUpdated plan\n")
        api_log = temp / "api.json"
        gh = temp / "gh"
        # Replace only the network boundary. A response larger than the pipe buffer makes an early close
        # deliver SIGPIPE, as gh can receive when head stops reading a paginated response.
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:3] == ['api', '--paginate']:\n"
            "    signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
            "    sys.stdout.write('42\\n' + '43\\n' * 100000)\n"
            "    sys.stdout.flush()\n"
            "else:\n"
            "    Path(os.environ['EXACLY_TEST_API_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
        )
        gh.chmod(0o755)
        done = run(
            "Comment the plan, replacing the earlier one",
            temp,
            temp,
            PATH=f"{temp}{os.pathsep}{os.environ['PATH']}",
            GITHUB_REPOSITORY="owner/repo",
            PR="17",
            EXACLY_TEST_API_LOG=str(api_log),
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(api_log.read_text()) == [
            "api",
            "-X",
            "PATCH",
            "repos/owner/repo/issues/comments/42",
            "-F",
            f"body=@{plan}",
        ]
