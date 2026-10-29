"""Fast offline checks for the installed policy CLI harness and release shell wiring.

Release runs this harness with the actual wheel and exact published version. Ordinary pytest uses
the editable source only through the explicit unit mode; shell tests replace uv with a recorder.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SMOKE = runpy.run_path(str(ROOT / "ci/smoke_policy.py"))
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


@pytest.fixture(scope="module")
def drafted() -> dict[str, Any]:
    return dict(SMOKE["run"](require_installed=False))


def test_console_smoke_exercises_api_equivalence_protection_and_safe_outputs(drafted: dict[str, Any]) -> None:
    assert drafted["package"]["version"]
    assert Path(drafted["package"]["module"]).is_file()
    for check in (
        "api_equivalent",
        "explicit_branch_protection_retained",
        "default_branch_protection_omitted",
        "exact_json_output",
        "existing_output_preserved",
        "invalid_inputs_rejected",
    ):
        assert drafted[check] is True
    assert drafted["github_transport"] == "fixture_with_sockets_blocked"
    assert drafted["scope"] == "policy_drafting_fixture_only"


def test_fixture_rejects_unexpected_github_reads_and_artifact_downloads() -> None:
    source = SMOKE["Source"]()
    with pytest.raises(RuntimeError, match="unexpected fixture GitHub request"):
        source.get_json(f"/repos/{SMOKE['REPOSITORY']}/branches/main")
    with pytest.raises(RuntimeError, match="must not download evidence artifacts"):
        source.download_artifact(SMOKE["REPOSITORY"], 1)


def test_release_smoke_refuses_editable_install_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        SMOKE["importlib"].metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="fixture-version", read_text=lambda filename: '{"dir_info":{"editable":true}}'
        ),
    )
    with pytest.raises(RuntimeError, match="requires an installed package"):
        SMOKE["installation"]()


@pytest.mark.parametrize("job", ["build", "verify-publication"])
def test_release_runs_policy_cli_from_installed_wheel_or_exact_published_version(tmp_path: Path, job: str) -> None:
    workspace = tmp_path / "source checkout"
    (workspace / "dist").mkdir(parents=True)
    wheel = workspace / "dist/exacly-fixture-py3-none-any.whl"
    wheel.touch()
    outside = tmp_path / "outside checkout"
    outside.mkdir()
    recorder = tmp_path / "commands.jsonl"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    program = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['SMOKE_RECORD'], 'a') as output:\n"
        "    output.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:]}) + '\\n')\n"
    )
    for name in ("uv", "uvx"):
        executable = binaries / name
        executable.write_text(program)
        executable.chmod(0o755)
    name = "Run the built wheel" if job == "build" else "Install the published version"
    step = next(step for step in RELEASE["jobs"][job]["steps"] if name in step.get("name", ""))
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
            "RUNNER_TEMP": str(outside),
            "TAG": "python-v0.7.0rc1",
            "SMOKE_RECORD": str(recorder),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    invocations = [json.loads(line) for line in recorder.read_text().splitlines()]
    smoke = str(workspace / "ci/smoke_policy.py")
    policy = [item for item in invocations if smoke in item["argv"]]
    assert len(policy) == 1
    invocation = policy[0]
    assert invocation["cwd"] == str(outside)
    args = invocation["argv"]
    assert "--isolated" in args and "--no-project" in args
    assert args[args.index("--with") + 1] == (str(wheel) if job == "build" else "exacly==0.7.0rc1")
    assert args[-2:] == ["python", smoke]
    if job == "verify-publication":
        assert args[args.index("--default-index") + 1] == "https://pypi.org/simple"
