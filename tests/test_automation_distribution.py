"""Fast installed-identity and actual release-shell checks for the automation smoke.

Release runs the real detached-runtime smoke. These tests exercise its installation guard
and shell wiring without requiring Bubblewrap, building distributions or registry access.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import importlib.metadata
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


@pytest.fixture(scope="module")
def smoke() -> dict[str, Any]:
    return runpy.run_path(str(ROOT / "ci/smoke_automation.py"))


def test_automation_smoke_rejects_an_editable_install(smoke: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke["importlib"].metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="fixture-version", read_text=lambda filename: '{"dir_info":{"editable":true}}'
        ),
    )
    with pytest.raises(RuntimeError, match="editable install"):
        smoke["installation"]()


def package_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, symlink: bool = False) -> tuple[Any, Path, Path]:
    import exacly

    # Ensure the identity guard's modules have been imported before replacing their paths.
    for name in (
        "dispatch",
        "dispatch_store",
        "executor",
        "executor_store",
        "executor_interfaces",
        "executor_local",
        "executor_runtime",
        "executions",
        "execution_store",
        "handoff",
        "handoff_git",
        "handoff_store",
        "isolation",
        "state_writer",
    ):
        importlib.import_module("exacly." + name)
    original = Path(exacly.__file__).resolve().parent
    site = tmp_path / "dependency overlay" / "site-packages"
    site.mkdir(parents=True)
    package = (tmp_path / "uv/archive-v0/wheel" if symlink else site) / "exacly"
    shutil.copytree(original, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if symlink:
        (site / "exacly").symlink_to(package, target_is_directory=True)
    info = site / "exacly-0.7.0rc1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: exacly\nVersion: 0.7.0rc1\n")
    with (info / "RECORD").open("w", newline="") as output:
        writer = csv.writer(output)
        for path in sorted(package.rglob("*.py")):
            content = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            writer.writerow((str(Path("exacly") / path.relative_to(package)), "sha256=" + digest, len(content)))
    for name, module in tuple(sys.modules.items()):
        source = getattr(module, "__file__", None)
        if (name == "exacly" or name.startswith("exacly.")) and source:
            path = Path(source).resolve()
            if path.is_relative_to(original):
                monkeypatch.setattr(module, "__file__", str(site / "exacly" / path.relative_to(original)))
    return importlib.metadata.PathDistribution(info), site / "exacly/__init__.py", info / "RECORD"


@pytest.mark.parametrize("symlink", [False, True])
def test_automation_smoke_accepts_record_owned_overlay_and_symlink(
    tmp_path: Path, smoke: dict[str, Any], monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    distribution, module, _ = package_record(tmp_path, monkeypatch, symlink=symlink)
    monkeypatch.setattr(smoke["importlib"].metadata, "distribution", lambda name: distribution)
    result = smoke["installation"]()
    assert result["version"] == "0.7.0rc1"
    assert result["module"] == str(module.resolve())
    assert result["verified_modules"] >= 8


@pytest.mark.parametrize("failure", ["missing_record", "tampered_content", "tampered_size", "unowned_module"])
def test_automation_smoke_rejects_missing_or_changed_distribution_bytes(
    tmp_path: Path, smoke: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import exacly.handoff

    distribution, module, record = package_record(tmp_path, monkeypatch)
    if failure == "missing_record":
        record.unlink()
    elif failure == "tampered_content":
        handoff = module.parent / "handoff.py"
        content = handoff.read_bytes()
        handoff.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    elif failure == "tampered_size":
        with record.open(newline="") as source:
            rows = list(csv.reader(source))
        for row in rows:
            if row[0] == "exacly/handoff.py":
                row[2] = str(int(row[2]) + 1)
        with record.open("w", newline="") as output:
            csv.writer(output).writerows(rows)
    else:
        unrelated = tmp_path / "source fallback" / "handoff.py"
        unrelated.parent.mkdir()
        unrelated.write_text('"""An unrelated module outside the installed package."""\n')
        monkeypatch.setattr(exacly.handoff, "__file__", str(unrelated))
    monkeypatch.setattr(smoke["importlib"].metadata, "distribution", lambda name: distribution)
    with pytest.raises(RuntimeError, match=r"installed|RECORD|record|package|differs"):
        smoke["installation"]()


@pytest.mark.parametrize("job", ["build", "verify-publication"])
def test_release_runs_automation_from_the_installed_artifact_outside_checkout(tmp_path: Path, job: str) -> None:
    workspace = tmp_path / "source checkout"
    (workspace / "dist").mkdir(parents=True)
    wheel = workspace / "dist/exacly-0.7.0rc1-py3-none-any.whl"
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
    for name in ("uv", "uvx", "python", "sh"):
        executable = binaries / name
        executable.write_text(program)
        executable.chmod(0o755)
    published_python = outside / "exacly-publication/bin/python"
    published_python.parent.mkdir(parents=True)
    published_python.write_text(program)
    published_python.chmod(0o755)
    (workspace / "ci").mkdir()
    (workspace / "ci/check_isolation_runtime.py").write_text(program)
    names = (
        ("Install the Linux isolation test runtime", "Check the Linux isolation test runtime", "Run the built wheel")
        if job == "build"
        else (
            "Install the Linux isolation test runtime",
            "Check the Linux isolation test runtime",
            "Install the published version",
            "Exercise the published package",
        )
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
                    "TAG": "python-v0.7.0rc1",
                    "SMOKE_RECORD": str(recorder),
                },
                check=True,
                capture_output=True,
                text=True,
            )
    invocations = [json.loads(line) for line in recorder.read_text().splitlines()]
    smoke = str(workspace / "ci/smoke_automation.py")
    selected = [entry for entry in invocations if smoke in entry["argv"]]
    assert len(selected) == 1
    invocation = selected[0]
    assert RELEASE["jobs"][job]["runs-on"] == "ubuntu-24.04"
    runtime_setup = next(entry for entry in invocations if "ci/setup_isolation_runtime.sh" in entry["argv"])
    runtime_check = next(entry for entry in invocations if entry["executable"] == "ci/check_isolation_runtime.py")
    assert runtime_setup["cwd"] == runtime_check["cwd"] == str(workspace)
    assert invocations.index(runtime_setup) < invocations.index(runtime_check) < invocations.index(invocation)
    assert invocation["cwd"] == str(outside)
    args = invocation["argv"]
    if job == "build":
        assert "--isolated" in args and "--no-project" in args
        assert args[args.index("--with") + 1] == str(wheel)
        assert args[-3:] == ["python", "-I", smoke]
    else:
        assert invocation["executable"] == str(published_python)
        assert args == ["-I", smoke]
        installer = str(workspace / "ci/install_publication.py")
        setups = [entry for entry in invocations if installer in entry["argv"]]
        assert len(setups) == 1 and invocations.index(setups[0]) < invocations.index(invocation)
        assert "0.7.0rc1" in setups[0]["argv"]
