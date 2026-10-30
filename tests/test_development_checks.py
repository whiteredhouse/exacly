"""Development subsets must not replace the complete report or verification task graph."""

import shlex
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS: dict[str, dict[str, Any]] = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
BACKEND_TESTS = {
    "python/tests/test_execution_isolation.py",
    "python/tests/test_isolation_syscalls.py",
    "python/tests/test_isolation_contract.py",
    "python/tests/test_isolation_cli.py",
    "python/tests/test_executor.py",
    "python/tests/test_executor_faults.py",
    "python/tests/test_executor_runtime.py",
    "python/tests/test_executor_cli.py",
}


def closure(name: str) -> set[str]:
    pending = [name]
    names = {name}
    while pending:
        for dependency in TASKS[pending.pop()].get("depends", []):
            if dependency not in names:
                names.add(dependency)
                pending.append(dependency)
    return names


def test_development_subset_keeps_the_full_test_report_and_backend_coverage_separate() -> None:
    core = shlex.split(TASKS["test-core"]["run"][-1])
    full = shlex.split(TASKS["test"]["run"])
    assert core[:4] == full[:4] == ["uv", "run", "--locked", "pytest"]
    assert all(path in core and path in full for path in ("spec/tests", "python/tests", "tests"))
    ignored = {argument.removeprefix("--ignore=") for argument in core if argument.startswith("--ignore=")}
    assert ignored == BACKEND_TESTS
    assert all((ROOT / path).is_file() for path in ignored)
    assert "--junitxml=reports/core-junit.xml" in core
    assert "--junitxml=reports/junit.xml" not in core
    assert "--junitxml=reports/junit.xml" in full
    assert "--junitxml=reports/core-junit.xml" not in full
    assert not any(argument.startswith(("--ignore", "--deselect")) or argument in ("-k", "-m") for argument in full)


def test_development_checks_cannot_refresh_model_state_or_establish_verification() -> None:
    assert closure("check-core") == {"check-core", "test-core", "sync", "lint", "typecheck", "examples"}
    assert closure("check") == {"check", "verification", "model", "test", "sync", "lint", "typecheck", "examples"}
    assert "--junit reports/junit.xml" in TASKS["model"]["run"]
    assert "reports/core-junit.xml" not in TASKS["model"]["run"]
