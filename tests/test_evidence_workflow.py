"""The evidence producer has one approved upload contract and preserves observed report bytes."""

import hashlib
import json
import re
import runpy
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ".github/workflows/verification-evidence.yml"
WORKFLOW: dict[Any, Any] = yaml.safe_load((ROOT / WORKFLOW_PATH).read_text())
PRODUCER = runpy.run_path(str(ROOT / "ci/package_verification_evidence.py"))
SHA = "a" * 40
PASSING = b'<testsuites><testsuite><testcase file="tests/test_example.py" name="example"/></testsuite></testsuites>'


def environment(**overrides: str) -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_REPOSITORY": "acme/example",
        "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": SHA,
        "GITHUB_JOB": "observe",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": f"acme/example/{WORKFLOW_PATH}@refs/heads/main",
        "EXACLY_TEST_OUTCOME": "success",
        **overrides,
    }


def package(tmp_path: Path, report: bytes = PASSING, **overrides: str) -> tuple[dict[str, Any], Path]:
    source = tmp_path / "junit.xml"
    source.write_bytes(report)
    out = tmp_path / "evidence"
    manifest = PRODUCER["package"](source, out, environ=environment(**overrides), checkout=lambda: SHA)
    return manifest, out


def test_workflow_is_one_read_only_push_main_job_without_dynamic_job_expansion() -> None:
    assert WORKFLOW[True] == {"push": {"branches": ["main"]}}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert set(WORKFLOW["jobs"]) == {"observe"}
    job = WORKFLOW["jobs"]["observe"]
    assert job["name"] == "Observe verification"
    assert job["runs-on"] == "ubuntu-latest"
    assert "strategy" not in job and "uses" not in job
    assert "continue-on-error" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])


def test_workflow_checks_out_the_run_revision_without_persisting_credentials() -> None:
    steps = WORKFLOW["jobs"]["observe"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
    install = next(step for step in steps if step.get("name") == "Install locked dependencies")
    assert install["run"] == "uv sync --locked"
    check = next(step for step in steps if step.get("name") == "Run verification")
    assert check["id"] == "verification"
    assert check["env"] == {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    assert (
        check["run"]
        == "uv run --locked pytest -c pyproject.toml spec/tests python/tests tests --junitxml=reports/junit.xml"
    )


def test_completed_failures_are_packaged_and_each_attempt_has_one_artifact_name() -> None:
    steps = WORKFLOW["jobs"]["observe"]["steps"]
    package_step = next(step for step in steps if step.get("name") == "Package verification evidence")
    assert package_step["id"] == "evidence"
    assert package_step["if"] == (
        "always() && (steps.verification.outcome == 'success' || steps.verification.outcome == 'failure')"
    )
    assert package_step["env"] == {"EXACLY_TEST_OUTCOME": "${{ steps.verification.outcome }}"}
    assert package_step["run"] == (
        "uv run --locked python ci/package_verification_evidence.py --report reports/junit.xml --out reports/evidence"
    )
    uploads = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["name"] == "Upload verification evidence"
    assert upload["if"] == "always() && steps.evidence.outcome == 'success'"
    assert upload["with"] == {
        "name": "exacly-evidence-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "reports/evidence",
        "if-no-files-found": "error",
        "retention-days": 30,
        "include-hidden-files": False,
    }


def test_bundle_preserves_report_and_records_only_producer_identity_and_integrity(tmp_path: Path) -> None:
    manifest, out = package(tmp_path)
    assert sorted(path.name for path in out.iterdir()) == ["junit.xml", "manifest.json"]
    assert (out / "junit.xml").read_bytes() == PASSING
    assert (tmp_path / "junit.xml").read_bytes() == PASSING
    assert manifest == {
        "schema_version": "0.1.0",
        "repository_id": 123,
        "repository": "acme/example",
        "run_id": 456,
        "run_attempt": 2,
        "workflow_path": WORKFLOW_PATH,
        "job": "observe",
        "implementation_revision": SHA,
        "report_digest": "sha256:" + hashlib.sha256(PASSING).hexdigest(),
        "check_outcome": "success",
    }
    assert json.loads((out / "manifest.json").read_text()) == manifest


@pytest.mark.parametrize("outcome,child", [("failure", '<failure message="incorrect"/>'), ("success", "<skipped/>")])
def test_failed_and_skipped_case_observations_are_retained(tmp_path: Path, outcome: str, child: str) -> None:
    report = f'<testsuite><testcase name="case">{child}</testcase></testsuite>'.encode()
    manifest, out = package(tmp_path, report, EXACLY_TEST_OUTCOME=outcome)
    assert manifest["check_outcome"] == outcome
    assert (out / "junit.xml").read_bytes() == report


@pytest.mark.parametrize(
    "overrides",
    [
        {"GITHUB_REPOSITORY": "not/a/repository"},
        {"GITHUB_REPOSITORY": "../example/extra"},
        {"GITHUB_REPOSITORY_ID": "0"},
        {"GITHUB_RUN_ID": "-1"},
        {"GITHUB_RUN_ATTEMPT": "attempt-two"},
        {"GITHUB_SHA": "short"},
        {"GITHUB_JOB": "other-job"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_REF": "refs/heads/other"},
        {"GITHUB_WORKFLOW_REF": "acme/example/other.yml@refs/heads/main"},
        {"EXACLY_TEST_OUTCOME": "cancelled"},
        {"EXACLY_TEST_OUTCOME": "skipped"},
        {"EXACLY_TEST_OUTCOME": ""},
    ],
)
def test_unsupported_producer_metadata_creates_no_output(tmp_path: Path, overrides: dict[str, str]) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "not-created" / "evidence"
    with pytest.raises(PRODUCER["EvidenceError"]):
        PRODUCER["package"](report, out, environ=environment(**overrides), checkout=lambda: SHA)
    assert not out.parent.exists()
    assert report.read_bytes() == PASSING


def test_wrong_actual_checkout_creates_no_output(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "evidence"
    with pytest.raises(PRODUCER["EvidenceError"], match="checkout"):
        PRODUCER["package"](report, out, environ=environment(), checkout=lambda: "b" * 40)
    assert not out.exists()


@pytest.mark.parametrize(
    "report",
    [
        b"",
        b"<testsuites><testsuite>",
        b"<testsuites/>",
        b"<other><testcase/></other>",
        b'<!DOCTYPE testsuite [<!ENTITY example "case">]><testsuite><testcase name="&example;"/></testsuite>',
        b'<testsuite><testcase name="failure"><failure/></testcase></testsuite>',
        b'<testsuite><testcase name="error"><error/></testcase></testsuite>',
        b'<testsuite><testcase name="\xff"/></testsuite>',
        '<testsuite><testcase name="case"/></testsuite>'.encode("utf-16"),
        b'<?xml version="1.0" encoding="UTF-16"?><testsuite><testcase/></testsuite>',
        b'<?xml version="1.0" encoding="fake-encoding"?><testsuite><testcase/></testsuite>',
    ],
)
def test_incomplete_unsafe_or_contradictory_reports_create_no_bundle(tmp_path: Path, report: bytes) -> None:
    source = tmp_path / "junit.xml"
    source.write_bytes(report)
    out = tmp_path / "evidence"
    with pytest.raises(PRODUCER["EvidenceError"]):
        PRODUCER["package"](source, out, environ=environment(), checkout=lambda: SHA)
    assert not out.exists()


def test_existing_output_directory_is_not_overwritten(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "evidence"
    out.mkdir()
    sentinel = out / "keep"
    sentinel.write_text("existing output")
    with pytest.raises(PRODUCER["EvidenceError"], match="already exist"):
        PRODUCER["package"](report, out, environ=environment(), checkout=lambda: SHA)
    assert sentinel.read_text() == "existing output"
    assert list(out.iterdir()) == [sentinel]


def test_oversized_report_is_rejected_before_output_creation(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(b"x" * (PRODUCER["MAX_REPORT_BYTES"] + 1))
    out = tmp_path / "evidence"
    with pytest.raises(PRODUCER["EvidenceError"], match="size"):
        PRODUCER["package"](report, out, environ=environment(), checkout=lambda: SHA)
    assert not out.exists()
