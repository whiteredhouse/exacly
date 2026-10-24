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
    for event in ("pull_request", "push"):
        assert {"spec/**", "validator/**", "tests/**"} <= set(triggers[event]["paths"])
    assert triggers["push"]["branches"] == ["main"]
    assert "mise run check" in run_steps("check")


def test_only_the_state_job_can_write() -> None:
    writers = [name for name, job in JOBS.items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers == ["state"]


def test_state_is_written_only_from_main_after_the_checks_pass() -> None:
    state = JOBS["state"]
    assert state["needs"] == "check"
    assert state["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert state["concurrency"]["cancel-in-progress"] is False


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
