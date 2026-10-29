"""Refresh keeps the model defining a check distinct from the implementation it checked."""

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from exacly import planning, policy, query, validate_documents
from exacly.applicability import Target
from exacly.refresh import RefreshError, Run, refresh
from test_applicability import WHEN, A, B
from test_refresh import ROOT, RUN, assessment, config, requirement, valid_state

C = "c" * 40
UNIT = {"workflow": "ci", "job": "unit", "cases": ["unit::test"]}
INTEGRATION = {"workflow": "ci", "job": "integration", "cases": ["integration::test"]}
POLICY: dict[str, Any] = {
    "name": "verification",
    "version": "1",
    "requirements": [{"record": "REQ-1", "dimensions": ["verification"]}],
}


def documents(two_jobs: bool = False) -> dict[str, Any]:
    tests = [UNIT, INTEGRATION] if two_jobs else UNIT
    return {"config": config(requirement("REQ-1", tests))}


def run(**fields: Any) -> Run:
    return replace(RUN, revision=B, model_revision=A, workflow="ci", job="unit", **fields)


def target(model_revision: str = A, revision: str = B) -> Target:
    return Target({"context": "main", "model_revision": model_revision, "revision": revision})


def test_split_identity_produces_valid_state_with_implementation_scoped_evidence() -> None:
    docs = documents()
    before = copy.deepcopy(docs)
    state = valid_state(refresh(docs, {"unit::test": "passed"}, run()))
    observed = assessment(state, "REQ-1")
    assert state["config_revision"] == A
    assert observed["subject"] == {"model_revision": A, "revision": B}
    assert observed["context"] == B
    assert observed["evidence"][0]["applies_to"] == B
    assert observed["state"] == "passed"
    assert validate_documents({**docs, "state": state}, WHEN, A).errors == []
    assert query.load({**docs, "state": state}, WHEN).status("REQ-1")["main"]["verification"] == "passed"
    assert docs == before


@pytest.mark.parametrize(
    ("model_revision", "revision", "expected"),
    [(A, B, "passed"), (A, A, "not_checked"), (B, B, "not_checked"), (A, C, "not_checked")],
)
def test_assessment_gate_and_plan_require_both_observed_identities(
    model_revision: str, revision: str, expected: str
) -> None:
    docs = documents()
    docs["state"] = refresh(docs, {"unit::test": "passed"}, run())
    before = copy.deepcopy(docs)
    selected = target(model_revision, revision)
    model = query.load(docs, WHEN)
    assessed = query.assess(model, "REQ-1", selected)
    gated = policy.evaluate(model, POLICY, selected)
    planned = planning.create(docs, POLICY, selected, WHEN)
    assert assessed["status"]["verification"] == expected
    assert gated["result"] == ("satisfied" if expected == "passed" else "unknown")
    if expected == "passed":
        assert planned["status"] == "satisfied" and planned["actions"] == []
    else:
        assert assessed["excluded"]
        assert planned["readiness"] == "unknown"
        assert [action["kind"] for action in planned["actions"]] == ["collect_evidence"]
    assert docs == before


def test_legacy_positional_runs_and_explicit_equal_identities_have_the_same_observations() -> None:
    docs = documents()
    legacy = Run(B, RUN.context, RUN.observed_at, "ci", "unit", RUN.job_status, RUN.url, RUN.source_url)
    explicit = replace(legacy, model_revision=B)
    first = refresh(docs, {"unit::test": "passed"}, legacy, distribution=True, root=ROOT)
    second = refresh(docs, {"unit::test": "passed"}, explicit, distribution=True, root=ROOT)
    assert legacy.model_revision is None
    assert legacy.config_revision == B
    assert first["config_revision"] == second["config_revision"] == B
    assert first["records"] == second["records"]


def test_replacing_a_legacy_runs_revision_keeps_the_model_fallback_dynamic() -> None:
    legacy = replace(RUN, revision=B)
    later = replace(legacy, revision=C)
    assert legacy.config_revision == B
    assert later.config_revision == C
    state = refresh(documents(), {"unit::test": "passed"}, replace(later, workflow="ci", job="unit"))
    assert state["config_revision"] == C
    assert assessment(state, "REQ-1")["subject"] == {"model_revision": C, "revision": C}


@pytest.mark.parametrize("different", ["implementation", "model"])
def test_a_later_job_preserves_original_subjects_and_cannot_merge_different_identities(different: str) -> None:
    docs = documents(two_jobs=True)
    first = refresh(docs, {"unit::test": "passed"}, run())
    original = copy.deepcopy(first)
    next_run = (
        replace(run(), job="integration", revision=C)
        if different == "implementation"
        else replace(run(), job="integration", model_revision=C)
    )
    second = refresh(docs, {"integration::test": "passed"}, next_run, previous=first)
    assessments = second["records"]["REQ-1"]["assessments"]
    retained = next(a for a in assessments if a["binding"]["job"] == "unit")
    assert retained == assessment(first, "REQ-1")
    assert first == original
    assert second["lineage"] == first["lineage"]
    assert second["serial"] == first["serial"] + 1
    assert query.load({**docs, "state": second}, WHEN).status("REQ-1")["main"]["verification"] == "not_checked"
    requested = target(A if different == "implementation" else C, C if different == "implementation" else B)
    answer = query.assess(query.load({**docs, "state": second}, WHEN), "REQ-1", requested)
    assert answer["status"]["verification"] == "not_checked"
    assert answer["missing"] and answer["excluded"]


def test_jobs_observing_the_same_model_and_implementation_can_establish_verification() -> None:
    docs = documents(two_jobs=True)
    first = refresh(docs, {"unit::test": "passed"}, run())
    second = refresh(docs, {"integration::test": "passed"}, replace(run(), job="integration"), previous=first)
    assert (
        query.assess(query.load({**docs, "state": second}, WHEN), "REQ-1", target())["status"]["verification"]
        == "passed"
    )


def test_a_changed_binding_replaces_its_observation_without_relabeling_the_old_one() -> None:
    docs = documents()
    first = refresh(docs, {"unit::test": "passed"}, run())
    original = copy.deepcopy(first)
    changed = documents()
    changed["config"]["records"][-1]["observe"]["tests"] = {**UNIT, "job": "new-unit", "cases": ["unit::new"]}
    second = refresh(changed, {}, replace(run(), revision=C, job="new-unit"), previous=first)
    observed = assessment(second, "REQ-1")
    assert observed["binding"]["job"] == "new-unit"
    assert observed["state"] == "not_checked"
    assert observed["subject"] == {"model_revision": A, "revision": C}
    assert first == original


def test_split_verification_keeps_distribution_subjects_and_evidence_unchanged() -> None:
    docs = documents()
    first = refresh(
        docs, {"unit::test": "passed"}, replace(run(), revision=C, model_revision=None), distribution=True, root=ROOT
    )
    original = copy.deepcopy(first)
    delivered = assessment(first, "REQ-1", "delivery")
    second = refresh(docs, {"unit::test": "passed"}, run(), previous=first)
    assert assessment(second, "REQ-1", "delivery") == delivered
    assert delivered["subject"] == {"model_revision": C, "revision": C, "environment": "main"}
    assert first == original
    assert second["config_revision"] == A


def test_split_verification_preserves_real_manual_qa_observations(tmp_path: Path) -> None:
    from test_manual_qa import documents as manual_documents
    from test_manual_qa import manual_requirement, observation, observe

    (tmp_path / "qa" / "observations").mkdir(parents=True)
    for platform in ("ios", "android"):
        observation(tmp_path, platform, platform, {1: "passed", 2: "passed", 3: "passed"})
    docs = manual_documents(manual_requirement(UNIT))
    first, _ = observe(tmp_path, docs=docs)
    original = copy.deepcopy(first)
    second = valid_state(refresh(docs, {"unit::test": "passed"}, replace(run(), context="production"), previous=first))
    retained = [a for a in second["records"]["REQ-1"]["assessments"] if a["source"] == "manual-qa:observations"]
    assert retained == first["records"]["REQ-1"]["assessments"]
    assert first == original
    assert second["config_revision"] == A


def test_legacy_retained_observations_do_not_gain_new_identity_claims() -> None:
    docs = documents(two_jobs=True)
    earlier_run = replace(run(), revision=C, model_revision=None)
    first = refresh(docs, {"unit::test": "passed"}, earlier_run, distribution=True, root=ROOT)
    for observed in first["records"]["REQ-1"]["assessments"]:
        observed.pop("source")
        observed.pop("subject")
    original = copy.deepcopy(first)
    second = refresh(docs, {"integration::test": "passed"}, replace(run(), job="integration"), previous=first)
    retained = [a for a in second["records"]["REQ-1"]["assessments"] if "subject" not in a]
    assert len(retained) == 2
    assert retained == [
        {**observed, "source": "tests" if observed["dimension"] == "verification" else "distribution"}
        for observed in original["records"]["REQ-1"]["assessments"]
    ]
    assert first == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", ""),
        ("revision", " \t"),
        ("revision", None),
        ("revision", 7),
        ("model_revision", ""),
        ("model_revision", " \t"),
        ("model_revision", 7),
    ],
)
def test_invalid_revision_identities_are_rejected(field: str, value: Any) -> None:
    with pytest.raises(RefreshError, match=field):
        replace(RUN, **{field: value})


def test_unsupported_split_distribution_does_not_change_earlier_state_or_config() -> None:
    docs = documents()
    previous = refresh(
        docs, {"unit::test": "passed"}, replace(run(), model_revision=None), distribution=True, root=ROOT
    )
    before = copy.deepcopy((docs, previous))
    with pytest.raises(RefreshError, match=r"distribution.*different model and implementation"):
        refresh(docs, {"unit::test": "passed"}, run(), previous=previous, distribution=True, root=ROOT)
    assert (docs, previous) == before


def test_equivalent_git_abbreviations_remain_compatible_with_distribution() -> None:
    state = valid_state(
        refresh(
            documents(), {"unit::test": "passed"}, replace(run(), model_revision=B[:7]), distribution=True, root=ROOT
        )
    )
    assert state["config_revision"] == B[:7]
    assert assessment(state, "REQ-1", "delivery")["subject"] == {
        "model_revision": B[:7],
        "revision": B,
        "environment": "main",
    }
