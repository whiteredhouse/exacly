"""Fast smoke-harness checks; Release runs the same harness against actual installed wheels.

Ordinary pytest uses the editable package explicitly and performs no build or registry access.
The shell checks replace uv with a recorder to verify independent installed environments and cwd.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from exacly import admission

ROOT = Path(__file__).resolve().parents[1]
SMOKE = runpy.run_path(str(ROOT / "ci/smoke_evidence.py"))
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


@pytest.fixture(scope="module")
def produced(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], bytes]:
    archive = tmp_path_factory.mktemp("producer-smoke") / "evidence.zip"
    result = SMOKE["run"](archive=archive, require_installed=False)
    return result, archive.read_bytes()


def test_console_smoke_exercises_bundle_outcomes_and_preserves_inputs(produced: tuple[dict[str, Any], bytes]) -> None:
    result, content = produced
    assert result["manifest_version"] == "0.1.0"
    assert result["exact_report_bytes"] is True
    assert result["failed_and_skipped_retained"] is True
    assert result["invalid_inputs_rejected"] is True
    assert result["existing_output_preserved"] is True
    assert result["scope"] == "format_compatibility_only"
    manifest, report = admission.unpack(content)
    assert manifest["implementation_revision"] != "0" * 40
    assert report == SMOKE["PASSING"]


def test_consumer_mode_needs_only_the_existing_admission_api(produced: tuple[dict[str, Any], bytes]) -> None:
    result, content = produced
    consumed = SMOKE["consume"](content, require_installed=False)
    assert consumed["manifest_version"] == result["manifest_version"]
    assert consumed["consumer"]["version"]
    assert consumed["scope"] == "format_compatibility_only"
    with zipfile.ZipFile(io.BytesIO(content)) as original:
        manifest = original.read("manifest.json")
    altered = io.BytesIO()
    with zipfile.ZipFile(altered, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("junit.xml", b'<testsuite><testcase name="changed"/></testsuite>')
    with pytest.raises(admission.Rejected):
        SMOKE["consume"](altered.getvalue(), require_installed=False)


def test_release_smoke_refuses_an_editable_install(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = SMOKE["importlib"].metadata
    monkeypatch.setattr(
        metadata,
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
def test_release_runs_producer_and_older_consumer_in_separate_installed_environments(tmp_path: Path, job: str) -> None:
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
    smoke = str(workspace / "ci/smoke_evidence.py")
    evidence = [item for item in invocations if smoke in item["argv"]]
    assert len(evidence) == 2
    producer, consumer = evidence
    assert producer["cwd"] == consumer["cwd"] == str(outside)
    producer_args, consumer_args = producer["argv"], consumer["argv"]
    for args in (producer_args, consumer_args):
        assert "--isolated" in args and "--no-project" in args
    assert producer_args[producer_args.index("--with") + 1] == (str(wheel) if job == "build" else "exacly==0.7.0rc1")
    assert consumer_args[consumer_args.index("--with") + 1] == "exacly==0.6.0rc2"
    assert producer_args[producer_args.index("--archive") + 1] == consumer_args[consumer_args.index("--consume") + 1]
    assert "--default-index" in consumer_args
