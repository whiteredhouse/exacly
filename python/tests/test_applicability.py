"""Subjects, target projections, and revision-safe summaries."""

import copy
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from exacly import query
from exacly.cli import exacly_main
from exacly.refresh import refresh
from test_refresh import ROOT, RUN, config, requirement

A, B = "a" * 40, "b" * 40
WHEN = dt.date(2026, 10, 26)


def documents(two_jobs: bool = False) -> dict[str, Any]:
    tests: Any = {"workflow": "ci", "job": "unit", "cases": ["unit::test"]}
    if two_jobs:
        tests = [tests, {"workflow": "ci", "job": "integration", "cases": ["integration::test"]}]
    docs = {"config": config(requirement("REQ-1", tests))}
    run = replace(RUN, revision=A, workflow="ci", job="unit")
    docs["state"] = refresh(docs, {"unit::test": "passed"}, run, distribution=True, root=ROOT)
    return docs


def target(**fields: Any) -> Any:
    from exacly.applicability import Target

    return Target({"context": "main", "model_revision": A, "revision": A, **fields})


def test_passed_tests_before_delivery_can_be_projected() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"] = docs["state"]["records"]["REQ-1"]["assessments"][:1]
    report = query.assess(query.load(docs, WHEN), "REQ-1", target())
    assert report["status"] == {"verification": "passed", "delivery": "not_observed"}
    assert report["applicable"][0]["subject"] == {"model_revision": A, "revision": A}
    assert report["as_of"] == "2026-10-26"


def test_default_summaries_do_not_combine_jobs_from_different_revisions() -> None:
    docs = documents(two_jobs=True)
    later = refresh(
        {"config": docs["config"]},
        {"integration::test": "passed"},
        replace(RUN, revision=B, workflow="ci", job="integration"),
        previous=docs["state"],
    )
    m = query.load({"config": docs["config"], "state": later}, WHEN)
    assert m.status("REQ-1")["main"]["verification"] == "not_checked"
    assert [r["id"] for r in query.gaps(m)["coverage"]["unverified"]] == ["REQ-1"]
    report = query.assess(m, "REQ-1", target(model_revision=B, revision=B))
    assert report["status"]["verification"] == "not_checked"
    assert report["missing"][0]["binding"] == {"workflow": "ci", "job": "unit"}
    assert report["excluded"][0]["assessment"]["subject"]["revision"] == A


@pytest.mark.parametrize(
    "fields",
    [
        {"model_revision": B},
        {"revision": B},
        {"environment": "production"},
        {"builds": {"app": {"digest": "sha256:123"}}},
        {"release": "1.0"},
        {"platform": "ios"},
    ],
)
def test_target_identity_must_be_established(fields: dict[str, Any]) -> None:
    report = query.assess(query.load(documents(), WHEN), "REQ-1", target(**fields))
    assert report["status"]["verification"] == "not_checked"
    assert report["excluded"] and all(e["reason"] for e in report["excluded"])


def test_source_tests_do_not_establish_deployed_environment_verification() -> None:
    report = query.assess(query.load(documents(), WHEN), "REQ-1", target(environment="main"))
    assert report["status"] == {"verification": "not_checked", "delivery": "released"}


@pytest.mark.parametrize("mutation", ["expired", "context", "kind"])
def test_positive_result_needs_valid_support(mutation: str) -> None:
    docs = documents()
    ev = docs["state"]["records"]["REQ-1"]["assessments"][0]["evidence"][0]
    ev[{"expired": "expires_at", "context": "applies_to", "kind": "kind"}[mutation]] = {
        "expired": "2026-10-25",
        "context": B,
        "kind": "implementation",
    }[mutation]
    assert query.assess(query.load(docs, WHEN), "REQ-1", target())["status"]["verification"] == "not_checked"


def test_evidence_expiring_today_is_still_supported() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][0]["evidence"][0]["expires_at"] = "2026-10-26"
    assert query.assess(query.load(docs, WHEN), "REQ-1", target())["status"]["verification"] == "passed"


def test_legacy_refresh_tests_establish_revision_but_not_artifact_identity() -> None:
    docs = documents()
    for a in docs["state"]["records"]["REQ-1"]["assessments"]:
        a.pop("subject", None)
    m = query.load(docs, WHEN)
    assert query.assess(m, "REQ-1", target())["status"] == {"verification": "passed", "delivery": "not_observed"}
    assert (
        query.assess(m, "REQ-1", target(builds={"app": {"digest": "sha256:D"}}))["status"]["verification"]
        == "not_checked"
    )


def test_exact_build_composition_and_scope_are_required() -> None:
    docs = documents()
    a = docs["state"]["records"]["REQ-1"]["assessments"][0]
    a["subject"] = {"model_revision": A, "builds": {"app": {"digest": "sha256:D"}}, "environment": "production"}
    chosen = target(revision=A).data.copy()
    chosen.pop("revision")
    chosen.update(builds={"app": {"digest": "sha256:D"}}, environment="production")
    from exacly.applicability import Target

    assert query.assess(query.load(docs, WHEN), "REQ-1", Target(chosen))["status"]["verification"] == "passed"
    chosen["builds"]["app"]["digest"] = "sha256:E"
    assert query.assess(query.load(docs, WHEN), "REQ-1", Target(chosen))["status"]["verification"] == "not_checked"


def test_narrow_scope_does_not_establish_unscoped_claim() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][0]["subject"] = {
        "model_revision": A,
        "revision": A,
        "environment": "staging",
    }
    assert query.assess(query.load(docs, WHEN), "REQ-1", target())["status"]["verification"] == "not_checked"


def test_projection_preserves_original_observations() -> None:
    docs = documents()
    before = copy.deepcopy(docs)
    m = query.load(docs, WHEN)
    assert query.assess(m, "REQ-1", target()) == query.assess(m, "REQ-1", target())
    assert docs == before


@pytest.mark.parametrize("fields", [{"context": ""}, {"revision": None}, {"unexpected": True}, {"builds": {}}])
def test_invalid_target_is_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        target(**fields)


def test_assess_command_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs = documents()
    folder = tmp_path / "model"
    folder.mkdir()
    for name, doc in docs.items():
        (folder / f"{name}.json").write_text(json.dumps(doc))
    selection = tmp_path / "target.json"
    selection.write_text(json.dumps(target().data))
    code = exacly_main(
        ["assess", "REQ-1", "-m", str(folder), "--target", str(selection), "--date", str(WHEN), "--json"]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"]["verification"] == "passed"


def test_same_model_revision_does_not_merge_different_tested_implementations() -> None:
    docs = documents(two_jobs=True)
    assessments = docs["state"]["records"]["REQ-1"]["assessments"]
    integration = copy.deepcopy(assessments[0])
    integration["binding"] = {"workflow": "ci", "job": "integration"}
    integration["subject"]["revision"] = B
    integration["context"] = B
    integration["evidence"][0]["applies_to"] = B
    assessments.append(integration)
    assert query.load(docs, WHEN).status("REQ-1")["main"]["verification"] == "not_checked"


def test_missing_required_delivery_source_cannot_be_hidden_by_distribution() -> None:
    docs = documents()
    docs["config"]["records"][-1]["observe"]["deployments"] = {"service": "app", "environments": ["main"]}
    answer = query.assess(query.load(docs, WHEN), "REQ-1", target())
    assert answer["status"]["delivery"] == "not_observed"
    assert any(m["source"] == "deployments" for m in answer["missing"])


def test_manual_qa_subject_preserves_exact_target_identity(tmp_path: Path) -> None:
    from exacly.applicability import Target
    from test_manual_qa import TARGET, observation, observe
    from test_manual_qa import documents as manual_documents

    (tmp_path / "qa" / "observations").mkdir(parents=True)
    observation(tmp_path, "ios", "ios", {1: "passed", 2: "passed", 3: "passed"})
    state, _ = observe(tmp_path)
    docs = {**manual_documents(), "state": state}
    selection = {k: v for k, v in TARGET.items() if k != "due"}
    selection.update(environment="production", platform="ios")
    answer = query.assess(query.load(docs, WHEN), "REQ-1", Target(selection))
    assert answer["status"]["verification"] == "passed"
    assert answer["applicable"][0]["subject"]["builds"] == TARGET["builds"]
    assert all(e["assessment"]["subject"]["platform"] == "android" for e in answer["excluded"])


def test_cli_reports_exclusion_reasons_in_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs = documents()
    model = tmp_path / "model.json"
    # A single config document plus an explicitly named state file.
    model.write_text(json.dumps(docs["config"]))
    state = tmp_path / "state.json"
    state.write_text(json.dumps(docs["state"]))
    selection = tmp_path / "target.json"
    selection.write_text(json.dumps(target(revision=B).data))
    assert exacly_main(["assess", "REQ-1", "-m", str(model), "-m", str(state), "--target", str(selection)]) == 0
    out = capsys.readouterr().out
    assert "verification: not_checked" in out
    assert "revision does not match target" in out


def test_obsolete_binding_does_not_poison_selected_implementation() -> None:
    docs = documents()
    assessments = docs["state"]["records"]["REQ-1"]["assessments"]
    obsolete = copy.deepcopy(assessments[0])
    obsolete.update(binding={"workflow": "ci", "job": "removed"}, state="failed", reason="Old job failed")
    assessments.append(obsolete)
    answer = query.assess(query.load(docs, WHEN), "REQ-1", target())
    assert answer["status"]["verification"] == "passed"
    assert any("binding" in e["reason"] for e in answer["excluded"])


def test_obsolete_binding_does_not_trigger_mixed_revision_guard() -> None:
    docs = documents()
    assessments = docs["state"]["records"]["REQ-1"]["assessments"]
    obsolete = copy.deepcopy(assessments[0])
    obsolete["binding"] = {"workflow": "ci", "job": "removed"}
    obsolete["subject"]["revision"] = B
    assessments.append(obsolete)
    assert query.load(docs, WHEN).status("REQ-1")["main"]["verification"] == "passed"


def ambiguous_documents() -> dict[str, Any]:
    docs = documents()
    checks = [{"workflow": "ci", "job": job, "cases": [f"{job}::test"]} for job in ("unit", "integration", "e2e")]
    docs["config"]["records"][-1]["observe"]["tests"] = checks
    base = docs["state"]["records"]["REQ-1"]["assessments"][0]
    assessments = []
    for binding, revision in zip(checks, ["abcdef0", "abcdef01" + "1" * 32, "abcdef02" + "2" * 32], strict=True):
        a = copy.deepcopy(base)
        a["binding"] = {k: binding[k] for k in ("workflow", "job")}
        a["subject"]["revision"] = revision
        assessments.append(a)
    docs["state"]["records"]["REQ-1"]["assessments"] = assessments
    return docs


def test_shared_abbreviation_does_not_hide_conflicting_full_revisions() -> None:
    m = query.load(ambiguous_documents(), WHEN)
    assert m.status("REQ-1")["main"]["verification"] == "not_checked"
    answer = query.assess(m, "REQ-1", target(revision="abcdef0"))
    assert answer["status"]["verification"] == "not_checked"
    assert any("ambiguous" in e["reason"] for e in answer["excluded"])


@pytest.mark.parametrize(("scope", "value"), [("environment", "staging"), ("platform", "ios"), ("release", "1.0")])
def test_narrow_delivery_scope_requires_matching_selection(scope: str, value: str) -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][1]["subject"][scope] = value
    m = query.load(docs, WHEN)
    assert query.assess(m, "REQ-1", target())["status"]["delivery"] == "not_observed"
    assert query.assess(m, "REQ-1", target(**{scope: value}))["status"]["delivery"] == "released"
