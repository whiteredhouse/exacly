"""Planning proposes verification work from applicable evidence without executing it."""

import copy
import datetime as dt
import json
from dataclasses import replace
from typing import Any

import jsonschema
import pytest
from exacly import planning, query
from exacly.refresh import refresh
from test_applicability import WHEN, A, B, documents, target
from test_refresh import ROOT, RUN, config, requirement

POLICY: dict[str, Any] = {
    "name": "verification-work",
    "version": "1",
    "requirements": [{"record": "REQ-1", "dimensions": ["verification"]}],
}


def create(docs: dict[str, Any] | None = None, **selection: Any) -> dict[str, Any]:
    return planning.create(documents() if docs is None else docs, POLICY, target(**selection), WHEN)


def unchecked_documents() -> dict[str, Any]:
    docs = documents()
    assessment = docs["state"]["records"]["REQ-1"]["assessments"][0]
    assessment.update(state="not_checked", evidence=[], reason="The selected tests have not run.")
    return docs


def only_action(plan: dict[str, Any], kind: str) -> dict[str, Any]:
    assert plan["status"] == "work_proposed"
    assert len(plan["actions"]) == 1
    action: dict[str, Any] = plan["actions"][0]
    assert action["kind"] == kind
    assert action["record"] == "REQ-1"
    assert action["dimension"] == "verification"
    assert action["id"]
    assert action["reason"]
    assert action["completion"]
    assert action["criteria_digest"]
    assert action["bindings_digest"]
    assert action["completion"] == {
        "record": action["record"],
        "dimension": "verification",
        "required_state": "passed",
        "target": plan["target"],
        "criteria_digest": action["criteria_digest"],
        "bindings_digest": action["bindings_digest"],
        "requires_trusted_observation": True,
    }
    assert plan["checks"][0]["classification"] == kind
    assert plan["checks"][0]["action_id"] == action["id"]
    return action


def reverse_objects(value: Any) -> Any:
    """Change mapping order while preserving list contents and order."""
    if isinstance(value, dict):
        return {key: reverse_objects(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reverse_objects(item) for item in value]
    return value


def test_satisfied_policy_has_no_work_and_records_selected_inputs() -> None:
    docs = documents()
    chosen = target()
    plan = planning.create(docs, POLICY, chosen, WHEN)
    jsonschema.validate(plan, planning.SCHEMA, format_checker=jsonschema.FormatChecker())
    assert plan["schema_version"] == "0.1.0"
    assert plan["advisory"] is True
    assert plan["tool"]["name"] == "exacly"
    assert plan["tool"]["version"]
    assert plan["namespace"] == "demo"
    assert plan["status"] == "satisfied"
    assert plan["readiness"] == "satisfied"
    assert plan["actions"] == []
    assert plan["target"] == chosen.data
    assert plan["as_of"] == "2026-10-26"
    assert plan["policy"]["name"] == POLICY["name"]
    assert plan["policy"]["version"] == POLICY["version"]
    assert plan["policy"]["digest"]
    assert plan["inputs"]["config"]["revision"] == A
    assert plan["inputs"]["config"]["digest"]
    assert plan["inputs"]["states"]
    snapshot = plan["inputs"]["states"][0]
    assert snapshot["context"] == "main"
    assert snapshot["lineage"] == docs["state"]["lineage"]
    assert snapshot["serial"] == docs["state"]["serial"]
    assert snapshot["digest"]
    assert plan["digest"]
    assert plan["checks"][0]["classification"] == "satisfied"
    assert not plan["checks"][0].get("action_id")


def test_identical_inputs_and_date_produce_identical_artifacts_without_mutation() -> None:
    docs = unchecked_documents()
    policy_doc = copy.deepcopy(POLICY)
    chosen = target()
    before = copy.deepcopy((docs, policy_doc, chosen.data))
    first = planning.create(docs, policy_doc, chosen, WHEN)
    second = planning.create(docs, policy_doc, chosen, WHEN)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert (docs, policy_doc, chosen.data) == before
    jsonschema.validate(first, planning.SCHEMA)


def test_document_and_object_mapping_order_do_not_change_the_plan() -> None:
    docs = unchecked_documents()
    first = planning.create(docs, POLICY, target(), WHEN)
    reordered = planning.create(reverse_objects(docs), reverse_objects(POLICY), target(), WHEN)
    assert reordered == first


@pytest.mark.parametrize(
    "selection",
    [
        {"model_revision": B},
        {"revision": B},
        {"builds": {"app": {"digest": "sha256:another-build"}}},
        {"environment": "production"},
        {"platform": "ios"},
        {"release": "1.0"},
    ],
)
def test_unrelated_target_evidence_proposes_collection(selection: dict[str, Any]) -> None:
    plan = create(**selection)
    only_action(plan, "collect_evidence")
    assert plan["readiness"] == "unknown"
    assert plan["checks"][0]["excluded"]


def test_missing_results_need_collection_without_assuming_implementation_is_missing() -> None:
    docs = documents()
    docs.pop("state")
    plan = create(docs)
    only_action(plan, "collect_evidence")
    assert plan["readiness"] == "unknown"
    assert plan["inputs"]["states"] == []
    assert all(action["kind"] != "implement" for action in plan["actions"])


def test_only_selected_requirements_get_work_and_each_action_has_its_own_identity() -> None:
    tests = {"workflow": "ci", "job": "unit", "cases": ["unit::test"]}
    docs = {"config": config(requirement("REQ-1", tests), requirement("REQ-2", tests))}
    first = create(docs)
    assert [action["record"] for action in first["actions"]] == ["REQ-1"]
    chosen = copy.deepcopy(POLICY)
    chosen["requirements"].append({"record": "REQ-2", "dimensions": ["verification"]})
    both = planning.create(docs, chosen, target(), WHEN)
    assert [action["record"] for action in both["actions"]] == ["REQ-1", "REQ-2"]
    assert len({action["id"] for action in both["actions"]}) == 2


def test_applicable_failure_is_an_investigation_with_its_observed_reason() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][0].update(state="failed", reason="The invoice was sent twice.")
    plan = create(docs)
    action = only_action(plan, "investigate_failure")
    assert plan["readiness"] == "not_satisfied"
    assert "invoice was sent twice" in action["reason"]
    assert plan["checks"][0]["evidence"]


@pytest.mark.parametrize(
    ("results", "state"),
    [
        ({"unit::pass": "passed", "unit::skip": "skipped"}, "inconclusive"),
        ({"unit::skip": "skipped"}, "not_checked"),
        ({}, "not_checked"),
    ],
)
def test_partial_or_skipped_execution_requires_more_evidence(results: dict[str, str], state: str) -> None:
    docs = {"config": config(requirement("REQ-1", {"workflow": "ci", "job": "unit", "cases": ["unit::*"]}))}
    docs["state"] = refresh(docs, results, replace(RUN, revision=A, workflow="ci", job="unit"), root=ROOT)
    plan = create(docs)
    only_action(plan, "collect_evidence")
    assert plan["checks"][0]["state"] == state
    assert plan["readiness"] == "unknown"


def test_failure_takes_precedence_over_another_missing_binding() -> None:
    docs = documents(two_jobs=True)
    docs["state"]["records"]["REQ-1"]["assessments"][0].update(state="failed", reason="Unit check failed.")
    plan = create(docs)
    only_action(plan, "investigate_failure")
    assert plan["checks"][0]["missing"]


def test_missing_verification_binding_is_a_configuration_blocker() -> None:
    docs = documents()
    docs.pop("state")
    docs["config"]["records"][-1].pop("observe")
    plan = create(docs)
    only_action(plan, "blocked_configuration")
    assert plan["readiness"] == "unknown"


def test_a_delivery_binding_does_not_supply_missing_verification_configuration() -> None:
    docs = documents()
    docs.pop("state")
    docs["config"]["records"][-1]["observe"] = {"deployments": {"service": "app", "environments": ["main"]}}
    only_action(create(docs), "blocked_configuration")


def test_valid_review_evidence_can_satisfy_a_requirement_without_a_binding() -> None:
    docs = documents()
    docs["config"]["records"][-1].pop("observe")
    assessment = docs["state"]["records"]["REQ-1"]["assessments"][0]
    assessment.pop("source")
    assessment.pop("binding")
    assessment["evidence"][0].update(kind="decision", claim="Review confirmed the acceptance criteria.")
    plan = create(docs)
    assert plan["status"] == "satisfied"
    assert plan["actions"] == []


def test_obsolete_binding_cannot_establish_completion() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][0]["binding"]["job"] = "removed-job"
    plan = create(docs)
    only_action(plan, "collect_evidence")
    assert any("binding" in exclusion["reason"] for exclusion in plan["checks"][0]["excluded"])


@pytest.mark.parametrize("observed_state", ["passed", "failed"])
def test_expired_evidence_cannot_establish_success_or_current_failure(observed_state: str) -> None:
    docs = documents()
    assessment = docs["state"]["records"]["REQ-1"]["assessments"][0]
    assessment["evidence"][0]["expires_at"] = "2026-10-25"
    if observed_state == "failed":
        assessment.update(state="failed", reason="Historical failure.")
    plan = create(docs)
    if observed_state == "passed":
        assert plan["status"] == "blocked"
        assert plan["actions"] == []
        assert any(finding["rule"] == "7.8" for finding in plan["findings"]["errors"])
    else:
        only_action(plan, "collect_evidence")
    assert plan["readiness"] == "unknown"


def test_conformance_errors_block_proposals_and_remain_visible() -> None:
    docs = documents()
    docs["config"]["relationships"] = [rel for rel in docs["config"]["relationships"] if rel["type"] != "implements"]
    plan = create(docs)
    assert plan["status"] == "blocked"
    assert plan["readiness"] == "unknown"
    assert plan["actions"] == []
    assert plan["findings"]["errors"]


def test_a_state_for_another_namespace_blocks_proposals() -> None:
    docs = unchecked_documents()
    docs["state"]["namespace"] = "unrelated"
    plan = create(docs)
    assert plan["status"] == "blocked"
    assert plan["actions"] == []
    assert any(finding["rule"] == "11.state-namespace" for finding in plan["findings"]["errors"])


@pytest.mark.parametrize(
    "bad_policy",
    [
        {},
        {**POLICY, "requirements": []},
        {**POLICY, "requirements": [{"record": "REQ-1", "dimensions": ["delivery"]}]},
        {**POLICY, "requirements": [{"record": "REQ-1", "dimensions": ["verification", "delivery"]}]},
        {**POLICY, "requirements": [{"record": "APP-1", "dimensions": ["verification"]}]},
        {**POLICY, "requirements": [{"record": "missing", "dimensions": ["verification"]}]},
    ],
)
def test_unsupported_or_invalid_policies_are_rejected(bad_policy: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        planning.create(documents(), bad_policy, target(), WHEN)


def test_schema_invalid_config_cannot_become_a_plan() -> None:
    docs = documents()
    docs["config"]["records"] = "not records"
    with pytest.raises((ValueError, query.QueryError)):
        create(docs)


def test_retired_requirements_cannot_be_selected_for_current_work() -> None:
    docs = documents()
    docs.pop("state")
    docs["config"]["records"][-1]["lifecycle"] = "retired"
    docs["config"]["relationships"] = [
        rel for rel in docs["config"]["relationships"] if "REQ-1" not in (rel["source"], rel["target"])
    ]
    with pytest.raises(ValueError, match="retired"):
        create(docs)


@pytest.mark.parametrize("bad_date", [None, "2026-10-26", dt.datetime(2026, 10, 26, tzinfo=dt.UTC)])
def test_planning_requires_an_explicit_date_without_an_implicit_clock(bad_date: Any) -> None:
    with pytest.raises(ValueError, match="evaluation date"):
        planning.create(documents(), POLICY, target(), bad_date)


def test_new_state_serial_changes_snapshot_without_reidentifying_unresolved_work() -> None:
    docs = unchecked_documents()
    first = create(docs)
    docs["state"]["serial"] += 1
    second = create(docs)
    assert first["digest"] != second["digest"]
    assert first["inputs"]["states"] != second["inputs"]["states"]
    assert first["actions"][0]["id"] == second["actions"][0]["id"]


def test_reason_and_evaluation_date_do_not_reidentify_the_same_unresolved_goal() -> None:
    docs = unchecked_documents()
    first = create(docs)
    docs["state"]["records"]["REQ-1"]["assessments"][0]["reason"] = "The next report is not available."
    second = planning.create(docs, POLICY, target(), WHEN + dt.timedelta(days=1))
    assert first["digest"] != second["digest"]
    assert first["actions"][0]["id"] == second["actions"][0]["id"]


@pytest.mark.parametrize("change", ["criteria", "binding", "policy", "target"])
def test_changed_approved_goal_or_target_has_a_new_action_identity(change: str) -> None:
    docs = unchecked_documents()
    first = create(docs)
    selected = target()
    policy_doc = copy.deepcopy(POLICY)
    if change == "criteria":
        docs["config"]["records"][-1]["acceptance_criteria"].append("A second behavior is verified.")
    elif change == "binding":
        docs["config"]["records"][-1]["observe"]["tests"]["cases"] = ["unit::new-test"]
    elif change == "policy":
        policy_doc["version"] = "2"
    else:
        selected = target(revision=B)
    second = planning.create(docs, policy_doc, selected, WHEN)
    assert first["actions"][0]["id"] != second["actions"][0]["id"]


def test_serializable_date_metadata_does_not_mutate_the_supplied_documents() -> None:
    docs = unchecked_documents()
    docs["config"]["x-recorded-at"] = WHEN
    before = copy.deepcopy(docs)
    plan = create(docs)
    json.dumps(plan, allow_nan=False)
    assert docs == before


def test_yaml_date_in_a_known_string_field_cannot_bypass_model_validation() -> None:
    docs = unchecked_documents()
    docs["config"]["records"][-1]["last_reviewed_at"] = WHEN
    before = copy.deepcopy(docs)
    with pytest.raises((ValueError, query.QueryError)):
        create(docs)
    assert docs == before


def test_yaml_date_cannot_replace_the_policy_version_string() -> None:
    policy_doc = copy.deepcopy(POLICY)
    policy_doc["version"] = WHEN
    with pytest.raises(ValueError):
        planning.create(unchecked_documents(), policy_doc, target(), WHEN)
    assert policy_doc["version"] == WHEN


def test_mutated_target_is_revalidated_before_date_values_are_serialized() -> None:
    selected = target()
    selected.data["revision"] = WHEN
    with pytest.raises(ValueError):
        planning.create(unchecked_documents(), POLICY, selected, WHEN)
    assert selected.data["revision"] == WHEN


@pytest.mark.parametrize("unsupported", [float("nan"), float("inf"), {"unsupported"}])
def test_non_json_extension_metadata_cannot_produce_a_saved_plan(unsupported: Any) -> None:
    docs = unchecked_documents()
    docs["config"]["x-extra"] = unsupported
    with pytest.raises(ValueError):
        create(docs)


@pytest.mark.parametrize("kind", ["collect_evidence", "investigate_failure", "blocked_configuration"])
def test_human_view_explains_the_same_proposal_as_the_json(kind: str) -> None:
    docs = unchecked_documents()
    if kind == "investigate_failure":
        docs = documents()
        docs["state"]["records"]["REQ-1"]["assessments"][0].update(state="failed", reason="Expected one invoice.")
    elif kind == "blocked_configuration":
        docs.pop("state")
        docs["config"]["records"][-1].pop("observe")
    plan = create(docs)
    action = only_action(plan, kind)
    rendered = planning.as_text(plan)
    assert "REQ-1" in rendered
    assert action["reason"] in rendered
    assert "2026-10-26" in rendered
    assert POLICY["name"] in rendered
    assert A in rendered
    assert action["id"] in rendered
