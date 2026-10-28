"""Package a GitHub verification report without making verification or admission claims.

The approved workflow is the producer contract. These environment values are metadata; consumers must
verify them against GitHub and an independently approved policy before admitting this bundle.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

WORKFLOW = ".github/workflows/verification-evidence.yml"
JOB = "observe"
MAX_REPORT_BYTES = 8 * 1024 * 1024


class EvidenceError(ValueError):
    """The producer cannot establish the supported bundle inputs."""


def positive_integer(environ: Mapping[str, str], key: str) -> int:
    value = environ.get(key, "")
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise EvidenceError(f"{key} must identify a positive integer")
    return int(value)


def checkout_revision() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise EvidenceError("cannot establish the checked-out implementation revision") from error


def package(
    report: Path,
    out: Path,
    *,
    environ: Mapping[str, str] | None = None,
    checkout: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Copy a complete XML document and its identity manifest into a new directory.

    Completed failures and reports containing only skipped cases remain observations. This producer does
    not establish that reported tests honestly or completely cover acceptance criteria.
    """
    env = os.environ if environ is None else environ
    repository = env.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise EvidenceError("GITHUB_REPOSITORY must identify one repository")
    if env.get("GITHUB_EVENT_NAME") != "push" or env.get("GITHUB_REF") != "refs/heads/main":
        raise EvidenceError("only push runs on main are supported")
    if env.get("GITHUB_JOB") != JOB:
        raise EvidenceError("the designated observation job must produce the bundle")
    if env.get("GITHUB_WORKFLOW_REF") != f"{repository}/{WORKFLOW}@refs/heads/main":
        raise EvidenceError("the designated workflow must produce the bundle")
    revision = env.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("GITHUB_SHA must be a full implementation revision")
    if (checkout or checkout_revision)() != revision:
        raise EvidenceError("the checkout does not match the declared implementation revision")
    outcome = env.get("EXACLY_TEST_OUTCOME")
    if outcome not in {"success", "failure"}:
        raise EvidenceError("the verification step must have completed with success or failure")
    repository_id = positive_integer(env, "GITHUB_REPOSITORY_ID")
    run_id = positive_integer(env, "GITHUB_RUN_ID")
    attempt = positive_integer(env, "GITHUB_RUN_ATTEMPT")
    with report.open("rb") as source:
        content = source.read(MAX_REPORT_BYTES + 1)
    if len(content) > MAX_REPORT_BYTES:
        raise EvidenceError("the verification report exceeds the supported size")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError("the verification report must use UTF-8") from error
    if "\x00" in text:
        raise EvidenceError("the verification report must use UTF-8 without null bytes")
    declaration = re.match(r"\ufeff?<\?xml\s+.*?\?>", text, re.DOTALL)
    if declaration is not None:
        encoding = re.search(r"\bencoding\s*=\s*(['\"])([^'\"]*)\1", declaration.group())
        if encoding is not None and encoding.group(2).lower() not in {"utf-8", "utf8"}:
            raise EvidenceError("the verification report must declare UTF-8 when declaring an encoding")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise EvidenceError("declarations are not supported in verification reports")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, LookupError) as error:
        raise EvidenceError("the verification report is not complete XML") from error
    if root.tag not in {"testsuite", "testsuites"} or next(root.iter("testcase"), None) is None:
        raise EvidenceError("the verification report must contain test cases")
    if outcome == "success" and (
        next(root.iter("failure"), None) is not None or next(root.iter("error"), None) is not None
    ):
        raise EvidenceError("the verification outcome contradicts the report")
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "repository_id": repository_id,
        "repository": repository,
        "run_id": run_id,
        "run_attempt": attempt,
        "workflow_path": WORKFLOW,
        "job": JOB,
        "implementation_revision": revision,
        "report_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "check_outcome": outcome,
    }
    if out.exists() or out.is_symlink():
        raise EvidenceError("the evidence output directory must not already exist")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    written: list[Path] = []
    try:
        report_copy = out / "junit.xml"
        with report_copy.open("xb") as destination:
            written.append(report_copy)
            destination.write(content)
        manifest_file = out / "manifest.json"
        with manifest_file.open("x", encoding="utf-8") as destination:
            written.append(manifest_file)
            destination.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except OSError:
        for path in written:
            path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            out.rmdir()
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        package(args.report, args.out)
    except (EvidenceError, OSError) as error:
        print(f"verification evidence: {error}", file=sys.stderr)
        return 1
    print(f"Prepared verification evidence in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
