"""The example workflow follows its contract; the reusable producer preserves observed report bytes."""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from exacly import admission, evidence

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ".github/workflows/verification-evidence.yml"
WORKFLOW: dict[Any, Any] = yaml.safe_load((ROOT / WORKFLOW_PATH).read_text())
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
    manifest = evidence.package(source, out, environ=environment(**overrides), checkout=lambda: SHA)
    return manifest, out


def test_configured_example_matches_the_actual_producer_workflow() -> None:
    profile = yaml.safe_load((ROOT / "ci/evidence-producer.yaml").read_text())
    producer = admission.validate_producer(profile["producer"])
    admission.workflow_contract((ROOT / producer["workflow"]["path"]).read_bytes(), producer)


@pytest.mark.parametrize(
    "selected",
    [
        "python/src/exacly/cli.py",
        "python/src/exacly/evidence.py",
        "python/src/exacly/_evidence_format.py",
        "python/src/exacly/__init__.py",
        "python/src/exacly/future_packaging_input.py",
        "ci/smoke.py",
        "ci/smoke_evidence.py",
        "ci/future/helper.py",
        ".github/workflows/release.yml",
    ],
)
def test_proposal_pins_source_and_ci_execution_inputs(selected: str) -> None:
    profile = yaml.safe_load((ROOT / "ci/evidence-producer.yaml").read_text())
    entries = [
        {
            "path": file.relative_to(ROOT).as_posix(),
            "mode": "100644",
            "type": "blob",
            "sha": hashlib.sha1(b"blob " + str(file.stat().st_size).encode() + b"\0" + file.read_bytes()).hexdigest(),
        }
        for file in sorted(
            [
                *(ROOT / "python/src/exacly").rglob("*.py"),
                *(ROOT / "ci").rglob("*.py"),
                ROOT / ".github/workflows/release.yml",
            ]
        )
    ]
    approved = admission.observer_digest(entries, profile["observer_paths"])
    changed = [dict(entry) for entry in entries]
    for entry in changed:
        if entry["path"] == selected:
            entry["sha"] = "b" * 40
            break
    else:
        changed.append({"path": selected, "mode": "100644", "type": "blob", "sha": "b" * 40})
    assert admission.observer_digest(changed, profile["observer_paths"]) != approved


def test_workflow_is_one_read_only_push_main_job_without_dynamic_job_expansion() -> None:
    assert WORKFLOW[True] == {"push": {"branches": ["main"]}}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert set(WORKFLOW["jobs"]) == {"observe"}
    job = WORKFLOW["jobs"]["observe"]
    assert job["name"] == "Observe verification"
    assert job["runs-on"] == "ubuntu-24.04"
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
    runtime = next(step for step in steps if step.get("name") == "Install the Linux isolation test runtime")
    assert runtime["run"] == "sh ci/setup_isolation_runtime.sh"
    boundary = next(step for step in steps if step.get("name") == "Check the Linux isolation test runtime")
    assert boundary["run"] == "/usr/bin/python3 ci/check_isolation_runtime.py"
    assert steps.index(checkout) < steps.index(runtime) < steps.index(boundary) < steps.index(install)
    check = next(step for step in steps if step.get("name") == "Run verification")
    assert check["id"] == "verification"
    assert check["env"] == {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    assert (
        check["run"]
        == "uv run --locked pytest -c pyproject.toml spec/tests python/tests tests --junitxml=reports/junit.xml"
    )


def test_ci_namespace_permission_is_scoped_to_system_bubblewrap_and_preserves_global_restrictions() -> None:
    setup = (ROOT / "ci/setup_isolation_runtime.sh").read_text()
    privileged = [line for line in setup.splitlines() if line.startswith("sudo ")]
    assert privileged == [
        "sudo -n env DEBIAN_FRONTEND=noninteractive timeout --signal=TERM --kill-after=15s 300s \\",
        "sudo -n env DEBIAN_FRONTEND=noninteractive timeout --signal=TERM --kill-after=15s 300s \\",
        "sudo -n timeout --signal=TERM --kill-after=15s 60s install -m 0644 "
        '"$isolation_setup_dir/apparmor/exacly-bwrap" /etc/apparmor.d/exacly-bwrap',
        "sudo -n timeout --signal=TERM --kill-after=15s 60s apparmor_parser --replace /etc/apparmor.d/exacly-bwrap",
    ]
    assert setup.count("Acquire::Retries=2") == setup.count("DPkg::Lock::Timeout=30") == 2
    assert setup.count("Acquire::http::Timeout=30") == setup.count("Acquire::https::Timeout=30") == 2
    assert "install --yes apparmor bubblewrap libseccomp2" in setup
    assert 'test "$isolation_restriction" = 1' in setup
    assert 'test "$isolation_enforcement" = Y' in setup
    assert 'test "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" = "$isolation_restriction"' in setup
    assert 'test "$(cat /sys/module/apparmor/parameters/enabled)" = "$isolation_enforcement"' in setup
    profile = (ROOT / "ci/apparmor/exacly-bwrap").read_text()
    assert re.search(r"profile exacly-bwrap /usr/bin/bwrap flags=\(unconfined\) \{\s*userns,\s*\}", profile)
    assert "capability" not in profile


@pytest.mark.parametrize("failed_call", [1, 2])
@pytest.mark.parametrize("status", [1, 100, 124, 137])
def test_runtime_setup_propagates_admin_package_and_timeout_failures(
    tmp_path: Path, failed_call: int, status: int
) -> None:
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "calls.json"
    (commands / "cat").write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "  /proc/sys/kernel/apparmor_restrict_unprivileged_userns) echo 1;;\n"
        "  /sys/module/apparmor/parameters/enabled) echo Y;;\n"
        "  *) exit 99;;\nesac\n"
    )
    (commands / "sudo").write_text(
        "#!/usr/bin/python3\nimport json, os, pathlib, sys\n"
        "p=pathlib.Path(os.environ['EXACLY_SETUP_TEST_CALLS'])\n"
        "calls=json.loads(p.read_text()) if p.exists() else []\n"
        "calls.append(sys.argv[1:]); p.write_text(json.dumps(calls))\n"
        "sys.exit(int(os.environ['EXACLY_SETUP_TEST_STATUS']) "
        "if len(calls)==int(os.environ['EXACLY_SETUP_TEST_FAILURE']) else 0)\n"
    )
    for path in commands.iterdir():
        path.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "ci/setup_isolation_runtime.sh")],
        env={
            "PATH": f"{commands}:/usr/bin:/bin",
            "EXACLY_SETUP_TEST_CALLS": str(calls),
            "EXACLY_SETUP_TEST_STATUS": str(status),
            "EXACLY_SETUP_TEST_FAILURE": str(failed_call),
        },
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == status
    invoked = json.loads(calls.read_bytes())
    assert len(invoked) == failed_call
    assert all(arguments[0] == "-n" for arguments in invoked)
    assert "update" in invoked[0]
    assert all("/etc/apparmor.d/exacly-bwrap" not in arguments for arguments in invoked)


@pytest.mark.parametrize(
    "path", ["ci/check_isolation_runtime.py", "ci/setup_isolation_runtime.sh", "ci/apparmor/exacly-bwrap"]
)
def test_producer_approval_fingerprint_covers_runtime_setup_and_policy(path: str) -> None:
    profile = yaml.safe_load((ROOT / "ci/evidence-producer.yaml").read_text())
    selectors = admission.validate_observer_paths(profile["observer_paths"])
    assert any(path == selector or path.startswith(selector + "/") for selector in selectors)
    entries = [{"path": path, "type": "blob", "mode": "100644", "sha": "a" * 40}]
    approved = admission.observer_digest(entries, selectors)
    entries[0]["sha"] = "b" * 40
    assert admission.observer_digest(entries, selectors) != approved


@pytest.mark.parametrize(
    ("workflow", "job_name"),
    [
        ("spec.yml", "check"),
        ("spec.yml", "state"),
        ("verification-evidence.yml", "observe"),
        ("release.yml", "build"),
    ],
)
def test_full_verification_jobs_provision_and_check_the_real_isolation_runtime(workflow: str, job_name: str) -> None:
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = document["jobs"][job_name]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    setup = next(step for step in steps if step.get("name") == "Install the Linux isolation test runtime")
    preflight = next(step for step in steps if step.get("name") == "Check the Linux isolation test runtime")
    assert setup["run"] == "sh ci/setup_isolation_runtime.sh"
    assert preflight["run"] == "/usr/bin/python3 ci/check_isolation_runtime.py"
    check = next(
        step
        for step in steps
        if step.get("run", "").startswith(("mise run check", "mise run model", "uv run --locked pytest"))
    )
    assert steps.index(setup) < steps.index(preflight) < steps.index(check)


def test_completed_failures_are_packaged_and_each_attempt_has_one_artifact_name() -> None:
    steps = WORKFLOW["jobs"]["observe"]["steps"]
    package_step = next(step for step in steps if step.get("name") == "Package verification evidence")
    assert package_step["id"] == "evidence"
    assert package_step["if"] == (
        "always() && (steps.verification.outcome == 'success' || steps.verification.outcome == 'failure')"
    )
    assert package_step["env"] == {"EXACLY_TEST_OUTCOME": "${{ steps.verification.outcome }}"}
    assert package_step["run"] == (
        "uv run --locked exacly evidence package --report reports/junit.xml --out reports/evidence"
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


@pytest.mark.parametrize(
    "branch,workflow_path",
    [
        ("release/stable", ".github/workflows/quality.yaml"),
        ("release@stable", ".github/workflows/quality@ci.yaml"),
    ],
)
def test_publisher_accepts_another_repository_branch_workflow_and_job(
    tmp_path: Path, branch: str, workflow_path: str
) -> None:
    manifest, out = package(
        tmp_path,
        GITHUB_REPOSITORY="another-company/widget",
        GITHUB_REPOSITORY_ID="789",
        GITHUB_REF=f"refs/heads/{branch}",
        GITHUB_WORKFLOW_REF=f"another-company/widget/{workflow_path}@refs/heads/{branch}",
        GITHUB_JOB="audit",
    )
    assert manifest["repository"] == "another-company/widget"
    assert manifest["repository_id"] == 789
    assert manifest["workflow_path"] == workflow_path
    assert manifest["job"] == "audit"
    assert (out / "junit.xml").read_bytes() == PASSING


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
        {"GITHUB_JOB": ""},
        {"GITHUB_JOB": "job with spaces"},
        {"GITHUB_JOB": "123job"},
        {"GITHUB_JOB": "parent/child"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_REF": "refs/heads/other"},
        {"GITHUB_WORKFLOW_REF": "acme/example/other.yml@refs/heads/main"},
        {"GITHUB_WORKFLOW_REF": "other/example/.github/workflows/quality.yaml@refs/heads/main"},
        {"GITHUB_WORKFLOW_REF": "acme/example/.github/workflows/nested/quality.yaml@refs/heads/main"},
        {"GITHUB_WORKFLOW_REF": "acme/example/.github/workflows/quality.json@refs/heads/main"},
        {"GITHUB_WORKFLOW_REF": "acme/example/.github/workflows/quality.yaml@refs/heads/other"},
        {"EXACLY_TEST_OUTCOME": "cancelled"},
        {"EXACLY_TEST_OUTCOME": "skipped"},
        {"EXACLY_TEST_OUTCOME": ""},
    ],
)
def test_unsupported_producer_metadata_creates_no_output(tmp_path: Path, overrides: dict[str, str]) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "not-created" / "evidence"
    with pytest.raises(evidence.EvidenceError):
        evidence.package(report, out, environ=environment(**overrides), checkout=lambda: SHA)
    assert not out.parent.exists()
    assert report.read_bytes() == PASSING


@pytest.mark.parametrize(
    "ref",
    [
        "refs/tags/main",
        "refs/heads/",
        "refs/heads/release//stable",
        "refs/heads/release/../stable",
        "refs/heads/release/.hidden",
        "refs/heads/release.lock/stable",
        "refs/heads/release/stable.",
        "refs/heads/release@{stable",
        "refs/heads/release stable",
        "refs/heads/-release",
    ],
)
def test_invalid_branch_reference_is_rejected_even_when_workflow_reference_matches(tmp_path: Path, ref: str) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "evidence"
    with pytest.raises(evidence.EvidenceError, match="branch reference"):
        evidence.package(
            report,
            out,
            environ=environment(GITHUB_REF=ref, GITHUB_WORKFLOW_REF=f"acme/example/{WORKFLOW_PATH}@{ref}"),
            checkout=lambda: SHA,
        )
    assert not out.exists()


def test_wrong_actual_checkout_creates_no_output(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "evidence"
    with pytest.raises(evidence.EvidenceError, match="checkout"):
        evidence.package(report, out, environ=environment(), checkout=lambda: "b" * 40)
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
    with pytest.raises(evidence.EvidenceError):
        evidence.package(source, out, environ=environment(), checkout=lambda: SHA)
    assert not out.exists()


def test_existing_output_directory_is_not_overwritten(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(PASSING)
    out = tmp_path / "evidence"
    out.mkdir()
    sentinel = out / "keep"
    sentinel.write_text("existing output")
    with pytest.raises(evidence.EvidenceError, match="already exist"):
        evidence.package(report, out, environ=environment(), checkout=lambda: SHA)
    assert sentinel.read_text() == "existing output"
    assert list(out.iterdir()) == [sentinel]


def test_oversized_report_is_rejected_before_output_creation(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(b"x" * (evidence.MAX_REPORT_BYTES + 1))
    out = tmp_path / "evidence"
    with pytest.raises(evidence.EvidenceError, match="size"):
        evidence.package(report, out, environ=environment(), checkout=lambda: SHA)
    assert not out.exists()
