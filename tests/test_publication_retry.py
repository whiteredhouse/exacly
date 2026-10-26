"""Publication install retries are bounded; smoke failures do not retry installation."""

import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def installer() -> dict[str, Any]:
    return runpy.run_path(str(ROOT / "ci" / "install_publication.py"))


def test_install_retries_with_fresh_metadata_until_the_exact_version_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = installer()
    attempts: list[list[str]] = []
    slept: list[float] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        attempts.append(command)
        assert kwargs["timeout"] <= 120
        return subprocess.CompletedProcess(command, 1 if len(attempts) < 3 else 0, "", "version not visible")

    monkeypatch.setattr(module["subprocess"], "run", run)
    monkeypatch.setattr(module["time"], "sleep", slept.append)
    module["install"]("0.4.0", Path("/tmp/publication/bin/python"))
    assert len(attempts) == 3
    assert slept == [10, 10]
    assert all("exacly==0.4.0" in args and "--refresh-package" in args for args in attempts)
    assert all(args[args.index("--python") + 1] == "/tmp/publication/bin/python" for args in attempts)


def test_retry_deadline_preserves_last_install_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    module = installer()
    clock = [0.0]
    attempts = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        attempts.append(command)
        return subprocess.CompletedProcess(command, 1, "", "PyPI version unavailable")

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(module["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(module["time"], "sleep", sleep)
    monkeypatch.setattr(module["subprocess"], "run", run)
    with pytest.raises(RuntimeError, match="PyPI version unavailable"):
        module["install"]("0.4.0", Path("/tmp/python"), timeout=20, interval=10)
    assert clock[0] == 20
    assert len(attempts) == 2


def test_hung_install_cannot_exceed_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    module = installer()

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert 0 < kwargs["timeout"] <= 20
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr="network timed out")

    monkeypatch.setattr(module["subprocess"], "run", run)
    with pytest.raises(RuntimeError, match="network timed out"):
        module["install"]("0.4.0", Path("/tmp/python"), timeout=20)


def test_smoke_failure_runs_once_in_a_separate_step(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["verify-publication"]["steps"]
    smoke = next(step["run"] for step in steps if step.get("name") == "Exercise the published package")
    environment = tmp_path / "exacly-publication" / "bin"
    environment.mkdir(parents=True)
    python = environment / "python"
    python.write_text('#!/bin/sh\necho invoked >> "$RUNNER_TEMP/smoke-invocations"\nexit 7\n')
    python.chmod(0o755)
    done = subprocess.run(["bash", "-c", smoke], cwd=ROOT, env={"RUNNER_TEMP": str(tmp_path)}, capture_output=True)
    assert done.returncode == 7
    assert (tmp_path / "smoke-invocations").read_text().splitlines() == ["invoked"]
