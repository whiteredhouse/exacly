"""Checks exacly-refresh: test results and bindings in, state that the validator accepts out.

Run with `mise run test`, or from the repository root: uv run pytest validator/tests
"""

import contextlib
import copy
import datetime as dt
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from exacly_validate import validate_documents
from exacly_validate.cli import refresh_main
from exacly_validate.refresh import RefreshError, Run, read_junit, refresh

ROOT = Path(__file__).resolve().parents[2]
WHEN = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
RUN = Run(
    revision="0b6ad3b",
    context="main",
    observed_at=WHEN,
    workflow=".github/workflows/ci.yml",
    job="check",
    job_status="success",
    url="https://ci.example/runs/7",
)
CI = {"workflow": ".github/workflows/ci.yml", "job": "check"}


def requirement(rid: str, tests: dict[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": rid,
        "type": "Requirement",
        "name": f"Requirement {rid}",
        "description": "A requirement.",
        "owner": "team",
        "lifecycle": "active",
        "evidence": [],
        "last_reviewed_at": "2026-10-20",
        "statement": "Do the thing.",
        "acceptance_criteria": ["The thing is done."],
        "verification_method": "Tests.",
    }
    if tests is not None:
        record["observe"] = {"tests": tests}
    return record


def config(*records: dict[str, Any], app_ref: str = "validator/src/exacly_validate") -> dict[str, Any]:
    """A small model: an Outcome, a Capability, a Process, an Application, and the given Requirements."""
    common = {"owner": "team", "lifecycle": "active", "evidence": [], "last_reviewed_at": "2026-10-20"}
    base = [
        {"id": "OUT-1", "type": "Outcome", "name": "O", "description": "O.", **common, "beneficiary": "Teams",
         "success_criterion": "Done", "evaluation_period": "Quarterly"},
        {"id": "MEAS-1", "type": "Measure", "name": "M", "description": "M.", **common, "lifecycle": "proposed"},
        {"id": "CAP-1", "type": "Capability", "name": "C", "description": "C.", **common, "scope": "All."},
        {"id": "PROC-1", "type": "Process", "name": "P", "description": "P.", **common, "perspective": "business",
         "actors": ["Team"], "trigger": "Start", "steps": ["Work"], "completion": "Done", "exceptions": []},
        {"id": "APP-1", "type": "Application", "name": "App", "description": "A.", **common,
         "responsibility": "Work.", "interfaces": ["CLI"], "implementation_ref": {"locator": app_ref}},
    ]  # fmt: skip
    relationships = [
        {"source": "MEAS-1", "type": "evaluates", "target": "OUT-1"},
        {"source": "CAP-1", "type": "contributes_to", "target": "OUT-1"},
        {"source": "PROC-1", "type": "uses_capability", "target": "CAP-1"},
        {"source": "PROC-1", "type": "uses", "target": "APP-1"},
    ]
    for r in records:
        relationships += [
            {"source": r["id"], "type": "supports", "target": "PROC-1"},
            {"source": "APP-1", "type": "implements", "target": r["id"]},
        ]
    model = {
        "namespace": "demo",
        "product": "Demo",
        "owner": "team",
        "scope": {"covers": "Everything", "excludes": []},
        "environment": "main",
        "review_interval_days": 90,
    }
    return {"exacly": "0.2.0", "model": model, "records": [*base, *records], "relationships": relationships}


RESULTS = {
    "tests/cases.yaml::7.1: a gap": "passed",
    "tests/cases.yaml::7.2: another gap": "passed",
    "tests/cases.yaml::7.3: skipped one": "skipped",
    "tests/cases.yaml::schema: broken": "failed",
    "tests/test_cli.py::test_exit_codes": "passed",
}


def assessment(state: dict[str, Any], rid: str, dimension: str = "verification") -> dict[str, Any]:
    found: list[dict[str, Any]] = [a for a in state["records"][rid]["assessments"] if a["dimension"] == dimension]
    assert len(found) == 1, state["records"][rid]
    return found[0]


def conforms(docs: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    report = validate_documents({**docs, "state": state}, WHEN.date(), RUN.revision).as_dict()
    assert report["errors"] == [], report["errors"]
    return report


def test_selected_cases_that_all_pass_verify_the_record() -> None:
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": ["tests/cases.yaml::7.*"]}))}
    state = refresh(docs, RESULTS, RUN)
    a = assessment(state, "REQ-1")
    assert (a["state"], a["context"], a["at"]) == ("passed", "0b6ad3b", "2026-10-24T09:00:00Z")
    assert a["evidence"] == [
        {
            "locator": "https://ci.example/runs/7",
            "kind": "test",
            "observed_at": "2026-10-24",
            "applies_to": "0b6ad3b",
            "claim": "All 2 selected test cases passed; 1 skipped",
        }
    ]
    assert "reason" not in a
    report = validate_documents({**docs, "state": state}, WHEN.date(), RUN.revision).as_dict()
    # Verified without a release is a 7.8 error: delivery needs its own evidence.
    assert [(f["rule"], f["record"]) for f in report["errors"]] == [("7.8", "REQ-1")]


def test_a_failing_case_fails_the_record_and_names_it() -> None:
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": ["tests/cases.yaml::*"]}))}
    a = assessment(refresh(docs, RESULTS, RUN), "REQ-1")
    assert a["state"] == "failed"
    assert a["reason"] == "Failed: tests/cases.yaml::schema: broken."
    assert a["evidence"][0]["claim"] == "1 of 3 selected test cases failed"


def test_many_failures_are_counted_rather_than_all_named() -> None:
    results = {f"t::case {i}": "failed" for i in range(8)}
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": ["t::*"]}))}
    assert assessment(refresh(docs, results, RUN), "REQ-1")["reason"].endswith(" and 3 more.")


@pytest.mark.parametrize(
    ("cases", "reason"),
    [
        (["tests/nothing::*"], "The binding selects no test case that ran."),
        (["tests/cases.yaml::7.3*"], "Every selected test case was skipped."),
    ],
)
def test_nothing_run_is_inconclusive_with_a_reason(cases: list[str], reason: str) -> None:
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": cases}))}
    a = assessment(refresh(docs, RESULTS, RUN), "REQ-1")
    assert (a["state"], a["reason"], a["evidence"]) == ("inconclusive", reason, [])


@pytest.mark.parametrize(
    ("status", "expected"),
    [("success", "passed"), ("failure", "failed"), ("cancelled", "failed"), (None, "inconclusive")],
)
def test_a_binding_without_cases_uses_the_job_result(status: str | None, expected: str) -> None:
    docs = {"config": config(requirement("REQ-1", CI))}
    run = Run(**{**RUN.__dict__, "job_status": status})
    a = assessment(refresh(docs, RESULTS, run), "REQ-1")
    assert a["state"] == expected
    assert ("reason" in a) == (expected != "passed")


def test_a_record_without_a_binding_gets_no_verification() -> None:
    docs = {"config": config(requirement("REQ-1"))}
    assert refresh(docs, RESULTS, RUN)["records"] == {}


def test_a_binding_for_another_workflow_is_not_observed_and_keeps_its_earlier_entry() -> None:
    docs = {"config": config(requirement("REQ-1", {**CI, "workflow": ".github/workflows/nightly.yml"}))}
    earlier = refresh({"config": config(requirement("REQ-1", CI))}, RESULTS, RUN)
    later = refresh(docs, RESULTS, Run(**{**RUN.__dict__, "revision": "1c2d3e4"}), previous=earlier)
    assert later["records"] == earlier["records"]
    assert assessment(later, "REQ-1")["context"] == "0b6ad3b"


def test_each_write_increases_the_serial_and_keeps_the_lineage() -> None:
    docs = {"config": config(requirement("REQ-1", CI))}
    first = refresh(docs, RESULTS, RUN)
    first["pending"] = [{"record": "REQ-1", "task": "issues/1", "opened_at": "2026-10-23T10:00:00Z"}]
    before = copy.deepcopy(first)
    second = refresh(docs, RESULTS, RUN, previous=first)
    assert first == before, "refresh must not change the earlier state"
    assert (first["serial"], second["serial"]) == (1, 2)
    assert second["lineage"] == first["lineage"]
    assert second["pending"] == first["pending"]
    assert refresh(docs, RESULTS, RUN)["lineage"] != first["lineage"]


@pytest.mark.parametrize(("key", "value"), [("namespace", "other"), ("context", "production")])
def test_earlier_state_for_another_model_or_context_is_refused(key: str, value: str) -> None:
    docs = {"config": config(requirement("REQ-1", CI))}
    earlier = {**refresh(docs, RESULTS, RUN), key: value}
    with pytest.raises(RefreshError):
        refresh(docs, RESULTS, RUN, previous=earlier)


def test_records_removed_from_config_leave_state() -> None:
    earlier = refresh({"config": config(requirement("REQ-1", CI), requirement("REQ-2", CI))}, RESULTS, RUN)
    later = refresh({"config": config(requirement("REQ-1", CI))}, RESULTS, RUN, previous=earlier)
    assert list(later["records"]) == ["REQ-1"]


def test_a_distribution_context_releases_requirements_whose_implementer_exists() -> None:
    docs = {"config": config(requirement("REQ-1", CI), requirement("REQ-2"))}
    state = refresh(docs, RESULTS, RUN, distribution=True, root=ROOT)
    released = assessment(state, "REQ-2", "delivery")
    assert (released["state"], released["context"]) == ("released", "main")
    assert released["evidence"][0]["kind"] == "implementation"
    assert released["evidence"][0]["applies_to"] == "main"
    assert "reason" not in released
    conforms(docs, state)


@pytest.mark.parametrize("ref", ["validator/src/missing", "validator/src/exacly_validate"])
def test_no_release_without_distribution_or_an_existing_reference(ref: str) -> None:
    docs = {"config": config(requirement("REQ-1"), app_ref=ref)}
    distribution = ref.endswith("missing")
    assert refresh(docs, RESULTS, RUN, distribution=distribution, root=ROOT)["records"] == {}


def test_junit_ids_come_from_the_case_property_or_file_and_name() -> None:
    report = """<testsuites><testsuite>
      <testcase classname="tests.test_x" name="test_case[0]" file="tests/test_x.py">
        <properties><property name="exacly-case" value="tests/cases.yaml::A case"/></properties>
      </testcase>
      <testcase classname="tests.test_x" name="test_cli" file="tests/test_x.py"><failure message="no"/></testcase>
      <testcase classname="tests.test_x" name="test_skip" file="tests/test_x.py"><skipped/></testcase>
      <testcase classname="tests.test_x" name="test_cli" file="tests/test_x.py"/>
      <testcase classname="tests.test_y" name="test_error"><error message="boom"/></testcase>
    </testsuite></testsuites>"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "junit.xml"
        path.write_text(report)
        assert read_junit([path]) == {
            "tests/cases.yaml::A case": "passed",
            "tests/test_x.py::test_cli": "failed",
            "tests/test_x.py::test_skip": "skipped",
            "tests.test_y::test_error": "failed",
        }


def test_cli_writes_state_the_validator_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/demo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_SHA", "0b6ad3b")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "acme/demo/.github/workflows/ci.yml@refs/heads/main")
    monkeypatch.setenv("GITHUB_JOB", "check")
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": ["tests/cases.yaml::7.*"]}))}
    junit = """<testsuites><testsuite><testcase name="test_case" file="t.py">
      <properties><property name="exacly-case" value="tests/cases.yaml::7.1: a gap"/></properties>
    </testcase></testsuite></testsuites>"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "model.json").write_text(json.dumps(docs["config"]))
        (folder / "junit.xml").write_text(junit)
        state_path = folder / "state" / "main.json"
        args = [str(folder / "model.json"), "--context", "main", "--state", str(state_path)]
        args += ["--junit", str(folder / "junit.xml"), "--distribution", "--root", str(ROOT)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert refresh_main(args) == 0
            assert refresh_main(args) == 0
        assert "serial 2" in out.getvalue()
        state = json.loads(state_path.read_text())
    assert assessment(state, "REQ-1")["evidence"][0]["locator"] == "https://github.com/acme/demo/actions/runs/42"
    released = assessment(state, "REQ-1", "delivery")["evidence"][0]["locator"]
    assert released == "https://github.com/acme/demo/blob/0b6ad3b/validator/src/exacly_validate"
    conforms(docs, state)
