"""Checks exacly refresh: test results and bindings in, state that the validator accepts out.

Run with `mise run test`, or from the repository root: uv run pytest python/tests
"""

import contextlib
import copy
import datetime as dt
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from exacly import query, validate_documents
from exacly.cli import refresh_main
from exacly.core import schema_for
from exacly.refresh import RefreshError, Run, read_junit, refresh

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


def config(*records: dict[str, Any], app_ref: str = "python/src/exacly") -> dict[str, Any]:
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


def valid_state(state: dict[str, Any]) -> dict[str, Any]:
    """The state, after checking it against the state schema."""
    jsonschema.validate(state, schema_for(state["exacly"], "state"), format_checker=jsonschema.FormatChecker())
    return state


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
    a = assessment(valid_state(refresh(docs, RESULTS, RUN)), "REQ-1")
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
        (["tests/nothing::*"], "The binding selects no test case that ran. Selected nothing: tests/nothing::*."),
        (["tests/cases.yaml::7.3*"], "Every selected test case was skipped."),
        (
            ["tests/cases.yaml::7.3*", "tests/gone"],
            "Every selected test case was skipped. Selected nothing: tests/gone.",
        ),
    ],
)
def test_nothing_run_is_not_checked_with_a_reason(cases: list[str], reason: str) -> None:
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": cases}))}
    a = assessment(valid_state(refresh(docs, RESULTS, RUN)), "REQ-1")
    assert (a["state"], a["reason"], a["evidence"]) == ("not_checked", reason, [])


@pytest.mark.parametrize(
    ("cases", "state", "reason"),
    [
        (
            ["tests/cases.yaml::7.1: a gap", "tests/cases.yaml::7.9: renamed"],
            "inconclusive",
            "Selected nothing: tests/cases.yaml::7.9: renamed.",
        ),
        (
            ["tests/cases.yaml::schema: broken", "tests/cases.yaml::7.9: renamed"],
            "failed",
            "Failed: tests/cases.yaml::schema: broken. Selected nothing: tests/cases.yaml::7.9: renamed.",
        ),
        (["tests/cases.yaml::7.1*", "tests/old/*"], "inconclusive", "Selected nothing: tests/old/*."),
        (["tests/cases.yaml::7.1: a gap", "tests/cases.yaml::7.2: another gap"], "passed", None),
        (["tests/cases.yaml::7.1: a gap", "tests/cases.yaml::7.3: skipped one"], "passed", None),
    ],
)
def test_every_pattern_must_select_a_case(cases: list[str], state: str, reason: str | None) -> None:
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": cases}))}
    a = assessment(valid_state(refresh(docs, RESULTS, RUN)), "REQ-1")
    assert (a["state"], a.get("reason")) == (state, reason)
    assert len(a["evidence"]) == 1


def test_many_patterns_that_select_nothing_are_counted() -> None:
    cases = ["t::kept", *(f"t::gone {i}" for i in range(7))]
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": cases}))}
    a = assessment(refresh(docs, {"t::kept": "passed"}, RUN), "REQ-1")
    assert (a["state"], a["reason"]) == ("inconclusive", "Selected nothing: " + ", ".join(cases[1:6]) + " and 2 more.")


def test_gaps_lists_an_inconclusive_requirement_as_unverified() -> None:
    cases = ["tests/cases.yaml::7.1: a gap", "tests/cases.yaml::7.9: renamed"]
    docs = {"config": config(requirement("REQ-1", {**CI, "cases": cases}))}
    state = refresh(docs, RESULTS, RUN)
    unverified = query.gaps(query.load({**docs, "state": state}, WHEN.date(), RUN.revision))["coverage"]["unverified"]
    assert [(r["id"], r["verification"]) for r in unverified] == [("REQ-1", {"main": "inconclusive"})]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("success", "passed"), ("failure", "failed"), ("cancelled", "failed"), (None, "not_checked")],
)
def test_a_binding_without_cases_uses_the_job_result(status: str | None, expected: str) -> None:
    docs = {"config": config(requirement("REQ-1", CI))}
    run = Run(**{**RUN.__dict__, "job_status": status})
    a = assessment(valid_state(refresh(docs, RESULTS, run)), "REQ-1")
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


@pytest.mark.parametrize("ref", ["python/src/missing", "python/src/exacly"])
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
    assert released == "https://github.com/acme/demo/blob/0b6ad3b/python/src/exacly"
    conforms(docs, state)


def test_refreshing_one_dimension_keeps_the_others() -> None:
    docs = {"config": config(requirement("REQ-1", CI))}
    first = refresh(docs, RESULTS, RUN, distribution=True, root=ROOT)
    first["records"]["REQ-1"]["evidence"] = [
        {"locator": "traces/1", "kind": "runtime_observation", "claim": "Seen", "observed_at": "2026-10-23",
         "applies_to": "main"}
    ]  # fmt: skip
    later_run = Run(**{**RUN.__dict__, "revision": "1c2d3e4"})
    second = valid_state(refresh(docs, RESULTS, later_run, previous=first))
    assert assessment(second, "REQ-1", "delivery") == assessment(first, "REQ-1", "delivery")
    assert assessment(second, "REQ-1")["context"] == "1c2d3e4"
    assert second["records"]["REQ-1"]["evidence"] == first["records"]["REQ-1"]["evidence"]
    conforms(docs, second)


def test_observations_of_removed_records_and_relationships_leave_state() -> None:
    before = config(requirement("REQ-1", CI), requirement("REQ-2", CI))
    earlier = refresh({"config": before}, RESULTS, RUN)
    seen = {"locator": "traces/1", "kind": "runtime_observation", "claim": "Seen", "observed_at": "2026-10-23",
            "applies_to": "main"}  # fmt: skip
    earlier["relationships"] = [
        {"source": "REQ-1", "type": "supports", "target": "PROC-1", "evidence": [seen]},
        {"source": "demo:REQ-2", "type": "supports", "target": "PROC-1", "evidence": [seen]},
        {"id": "REL-GONE", "evidence": [seen]},
    ]
    earlier["pending"] = [
        {"record": "REQ-1", "task": "issues/1", "opened_at": "2026-10-23T10:00:00Z"},
        {"record": "demo:REQ-2", "task": "issues/2", "opened_at": "2026-10-23T10:00:00Z"},
    ]
    docs = {"config": config(requirement("REQ-1", CI))}
    later = valid_state(refresh(docs, RESULTS, RUN, previous=earlier, distribution=True, root=ROOT))
    assert list(later["records"]) == ["REQ-1"]
    assert later["relationships"] == earlier["relationships"][:1]
    assert later["pending"] == earlier["pending"][:1]
    conforms(docs, later)


def test_a_release_that_no_longer_holds_is_cleared() -> None:
    docs = {"config": config(requirement("REQ-1", CI))}
    first = refresh(docs, RESULTS, RUN, distribution=True, root=ROOT)
    assert assessment(first, "REQ-1", "delivery")["state"] == "released"
    moved = {"config": config(requirement("REQ-1", CI), app_ref="python/src/moved")}
    second = valid_state(refresh(moved, RESULTS, RUN, previous=first, distribution=True, root=ROOT))
    assert [a["dimension"] for a in second["records"]["REQ-1"]["assessments"]] == ["verification"]


def test_references_qualified_with_the_model_namespace_find_the_implementer() -> None:
    cfg = config(requirement("REQ-1", CI))
    for rel in cfg["relationships"]:
        if rel["type"] == "implements":
            rel["source"], rel["target"] = f"demo:{rel['source']}", f"demo:{rel['target']}"
    docs = {"config": cfg}
    state = refresh(docs, RESULTS, RUN, distribution=True, root=ROOT)
    assert assessment(state, "REQ-1", "delivery")["state"] == "released"
    conforms(docs, state)
