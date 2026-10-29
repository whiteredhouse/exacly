"""Fast offline checks for the installed policy CLI harness and release shell wiring.

Release runs this harness with the actual wheel and exact published version. Ordinary pytest uses
the editable source only through the explicit unit mode; shell tests replace uv with a recorder.
"""

from __future__ import annotations

import base64
import hashlib
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


def wheel_record(tmp_path: Path, *, symlink: bool = False) -> tuple[Any, Path]:
    site = tmp_path / "dependency overlay" / "site-packages"
    site.mkdir(parents=True)
    package = (tmp_path / "uv" / "archive-v0" / "wheel" if symlink else site) / "exacly"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    content = b'"""Installed wheel fixture."""\n'
    module.write_bytes(content)
    if symlink:
        (site / "exacly").symlink_to(package, target_is_directory=True)
    info = site / "exacly-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: exacly\nVersion: 1.0\n")
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    (info / "RECORD").write_text(f"exacly/__init__.py,sha256={digest},{len(content)}\n")
    return SMOKE["importlib"].metadata.PathDistribution(info), site / "exacly/__init__.py"


@pytest.mark.parametrize("symlink", [False, True])
def test_release_smoke_accepts_record_owned_overlay_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    import exacly

    distribution, module = wheel_record(tmp_path, symlink=symlink)
    assert not module.resolve().is_relative_to(Path(sys.prefix).resolve())
    monkeypatch.setattr(exacly, "__file__", str(module))
    monkeypatch.setattr(SMOKE["importlib"].metadata, "distribution", lambda name: distribution)
    assert SMOKE["installation"]() == {"version": "1.0", "module": str(module.resolve())}


@pytest.mark.parametrize("failure", ["unrelated_source", "missing_record"])
def test_release_smoke_rejects_module_without_distribution_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import exacly

    distribution, module = wheel_record(tmp_path)
    if failure == "unrelated_source":
        module = tmp_path / "uv/archive-v0/unrelated-source/exacly/__init__.py"
        module.parent.mkdir(parents=True)
        module.write_text('"""Unrelated source fallback."""\n')
    else:
        (module.parent.parent / "exacly-1.0.dist-info/RECORD").unlink()
    monkeypatch.setattr(exacly, "__file__", str(module))
    monkeypatch.setattr(SMOKE["importlib"].metadata, "distribution", lambda name: distribution)
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
        "    output.write(json.dumps({'cwd': os.getcwd(), 'executable': sys.argv[0], 'argv': sys.argv[1:]}) + '\\n')\n"
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
    scripts = [
        step["run"] for step in RELEASE["jobs"][job]["steps"] if any(name in step.get("name", "") for name in names)
    ]
    for script in scripts:
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=workspace,
            env={
                **os.environ,
                "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
                "RUNNER_TEMP": str(outside),
                "TAG": "python-v0.7.0",
                "SMOKE_RECORD": str(recorder),
            },
            text=True,
            capture_output=True,
            check=True,
        )
    invocations = [json.loads(line) for line in recorder.read_text().splitlines()]
    if job == "verify-publication":
        installer = str(workspace / "ci/install_publication.py")
        setup = [item for item in invocations if installer in item["argv"]]
        assert len(setup) == 1
        assert setup[0]["cwd"] == str(outside)
        assert setup[0]["argv"] == [
            installer,
            "--version",
            "0.7.0",
            "--environment",
            str(published_python.parent.parent),
        ]
    smoke = str(workspace / "ci/smoke_policy.py")
    policy = [item for item in invocations if smoke in item["argv"]]
    assert len(policy) == 1
    invocation = policy[0]
    assert invocation["cwd"] == str(outside)
    args = invocation["argv"]
    if job == "build":
        assert "--isolated" in args and "--no-project" in args
        assert args[args.index("--with") + 1] == str(wheel)
        assert args[-2:] == ["python", smoke]
    else:
        assert invocation["executable"] == str(published_python)
        assert args == [smoke]
