"""Producer and consumer share one wire contract while validating their own inputs."""

import copy
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from exacly import _evidence_format as wire
from exacly import admission, evidence

REVISION = "a" * 40
WORKFLOW = ".github/workflows/verification.yml"
LEGACY_REPORT = b'<testsuite><testcase name="legacy"/></testsuite>'
LEGACY_MANIFEST: dict[str, Any] = {
    "schema_version": "0.1.0",
    "repository_id": 123,
    "repository": "acme/example",
    "run_id": 456,
    "run_attempt": 2,
    "workflow_path": WORKFLOW,
    "job": "verify",
    "implementation_revision": REVISION,
    "report_digest": "sha256:" + hashlib.sha256(LEGACY_REPORT).hexdigest(),
    "check_outcome": "success",
}


def archive(manifest: dict[str, Any], report: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("manifest.json", json.dumps(manifest))
        zipped.writestr("junit.xml", report)
    return output.getvalue()


def environment() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_REPOSITORY": "acme/example",
        "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": REVISION,
        "GITHUB_JOB": "verify",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/release/stable",
        "GITHUB_WORKFLOW_REF": f"acme/example/{WORKFLOW}@refs/heads/release/stable",
        "EXACLY_TEST_OUTCOME": "success",
    }


def test_existing_admission_aliases_keep_the_manifest_contract_separate_from_receipts() -> None:
    assert admission.VERSION == wire.MANIFEST_VERSION == "0.1.0"
    assert admission.MANIFEST_SCHEMA is wire.MANIFEST_SCHEMA
    assert admission.REPORT_LIMIT == evidence.MAX_REPORT_BYTES == wire.REPORT_LIMIT == 8 * 1024 * 1024
    assert admission.MANIFEST_LIMIT == wire.MANIFEST_LIMIT == 64 * 1024
    assert admission.RECEIPT_VERSION == "0.2.0"


def test_shared_format_loads_without_the_consumer_or_planner_dependencies() -> None:
    script = (
        "import runpy, sys; "
        "sys.modules['jsonschema'] = None; "
        "sys.modules['exacly.planning'] = None; "
        "contract = runpy.run_path(sys.argv[1]); "
        "assert contract['MANIFEST_VERSION'] == '0.1.0'; "
        "assert 'exacly.admission' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", script, wire.__file__], check=True, capture_output=True)


def test_packaged_junit_bytes_and_manifest_roundtrip_through_independent_admission(tmp_path: Path) -> None:
    report = (
        b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\r\n'
        b'<testsuites>\r\n<testsuite><testcase name="caf\xc3\xa9"/></testsuite>\r\n</testsuites>\r\n'
    )
    original = tmp_path / "input.xml"
    original.write_bytes(report)
    output = tmp_path / "bundle"
    manifest = evidence.package(original, output, environ=environment(), checkout=lambda: REVISION)
    expected = {**LEGACY_MANIFEST, "report_digest": "sha256:" + hashlib.sha256(report).hexdigest()}
    assert manifest == expected
    assert (output / "manifest.json").read_bytes() == (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode()
    assert (output / "junit.xml").read_bytes() == original.read_bytes() == report
    received, retained = admission.unpack(archive(json.loads((output / "manifest.json").read_text()), report))
    assert received == expected
    assert retained == report


def test_consumer_accepts_original_manifest_without_calling_the_packager(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("admission must validate independently of the producer")

    monkeypatch.setattr(evidence, "package", forbidden)
    manifest, report = admission.unpack(archive(LEGACY_MANIFEST, LEGACY_REPORT))
    assert manifest == LEGACY_MANIFEST
    assert report == LEGACY_REPORT


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", "0.2.0"),
        ("repository_id", True),
        ("run_attempt", "2"),
        ("job", "dynamic/matrix"),
        ("check_outcome", "skipped"),
        ("unexpected_claim", True),
    ],
)
def test_consumer_revalidates_untrusted_manifest_against_the_shared_contract(key: str, value: Any) -> None:
    changed = copy.deepcopy(LEGACY_MANIFEST)
    changed[key] = value
    with pytest.raises(admission.Rejected, match="manifest is invalid"):
        admission.unpack(archive(changed, LEGACY_REPORT))
