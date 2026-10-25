"""Checks how the repository keeps config and state (Exacly's own REQ-011): CI checks every change, and
only the state job, on main and after the checks pass, can write, committing nothing but state.

Run with `mise run test`, or from the repository root: uv run pytest tests
"""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW: dict[Any, Any] = yaml.safe_load((ROOT / ".github" / "workflows" / "spec.yml").read_text())
JOBS: dict[str, Any] = WORKFLOW["jobs"]


def run_steps(job: str) -> str:
    return "\n".join(step.get("run", "") for step in JOBS[job]["steps"])


def test_the_workflow_cannot_write_by_default() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_every_change_to_the_model_or_its_tooling_is_checked() -> None:
    # PyYAML reads the `on` key as True.
    triggers = WORKFLOW[True]
    assert {"spec/**", "validator/**", "tests/**"} <= set(triggers["pull_request"]["paths"])
    assert triggers["push"]["branches"] == ["main"]
    assert "mise run check" in run_steps("check")


def test_every_push_to_main_refreshes_the_state() -> None:
    # A push the path filter skipped could move main past a running state job, which then cannot push,
    # and nothing would refresh the state until the next push that matched the filter.
    assert "paths" not in WORKFLOW[True]["push"]


def test_a_rejected_state_push_fails_unless_main_moved_on() -> None:
    script = run_steps("state")
    assert "|| echo" not in script
    assert "set -euo pipefail" in script
    assert "git fetch origin main" in script
    assert '"$(git rev-parse origin/main)" != "$GITHUB_SHA"' in script
    assert "exit 1" in script


def test_only_the_state_job_can_write() -> None:
    writers = [name for name, job in JOBS.items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers == ["state"]


def test_state_is_written_only_from_main_after_the_checks_pass() -> None:
    state = JOBS["state"]
    assert state["needs"] == "check"
    assert state["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert state["concurrency"]["cancel-in-progress"] is False
    # Without a queue, a later job replaces the pending one, so an older revision whose checks finish
    # late could cancel the newest revision's state job.
    assert state["concurrency"]["queue"] == "max"


def test_the_state_job_commits_only_state_and_skips_ci() -> None:
    script = run_steps("state")
    assert "mise run model" in script
    added = [line.split("git add", 1)[1].split() for line in script.splitlines() if "git add" in line]
    assert added == [["spec/exacly/state/main.json"]]
    assert "[skip ci]" in script
    assert "git push origin HEAD:refs/heads/main" in script
    assert "--force" not in script


def test_reports_and_computed_state_are_not_committed() -> None:
    assert "reports/" in (ROOT / ".gitignore").read_text().splitlines()


def test_the_plan_is_commented_only_on_pull_requests_from_this_repository() -> None:
    plan = JOBS["plan"]
    assert plan["permissions"] == {"contents": "read", "pull-requests": "write"}
    # A newer push replaces a plan still being written, so two runs can't both add a comment.
    assert plan["concurrency"] == {
        "group": "exacly-plan-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert plan["if"] == (
        "github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository"
    )
    script = run_steps("plan")
    # This repository's own plan runs the pull request's code, not a released validator.
    # Config only: state committed for main can name records the change removes.
    assert """exacly view plan --base "origin/$BASE" -m 'spec/exacly/*.yaml'""" in script
    # The base branch's name reaches the script through the environment, never pasted into it.
    assert "${{" not in script
    assert "<!-- exacly-plan -->" in script
    assert "| sed -n '1p'" in script
