"""The installed producer command packages real checkout identity without making admission claims."""

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import NoReturn

import pytest
from exacly import admission, github_evidence
from exacly.cli import exacly_main

REPORT = b'<testsuite><testcase classname="billing" name="invoice_total"/></testsuite>'
WORKFLOW = ".github/workflows/verification.yml"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Producer test",
            "-c",
            "user.email=producer@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path, Path, str]:
    repo = tmp_path / "consumer"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "README.md").write_text("Consumer source under verification.\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "source")
    revision = git(repo, "rev-parse", "HEAD")
    report, out = repo / "test results.xml", repo / "evidence bundle"
    report.write_bytes(REPORT)
    monkeypatch.chdir(repo)
    for key, value in {
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_REPOSITORY": "acme/example",
        "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": revision,
        "GITHUB_JOB": "test",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": f"acme/example/{WORKFLOW}@refs/heads/main",
        "EXACLY_TEST_OUTCOME": "success",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return ["package", "github-actions", "--report", str(report), "--out", str(out)], report, out, revision


def test_json_manifest_matches_the_bundle_and_is_consumable_by_admission(
    producer: tuple[list[str], Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args, report, out, revision = producer

    def no_client(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Packaging must not construct a GitHub client")

    monkeypatch.setattr(github_evidence, "GitHubClient", no_client)
    assert exacly_main([*args, "--json"]) == 0
    printed = capsys.readouterr()
    assert not printed.err
    assert printed.out == (out / "manifest.json").read_text()
    manifest = json.loads(printed.out)
    assert manifest == {
        "schema_version": "0.1.0",
        "repository_id": 123,
        "repository": "acme/example",
        "run_id": 456,
        "run_attempt": 2,
        "workflow_path": WORKFLOW,
        "job": "test",
        "implementation_revision": revision,
        "report_digest": "sha256:" + hashlib.sha256(REPORT).hexdigest(),
        "check_outcome": "success",
    }
    assert (out / "junit.xml").read_bytes() == report.read_bytes() == REPORT
    assert {p.name for p in out.iterdir()} == {"manifest.json", "junit.xml"}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ("manifest.json", "junit.xml"):
            bundle.writestr(name, (out / name).read_bytes())
    assert admission.unpack(archive.getvalue()) == (manifest, REPORT)


def test_text_output_describes_preparation_without_an_admission_claim(
    producer: tuple[list[str], Path, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, out, _ = producer
    assert exacly_main(args) == 0
    printed = capsys.readouterr()
    assert printed.out == f"Prepared verification evidence in {out}\n"
    assert not printed.err


@pytest.mark.parametrize("outcome,child", [("failure", "<failure/>"), ("success", "<skipped/>")])
def test_completed_failures_and_skips_are_retained_as_report_bytes(
    producer: tuple[list[str], Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    child: str,
) -> None:
    args, report, out, _ = producer
    content = f'<testsuite><testcase name="case">{child}</testcase></testsuite>'.encode()
    report.write_bytes(content)
    monkeypatch.setenv("EXACLY_TEST_OUTCOME", outcome)
    assert exacly_main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["check_outcome"] == outcome
    assert (out / "junit.xml").read_bytes() == report.read_bytes() == content


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("GITHUB_SHA", "b" * 40, "checkout"),
        ("GITHUB_EVENT_NAME", "pull_request", "push"),
        ("GITHUB_REPOSITORY_ID", "0", "positive integer"),
        ("EXACLY_TEST_OUTCOME", "cancelled", "completed"),
    ],
)
def test_invalid_producer_inputs_return_one_without_creating_output(
    producer: tuple[list[str], Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
    reason: str,
) -> None:
    args, report, out, _ = producer
    monkeypatch.setenv(field, value)
    assert exacly_main(args) == 1
    printed = capsys.readouterr()
    assert not printed.out
    assert printed.err.startswith("exacly package: ") and reason in printed.err
    assert not out.exists()
    assert report.read_bytes() == REPORT


@pytest.mark.parametrize("destination", ["existing_directory", "directory_alias", "report_file"])
def test_existing_outputs_and_aliases_are_preserved(
    producer: tuple[list[str], Path, Path, str],
    capsys: pytest.CaptureFixture[str],
    destination: str,
) -> None:
    args, report, out, _ = producer
    if destination == "report_file":
        selected = report
    else:
        out.mkdir()
        (out / "keep.txt").write_text("Existing evidence\n")
        selected = out
        if destination == "directory_alias":
            selected = out.parent / "evidence alias"
            selected.symlink_to(out, target_is_directory=True)
    args[args.index("--out") + 1] = str(selected)
    assert exacly_main(args) == 1
    printed = capsys.readouterr()
    assert not printed.out and "already exist" in printed.err
    assert report.read_bytes() == REPORT
    if destination != "report_file":
        assert {p.name for p in out.iterdir()} == {"keep.txt"}
        assert (out / "keep.txt").read_text() == "Existing evidence\n"


def test_missing_report_returns_a_packaging_error_without_traceback(
    producer: tuple[list[str], Path, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    args, report, out, _ = producer
    report.unlink()
    assert exacly_main(args) == 1
    printed = capsys.readouterr()
    assert not printed.out and printed.err.startswith("exacly package: ")
    assert "Traceback" not in printed.err
    assert not out.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["package"],
        ["package", "github-actions"],
        ["package", "github-actions", "--report", "junit.xml"],
        ["package", "another-source", "--report", "junit.xml", "--out", "evidence"],
    ],
)
def test_missing_arguments_and_unknown_sources_use_argument_error_exit_status(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    printed = capsys.readouterr()
    assert not printed.out and "usage: exacly" in printed.err
