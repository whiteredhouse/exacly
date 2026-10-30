"""Optional readiness XML preserves native proof and never publishes a successful subset."""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from exacly import composition, composition_io, evidence_retention
from exacly.cli import exacly_main
from exacly.refresh import case_id, read_junit
from test_admission import BASE, Source
from test_composition_cli import IDENTIFIERS, backend, scenario


def verdicts(root: ET.Element) -> dict[str, ET.Element]:
    return {
        identifier: case
        for identifier in IDENTIFIERS
        for case in root.iter("testcase")
        if case_id(case) == f"exacly.composition/v1/native-release/{identifier}/verdict"
    }


@pytest.mark.parametrize("requested", [False, True])
def test_readiness_junit_is_optional_and_preserves_native_results_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], requested: bool
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    if requested:
        args.append("--readiness-junit")
    original = composition.evaluate
    evaluations: list[dict[str, Any]] = []

    def evaluate(*args: Any, **kwargs: Any) -> composition.CompositionResult:
        assert kwargs["retain_admissions"] is False
        result = original(*args, **kwargs)
        evaluations.append(copy.deepcopy(result.report))
        return result

    monkeypatch.setattr(composition, "evaluate", evaluate)
    assert exacly_main(args) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert evaluations == [report]
    assert report == json.loads((output / "report.json").read_text())
    assert report["complete"] is True and report["result"] == "satisfied"
    assert not captured.err and source.index == 3
    for member, native in zip(report["members"], source.sources, strict=True):
        folder = output / "members" / member["id"]
        assert (folder / "junit.xml").read_bytes() == native.report
        assert (folder / "archive.zip").read_bytes() == native.archive
        assert json.loads((folder / "gate.json").read_text()) == member["gate"]
    xml = output / "readiness.xml"
    assert xml.exists() is requested
    if requested:
        root = ET.parse(xml).getroot()
        assert set(verdicts(root)) == set(IDENTIFIERS)
        assert root.attrib["failures"] == root.attrib["errors"] == root.attrib["skipped"] == "0"
        assert len(list(root.iter("testcase"))) == int(root.attrib["tests"]) == 8
        assert len(read_junit([xml])) == 8
        assert set(read_junit([xml]).values()) == {"passed"}


@pytest.mark.parametrize("outcome", ["failure", "unknown", "rejected"])
def test_blocked_readiness_xml_keeps_every_required_member_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], outcome: str
) -> None:
    blocked = Source("failure")
    if outcome == "unknown":
        blocked = Source(
            report=(
                b'<testsuite><testcase file="python/tests/test_requirement.py" '
                b'name="test_requirement"><skipped/></testcase></testsuite>'
            )
        )
    elif outcome == "rejected":
        blocked = Source()
        blocked.run["head_sha"] = "f" * 40
    args, _, output, source = scenario(tmp_path, [Source(), blocked, Source(), Source()])
    backend(monkeypatch, source)
    assert exacly_main([*args, "--readiness-junit"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is True and report["result"] == "blocked"
    xml = output / "readiness.xml"
    root = ET.parse(xml).getroot()
    members = verdicts(root)
    assert set(members) == set(IDENTIFIERS)
    assert members[IDENTIFIERS[1]].find("failure") is not None
    assert all(
        members[identifier].find("failure") is None for identifier in IDENTIFIERS if identifier != IDENTIFIERS[1]
    )
    assert root.attrib["errors"] == root.attrib["skipped"] == "0"
    assert int(root.attrib["failures"]) == (1 if outcome == "rejected" else 2)
    observed = read_junit([xml])
    assert observed[f"exacly.composition/v1/native-release/{IDENTIFIERS[1]}/verdict"] == "failed"
    if outcome == "rejected":
        assert len(observed) == 7
        assert report["members"][1]["gate"] is None
        assert {path.name for path in (output / "members" / IDENTIFIERS[1]).iterdir()} == {"receipt.json"}
    else:
        assert len(observed) == 8
        assert report["members"][1]["gate"]["result"] == ("unknown" if outcome == "unknown" else "not_satisfied")
        assert (output / "members" / IDENTIFIERS[1] / "junit.xml").read_bytes() == blocked.report


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o777])
def test_requested_readiness_xml_is_private_independently_of_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mask: int
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    previous = os.umask(mask)
    try:
        assert exacly_main([*args, "--readiness-junit"]) == 0
    finally:
        os.umask(previous)
    assert (output / "readiness.xml").is_file()
    for path in (output, *output.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize("failure", ["render", "write", "file-sync", "replace", "directory-sync", "aggregate"])
def test_requested_readiness_failure_leaves_an_incomplete_bundle_and_no_passing_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    reached: list[str] = []
    original_temp = tempfile.NamedTemporaryFile
    original_fsync = os.fsync
    original_replace = os.replace
    original_sync = evidence_retention.sync_directory
    original_report = composition_io._replace_report
    readiness_descriptors: set[int] = set()

    def unavailable_renderer(report: dict[str, Any]) -> str:
        reached.append("render")
        raise ValueError("simulated readiness rendering failure")

    def failed_write(data: bytes) -> int:
        reached.append("write")
        raise OSError("simulated readiness write failure")

    def temporary(*args: Any, **kwargs: Any) -> Any:
        stream = original_temp(*args, **kwargs)
        if str(kwargs.get("prefix", "")).startswith(".readiness-"):
            readiness_descriptors.add(stream.fileno())
            if failure == "write":
                monkeypatch.setattr(stream, "write", failed_write)
        return stream

    def fsync(descriptor: int) -> None:
        if failure == "file-sync" and descriptor in readiness_descriptors:
            reached.append("file-sync")
            raise OSError("simulated readiness fsync failure")
        original_fsync(descriptor)

    def replace(source: Any, destination: Any) -> None:
        if failure == "replace" and Path(destination) == output / "readiness.xml":
            reached.append("replace")
            raise OSError("simulated readiness replacement failure")
        original_replace(source, destination)

    def sync(directory: Path) -> None:
        if failure == "directory-sync" and directory == output and (output / "readiness.xml").exists():
            reached.append("directory-sync")
            raise OSError("simulated readiness directory fsync failure")
        original_sync(directory)

    def publish(directory: Path, report: dict[str, Any]) -> None:
        if failure == "aggregate" and report["complete"]:
            assert (output / "readiness.xml").is_file()
            reached.append("aggregate")
            raise OSError("simulated complete aggregate replacement failure")
        original_report(directory, report)

    if failure == "render":
        monkeypatch.setattr(composition, "as_junit", unavailable_renderer)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(evidence_retention, "sync_directory", sync)
    monkeypatch.setattr(composition_io, "_replace_report", publish)
    assert exacly_main([*args, "--readiness-junit"]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "simulated" in captured.err
    assert reached == [failure]
    aggregate = json.loads((output / "report.json").read_text())
    assert aggregate["complete"] is False and aggregate["result"] == "blocked"
    assert len(aggregate["members"]) == 4 and source.index == 3
    assert not (output / "readiness.xml").exists()
    assert not list(output.glob(".readiness-*"))
    assert not list(output.glob(".report-*"))
    assert all((output / "members" / identifier / "gate.json").is_file() for identifier in IDENTIFIERS)


def test_interrupted_admission_never_emits_a_passing_partial_readiness_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = source.get_json

    def interrupted(path: str) -> dict[str, Any]:
        if path.strip("/") == BASE and source.index == 0:
            raise KeyboardInterrupt
        return original(path)

    monkeypatch.setattr(source, "get_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        exacly_main([*args, "--readiness-junit"])
    aggregate = json.loads((output / "report.json").read_text())
    assert aggregate["complete"] is False and aggregate["result"] == "blocked"
    assert [member["id"] for member in aggregate["members"]] == [IDENTIFIERS[0]]
    assert (output / "members" / IDENTIFIERS[0] / "gate.json").is_file()
    assert not (output / "readiness.xml").exists()


def test_interruption_after_xml_publication_removes_it_and_keeps_aggregate_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = composition_io._replace_report

    def interrupted(directory: Path, report: dict[str, Any]) -> None:
        if report["complete"]:
            assert (output / "readiness.xml").is_file()
            raise KeyboardInterrupt
        original(directory, report)

    monkeypatch.setattr(composition_io, "_replace_report", interrupted)
    with pytest.raises(KeyboardInterrupt):
        exacly_main([*args, "--readiness-junit"])
    aggregate = json.loads((output / "report.json").read_text())
    assert aggregate["complete"] is False and aggregate["result"] == "blocked"
    assert len(aggregate["members"]) == 4 and source.index == 3
    assert not (output / "readiness.xml").exists()


def test_interruption_after_aggregate_commit_preserves_the_complete_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = os.replace

    def committed_then_interrupted(temporary: Any, destination: Any) -> None:
        final = (
            Path(destination) == output / "report.json" and json.loads(Path(temporary).read_text())["complete"] is True
        )
        original(temporary, destination)
        if final:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", committed_then_interrupted)
    with pytest.raises(KeyboardInterrupt):
        exacly_main([*args, "--readiness-junit"])
    aggregate = json.loads((output / "report.json").read_text())
    assert aggregate["complete"] is True and aggregate["result"] == "satisfied"
    assert len(aggregate["members"]) == 4 and source.index == 3
    xml = output / "readiness.xml"
    assert xml.is_file()
    assert len(read_junit([xml])) == 8 and set(read_junit([xml]).values()) == {"passed"}


@pytest.mark.parametrize("cleanup_failure", ["unlink", "marker-read"])
def test_cleanup_failure_preserves_the_original_output_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cleanup_failure: str
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original_publish = composition_io._replace_report
    original_unlink = Path.unlink
    original_read = Path.read_bytes
    failed: list[bool] = []
    cleanup_attempts: list[str] = []

    def publish(directory: Path, report: dict[str, Any]) -> None:
        if report["complete"]:
            assert (output / "readiness.xml").is_file()
            failed.append(True)
            raise OSError("original aggregate publication failure")
        original_publish(directory, report)

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if failed and cleanup_failure == "unlink" and path == output / "readiness.xml":
            cleanup_attempts.append("unlink")
            raise PermissionError("secondary readiness cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    def read(path: Path) -> bytes:
        if failed and cleanup_failure == "marker-read" and path == output / "report.json":
            cleanup_attempts.append("marker-read")
            raise OSError("secondary canonical marker read failure")
        return original_read(path)

    monkeypatch.setattr(composition_io, "_replace_report", publish)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "read_bytes", read)
    assert exacly_main([*args, "--readiness-junit"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "original aggregate publication failure" in captured.err and "secondary" not in captured.err
    assert failed == [True] and cleanup_attempts == [cleanup_failure]
    aggregate = json.loads((output / "report.json").read_text())
    assert aggregate["complete"] is False and aggregate["result"] == "blocked"
    assert len(aggregate["members"]) == 4 and source.index == 3
    assert (output / "readiness.xml").is_file()
