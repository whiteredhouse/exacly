"""Exercise the installed evidence producer and versioned consumer outside the checkout.

The declared GitHub context is a test fixture. These checks establish bundle-format and CLI
compatibility, not live evidence origin, test honesty, coverage or approval.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PASSING = b'<testsuite><testcase classname="billing" name="invoice_total"/></testsuite>'
WORKFLOW = ".github/workflows/evidence.yml"
Doc = dict[str, Any]


def installation(*, require_installed: bool = True) -> Doc:
    import exacly

    module = Path(exacly.__file__).resolve()
    distribution = importlib.metadata.distribution("exacly")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if require_installed and (
        direct.get("dir_info", {}).get("editable") is True or not module.is_relative_to(Path(sys.prefix).resolve())
    ):
        raise RuntimeError(f"evidence smoke requires an installed package, found {module}")
    return {"version": distribution.version, "module": str(module)}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Wheel smoke",
            "-c",
            "user.email=wheel@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def archive_bytes(out: Path) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("manifest.json", "junit.xml"):
            archive.writestr(name, (out / name).read_bytes())
    return content.getvalue()


def consume(content: bytes, *, require_installed: bool = True) -> Doc:
    installed = installation(require_installed=require_installed)
    # This branch intentionally imports only the pre-existing consumer API: release CI also
    # invokes it with the published RC2 package, which has no exacly.evidence producer module.
    from exacly import admission

    manifest, report = admission.unpack(content)
    if manifest["schema_version"] != "0.1.0":
        raise RuntimeError("unexpected evidence manifest protocol")
    if manifest["report_digest"] != "sha256:" + hashlib.sha256(report).hexdigest():
        raise RuntimeError("consumer did not establish the retained report digest")
    return {
        "consumer": installed,
        "manifest_version": manifest["schema_version"],
        "report_digest": manifest["report_digest"],
        "scope": "format_compatibility_only",
    }


def run(*, archive: Path | None = None, require_installed: bool = True) -> Doc:
    installed = installation(require_installed=require_installed)
    executable = Path(sys.executable).parent / ("exacly.exe" if os.name == "nt" else "exacly")
    if not executable.is_file():
        raise RuntimeError(f"installed console command is missing: {executable}")
    with tempfile.TemporaryDirectory(prefix="exacly-evidence-wheel-") as scratch:
        repo = Path(scratch) / "consumer"
        repo.mkdir()
        git(repo, "init", "--quiet", "--initial-branch=main")
        (repo / "README.md").write_text("Independent consumer checkout.\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "--quiet", "-m", "consumer source")
        revision = git(repo, "rev-parse", "HEAD")
        report = repo / "test results.xml"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN", "PYTHONPATH", "PYTHONHOME"}
        }
        environment.update(
            GITHUB_REPOSITORY_ID="123",
            GITHUB_REPOSITORY="acme/wheel-smoke",
            GITHUB_RUN_ID="456",
            GITHUB_RUN_ATTEMPT="2",
            GITHUB_SHA=revision,
            GITHUB_JOB="test",
            GITHUB_EVENT_NAME="push",
            GITHUB_REF="refs/heads/main",
            GITHUB_WORKFLOW_REF=f"acme/wheel-smoke/{WORKFLOW}@refs/heads/main",
            EXACLY_TEST_OUTCOME="success",
        )

        def invoke(name: str, *, expected: int = 0, json_output: bool = False, **changes: str) -> tuple[Path, str]:
            out = repo / name
            existed = out.exists()
            args = [str(executable), "evidence", "package", "--report", str(report), "--out", str(out)]
            if json_output:
                args.append("--json")
            result = subprocess.run(
                args, cwd=repo, env={**environment, **changes}, text=True, capture_output=True, check=False
            )
            if result.returncode != expected:
                raise RuntimeError(
                    f"installed producer exited {result.returncode}, expected {expected}: "
                    f"{result.stdout} {result.stderr}"
                )
            if expected and (result.stdout or not result.stderr or (out.exists() and not existed)):
                raise RuntimeError("invalid producer inputs created evidence output")
            return out, result.stdout

        report.write_bytes(PASSING)
        human, summary = invoke("human bundle")
        if not summary.startswith("Prepared verification evidence in "):
            raise RuntimeError("producer summary did not describe packaging")
        out, printed = invoke("json bundle", json_output=True)
        manifest = json.loads((out / "manifest.json").read_text())
        if printed != (out / "manifest.json").read_text():
            raise RuntimeError("JSON output differs from the retained manifest")
        if sorted(path.name for path in out.iterdir()) != ["junit.xml", "manifest.json"]:
            raise RuntimeError("producer output contains unexpected files")
        if manifest["implementation_revision"] != revision or manifest["check_outcome"] != "success":
            raise RuntimeError("producer manifest lost the declared checkout or outcome")
        if (out / "junit.xml").read_bytes() != PASSING or (human / "junit.xml").read_bytes() != PASSING:
            raise RuntimeError("producer altered JUnit bytes")
        if report.read_bytes() != PASSING:
            raise RuntimeError("producer rewrote the original report")
        content = archive_bytes(out)
        consumed = consume(content, require_installed=require_installed)
        original = {path.name: path.read_bytes() for path in out.iterdir()}
        invoke("json bundle", expected=1)
        if original != {path.name: path.read_bytes() for path in out.iterdir()}:
            raise RuntimeError("producer overwrote existing evidence")
        invoke("wrong checkout", expected=1, GITHUB_SHA="0" * 40)
        invoke("pull request", expected=1, GITHUB_EVENT_NAME="pull_request")
        invoke("missing run", expected=1, GITHUB_RUN_ID="")
        failed = b'<testsuite><testcase name="case"><failure/></testcase></testsuite>'
        report.write_bytes(failed)
        failure, _ = invoke("failed bundle", EXACLY_TEST_OUTCOME="failure")
        if (failure / "junit.xml").read_bytes() != failed:
            raise RuntimeError("producer changed failed observations")
        consume(archive_bytes(failure), require_installed=require_installed)
        invoke("contradictory outcome", expected=1)
        skipped = b'<testsuite><testcase name="case"><skipped/></testcase></testsuite>'
        report.write_bytes(skipped)
        skip, _ = invoke("skipped bundle")
        if (skip / "junit.xml").read_bytes() != skipped:
            raise RuntimeError("producer changed skipped observations")
        consume(archive_bytes(skip), require_installed=require_installed)
        report.write_bytes(b"<testsuite>")
        invoke("incomplete report", expected=1)
        if archive is not None:
            with archive.open("xb") as destination:
                destination.write(content)
        return {
            "producer": installed,
            "consumer": consumed["consumer"],
            "manifest_version": manifest["schema_version"],
            "exact_report_bytes": True,
            "failed_and_skipped_retained": True,
            "invalid_inputs_rejected": True,
            "existing_output_preserved": True,
            "scope": "format_compatibility_only",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument("--archive", type=Path, help="save the produced bundle for another compatible consumer")
    choices.add_argument("--consume", type=Path, help="validate an existing bundle using this installed consumer")
    args = parser.parse_args(argv)
    result = consume(args.consume.read_bytes()) if args.consume is not None else run(archive=args.archive)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
