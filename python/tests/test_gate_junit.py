"""Readiness XML is an optional artifact of one unchanged, read-only gate decision."""

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from exacly import policy
from exacly.cli import exacly_main
from exacly.refresh import case_id, read_junit, refresh
from test_applicability import WHEN, A, B, documents, target
from test_policy import POLICY
from test_refresh import ROOT, RUN


def inputs(tmp_path: Path, docs: dict[str, Any] | None = None) -> tuple[list[str], Path, Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    for name, document in (documents() if docs is None else docs).items():
        (model / f"{name}.json").write_text(json.dumps(document))
    rule, selection = tmp_path / "policy.json", tmp_path / "target.json"
    rule.write_text(json.dumps(POLICY))
    selection.write_text(json.dumps(target().data))
    args = ["gate", "-m", str(model), "--policy", str(rule), "--target", str(selection), "--date", str(WHEN)]
    return args, model, rule, selection


def report_args(path: Path, label: str = "release-linux") -> list[str]:
    return ["--junit", str(path), "--junit-id", label]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(("state", "code"), [("passed", 0), ("failed", 1), ("not_checked", 1)])
def test_xml_preserves_stdout_exit_and_inputs_and_evaluates_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
    state: str,
    code: int,
) -> None:
    docs = documents()
    assessment = docs["state"]["records"]["REQ-1"]["assessments"][0]
    assessment.update(state=state, reason="Native readiness reason <&>")
    args, _, _, _ = inputs(tmp_path, docs)
    if json_output:
        args.append("--json")
    assert exacly_main(args) == code
    baseline = capsys.readouterr()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    calls = []
    evaluate = policy.evaluate

    def counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(True)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(policy, "evaluate", counted)
    output = tmp_path / "reports" / "readiness.xml"
    assert exacly_main([*args, *report_args(output)]) == code
    assert capsys.readouterr() == baseline
    assert calls == [True]
    assert all(p.read_bytes() == content for p, content in before.items())
    suite = ET.parse(output).getroot().find("testsuite")
    assert suite is not None
    cases = list(suite.iter("testcase"))
    assert len(cases) == 2
    assert int(suite.get("tests", "-1")) == 2
    assert int(suite.get("failures", "-1")) == (0 if code == 0 else 1)
    assert suite.get("errors") == "0" and suite.get("skipped") == "0"
    assert len(read_junit([output])) == 2
    assert set(read_junit([output]).values()) == ({"passed"} if code == 0 else {"passed", "failed"})
    if state == "failed":
        assert any("Native readiness reason <&>" in "".join(f.itertext()) for f in suite.iter("failure"))


@pytest.mark.parametrize("kind", ["missing", "skipped", "incomplete", "expired", "mismatched"])
def test_unknown_evidence_stays_blocking_in_xml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    docs = documents()
    if kind == "missing":
        docs.pop("state")
    elif kind in {"skipped", "incomplete"}:
        results = {"unit::test": "skipped"}
        if kind == "incomplete":
            docs["config"]["records"][-1]["observe"]["tests"]["cases"] = ["unit::*"]
            results = {"unit::test": "passed", "unit::skipped": "skipped"}
        docs["state"] = refresh(
            {"config": docs["config"]},
            results,
            replace(RUN, revision=A, workflow="ci", job="unit"),
            distribution=True,
            root=ROOT,
        )
    elif kind == "expired":
        docs["state"]["records"]["REQ-1"]["assessments"][0]["evidence"][0]["expires_at"] = "2026-10-25"
    args, _, _, selection = inputs(tmp_path, docs)
    if kind == "mismatched":
        selection.write_text(json.dumps(target(revision=B).data))
    output = tmp_path / "gate.xml"
    assert exacly_main([*args, "--json", *report_args(output)]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["result"] == "unknown"
    xml = ET.parse(output).getroot()
    assert not list(xml.iter("skipped"))
    assert any("unknown" in failure.get("type", "") for failure in xml.iter("failure"))
    assert "failed" in read_junit([output]).values()


def test_nonconforming_readable_model_remains_unknown_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    docs = documents()
    docs["config"]["relationships"] = [r for r in docs["config"]["relationships"] if r["type"] != "implements"]
    args, _, _, _ = inputs(tmp_path, docs)
    output = tmp_path / "gate.xml"
    assert exacly_main([*args, "--json", *report_args(output)]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["result"] == "unknown" and answer["findings"]["errors"]
    assert all(value == "failed" for value in read_junit([output]).values())
    assert "the model does not conform" in output.read_text()


@pytest.mark.parametrize("bad", ["policy", "target", "schema", "syntax", "unreadable", "label"])
def test_invocation_error_does_not_create_a_successful_or_empty_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad: str,
) -> None:
    args, model, rule, selection = inputs(tmp_path)
    if bad == "policy":
        rule.write_text("{}")
    elif bad == "target":
        selection.write_text("{}")
    elif bad == "schema":
        (model / "config.json").write_text("{}")
    elif bad == "syntax":
        rule.write_text("[broken")
    elif bad == "unreadable":
        rule.unlink()
    output = tmp_path / "reports" / "gate.xml"
    assert exacly_main([*args, *report_args(output, " " if bad == "label" else "release-linux")]) == 2
    shown = capsys.readouterr()
    assert not shown.out and "exacly gate:" in shown.err
    assert not output.parent.exists()


@pytest.mark.parametrize("option", ["path", "id"])
def test_report_path_and_stable_logical_id_are_paired(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    args, _, _, _ = inputs(tmp_path)
    extra = ["--junit", str(tmp_path / "gate.xml")] if option == "path" else ["--junit-id", "release"]
    assert exacly_main([*args, *extra]) == 2
    shown = capsys.readouterr()
    assert not shown.out and "supplied together" in shown.err


@pytest.mark.parametrize(
    "which",
    ["config", "state", "policy", "target", "symlink", "hardlink", "parent_alias", "nested", "glob"],
)
def test_xml_output_protects_inputs_and_model_folders(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    which: str,
) -> None:
    args, model, rule, selection = inputs(tmp_path)
    if which in {"symlink", "hardlink"}:
        output = tmp_path / "alias.xml"
        if which == "symlink":
            output.symlink_to(rule)
        else:
            os.link(rule, output)
    elif which == "parent_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(model, target_is_directory=True)
        output = alias / "gate.xml"
    elif which in {"nested", "glob"}:
        output = model / "reports" / "gate.xml"
        if which == "glob":
            args[2] = str(model / "*.json")
    else:
        protected = {
            "config": model / "config.json",
            "state": model / "state.json",
            "policy": rule,
            "target": selection,
        }
        output = protected[which]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert exacly_main([*args, *report_args(output)]) == 2
    shown = capsys.readouterr()
    assert not shown.out and "--junit" in shown.err
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("which", ["directory", "parent_file", "symlink_loop"])
def test_output_path_failure_is_visible_without_success_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    which: str,
) -> None:
    args, _, _, _ = inputs(tmp_path)
    output = tmp_path / "gate.xml"
    if which == "directory":
        output.mkdir()
    elif which == "parent_file":
        output.write_text("keep")
        output = output / "child.xml"
    else:
        output.symlink_to(output.name)
    assert exacly_main([*args, *report_args(output)]) == 2
    shown = capsys.readouterr()
    assert not shown.out and "exacly gate:" in shown.err and "Traceback" not in shown.err


def test_atomic_write_failure_preserves_old_report_and_cleans_temporary_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, _, _ = inputs(tmp_path)
    output = tmp_path / "reports" / "gate.xml"
    output.parent.mkdir()
    output.write_text("previous report")

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("report publication denied")

    monkeypatch.setattr("exacly.cli.os.replace", denied)
    assert exacly_main([*args, *report_args(output)]) == 2
    shown = capsys.readouterr()
    assert not shown.out and "report publication denied" in shown.err
    assert output.read_text() == "previous report"
    assert list(output.parent.iterdir()) == [output]


def test_explicit_target_ids_keep_combined_reports_distinct(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args, _, _, selection = inputs(tmp_path)
    first, second = tmp_path / "first.xml", tmp_path / "second.xml"
    assert exacly_main([*args, *report_args(first, "release-first")]) == 0
    selection.write_text(json.dumps(target(revision=B).data))
    assert exacly_main([*args, *report_args(second, "release-second")]) == 1
    capsys.readouterr()
    first_ids = {case_id(c) for c in ET.parse(first).getroot().iter("testcase")}
    second_ids = {case_id(c) for c in ET.parse(second).getroot().iter("testcase")}
    assert first_ids.isdisjoint(second_ids)
    combined = read_junit([first, second])
    assert len(combined) == 4 and set(combined.values()) == {"passed", "failed"}


def test_no_xml_on_invalid_invocation_leaves_existing_artifact_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args, _, rule, _ = inputs(tmp_path)
    output = tmp_path / "gate.xml"
    assert exacly_main([*args, *report_args(output)]) == 0
    before = output.read_bytes()
    rule.write_text("{}")
    assert exacly_main([*args, *report_args(output)]) == 2
    assert output.read_bytes() == before
    assert "exacly gate:" in capsys.readouterr().err
