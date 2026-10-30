"""Installed composition smoke behavior and actual release-shell invocation contracts."""

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
SMOKE = runpy.run_path(str(ROOT / "ci/smoke_composition.py"))
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


@pytest.fixture(scope="module")
def composed() -> dict[str, Any]:
    return dict(SMOKE["run"](require_installed=False))


def test_console_smoke_exercises_four_native_producers_freshness_and_private_outputs(composed: dict[str, Any]) -> None:
    assert composed["package"]["version"]
    assert Path(composed["package"]["module"]).is_file()
    assert Path(composed["package"]["composition_junit_module"]).is_file()
    for check in (
        "offline_definition_validation",
        "four_native_admissions_and_gates",
        "fresh_admission_on_rerun",
        "private_permissions_under_022_000_777",
        "namespace_and_target_preserved",
        "default_layout_has_no_readiness_junit",
        "native_readiness_junit_roundtrip",
        "readiness_junit_private_under_022_000_777",
        "readiness_junit_preserves_producer_proof_bytes",
        "readiness_junit_blocks_failed_and_rejected_members",
        "readiness_junit_unknown_is_failed_not_skipped",
        "admitted_failure_blocks_aggregate",
        "omission_rejected_before_source_access",
        "existing_output_preserved",
        "inputs_preserved",
    ):
        assert composed[check] is True
    assert composed["github_transport"] == "fixture_with_sockets_blocked"
    assert composed["scope"] == "composition_fixture_only"


def test_fixture_rejects_unexpected_metadata_or_artifact_access() -> None:
    source = SMOKE["Source"]()
    with pytest.raises(RuntimeError, match="unexpected composition fixture GitHub request"):
        source.get_json(f"/repos/{SMOKE['REPOSITORY']}/issues/123")
    with pytest.raises(RuntimeError, match="unexpected composition fixture artifact"):
        source.download_artifact(SMOKE["REPOSITORY"], 123)


def test_installed_smoke_refuses_editable_source_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_release_runs_composition_from_the_installed_artifact_outside_checkout(tmp_path: Path, job: str) -> None:
    workspace = tmp_path / "checkout"
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
        "import json,os,sys\n"
        "with open(os.environ['SMOKE_RECORD'],'a') as output:\n"
        " output.write(json.dumps({'cwd':os.getcwd(),'executable':sys.argv[0],'argv':sys.argv[1:]})+'\\n')\n"
    )
    for name in ("uv", "uvx", "python"):
        executable = binaries / name
        executable.write_text(program)
        executable.chmod(0o755)
    published_python = outside / "exacly-publication/bin/python"
    published_python.parent.mkdir(parents=True)
    published_python.write_text(program)
    published_python.chmod(0o755)
    names = (
        ("Run the built wheel",)
        if job == "build"
        else ("Install the published version", "Exercise the published package")
    )
    for step in RELEASE["jobs"][job]["steps"]:
        if any(name in step.get("name", "") for name in names):
            subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                cwd=workspace,
                env={
                    **os.environ,
                    "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
                    "RUNNER_TEMP": str(outside),
                    "TAG": "python-v0.7.0",
                    "SMOKE_RECORD": str(recorder),
                },
                check=True,
                capture_output=True,
                text=True,
            )
    invocations = [json.loads(line) for line in recorder.read_text().splitlines()]
    smoke = str(workspace / "ci/smoke_composition.py")
    selected = [entry for entry in invocations if smoke in entry["argv"]]
    assert len(selected) == 1
    invocation = selected[0]
    assert invocation["cwd"] == str(outside)
    args = invocation["argv"]
    if job == "build":
        assert "--isolated" in args and "--no-project" in args
        assert args[args.index("--with") + 1] == str(wheel)
        assert args[-2:] == ["python", smoke]
    else:
        assert invocation["executable"] == str(published_python)
        assert args == [smoke]
        installer = str(workspace / "ci/install_publication.py")
        setups = [entry for entry in invocations if installer in entry["argv"]]
        assert len(setups) == 1 and invocations.index(setups[0]) < invocations.index(invocation)
