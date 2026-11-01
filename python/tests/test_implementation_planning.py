"""Implementation proposals preserve approved intent while keeping observation gaps distinct."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest
from exacly import admission, planning
from exacly import implementation_planning as implementation
from exacly.applicability import Target
from test_applicability import WHEN, B, documents, target
from test_refresh import config, requirement

Doc = dict[str, Any]
Inputs = tuple[Doc, Doc, Doc, Target]


def inputs(two_goals: bool = False) -> Inputs:
    """Small conforming approved inputs, also reusable by public CLI acceptance tests."""
    docs = documents()
    docs.pop("state")
    if two_goals:
        docs = {"config": config(requirement("REQ-1"), requirement("REQ-2"))}
    repository = {"id": 17, "full_name": "example/product"}
    chosen = target()
    goals = []
    for number in range(1, 3 if two_goals else 2):
        goals.append(
            {
                "record": f"REQ-{number}",
                "criteria": [0],
                "output_paths": [f"src/feature{number}.py"],
                "depends_on": ["REQ-1"] if number == 2 else [],
                "findings": [
                    {
                        "id": f"finding-{number}",
                        "kind": "approved_behavior_gap",
                        "criteria": [0],
                        "reason": "Independent product review established the requested behavior gap.",
                        "review": {"issuer": "independent-reviewer", "id": f"review-{number}"},
                        "evidence": [{"locator": f"review://product/{number}", "digest": "sha256:" + "c" * 64}],
                    }
                ],
                "unresolved_decisions": [],
            }
        )
    intent: Doc = {
        "schema_version": "0.1.0",
        "id": "approved-product-change",
        "revision": 1,
        "supersedes": None,
        "repository": repository,
        "target": chosen.data,
        "goals": goals,
        "proposed_contract_changes": [],
    }
    policy: Doc = {
        "schema_version": "0.1.0",
        "name": "bounded-product-planning",
        "version": "1",
        "stage": "implementation-planning",
        "issuer": "operator",
        "approval_id": "planning-review-17",
        "repository": copy.deepcopy(repository),
        "model": {"namespace": "demo", "digest": admission.config_digest(docs)},
        "target": copy.deepcopy(chosen.data),
        "requirements": [goal["record"] for goal in goals],
        "allowed_output_paths": [path for goal in goals for path in goal["output_paths"]],
        "not_before": "2026-10-01",
        "expires_at": "2026-12-01",
    }
    policy["genesis_contract_digest"] = implementation.genesis_digest(docs, intent, policy, chosen, WHEN)
    return docs, intent, policy, chosen


def pins(intent: Doc, policy: Doc) -> Doc:
    return {
        "approved_intent_digest": planning.digest(intent),
        "approved_policy_digest": planning.digest(policy),
        "issuer": policy["issuer"],
        "approval_id": policy["approval_id"],
    }


def create(values: Inputs | None = None, *, previous: Doc | None = None, date: dt.date = WHEN) -> Doc:
    docs, intent, policy, chosen = inputs() if values is None else values
    if previous is None and intent["revision"] == 1:
        # Test setup independently selects this initial fixture objective. Lineage-reset tests
        # call the actual API directly so they retain the previously approved genesis pin.
        policy["genesis_contract_digest"] = implementation.genesis_digest(docs, intent, policy, chosen, date)
    return implementation.create(docs, intent, policy, chosen, date, previous=previous, **pins(intent, policy))


def current(proposal: Doc, values: Inputs, *, previous: Doc | None = None, date: dt.date = WHEN) -> Doc:
    docs, intent, policy, chosen = values
    return implementation.validate_current(
        proposal, docs, intent, policy, chosen, date, previous=previous, **pins(intent, policy)
    )


def test_missing_evidence_is_not_an_implementation_gap() -> None:
    values = inputs()
    finding = values[1]["goals"][0]["findings"][0]
    finding.update(kind="observation_gap", review=None, evidence=[])
    proposal = create(values)
    assert proposal["status"] == "observation_gaps" and proposal["tasks"] == []
    assert proposal["observations"][0]["state"] == "not_checked"
    assert proposal["observations"][0]["classification"] == "collect_evidence"


def test_decomposition_preserves_approved_criteria_and_dependencies() -> None:
    values = inputs(two_goals=True)
    before = copy.deepcopy(values)
    proposal = create(values)
    assert values == before
    first, second = proposal["tasks"]
    assert first["record"] == "REQ-1" and second["record"] == "REQ-2"
    assert first["depends_on"] == [] and second["depends_on"] == [first["id"]]
    assert first["criterion"]["text"] == "The thing is done."
    assert first["completion"]["condition"] == first["criterion"]["text"]
    assert first["completion"]["requires_trusted_observation"] is True
    assert first["completion"]["requires_independent_review"] is True
    assert first["outputs"] == ["src/feature1.py"]
    assert first["input_contract"] and first["criteria_digest"] and first["bindings_digest"]
    assert proposal["status"] == "work_proposed" and proposal["advisory"] is True
    assert current(proposal, values)["ready_for_separate_execution_review"] is True


def test_changed_contract_requires_independent_review_and_supersession() -> None:
    values = inputs()
    first = create(values)
    values[0]["config"]["records"][-1]["acceptance_criteria"][0] = "The independently changed thing is done."
    with pytest.raises(ValueError, match="approved content"):
        current(first, values)
    values[2]["model"]["digest"] = admission.config_digest(values[0])
    with pytest.raises(ValueError, match="explicit revision"):
        current(first, values)
    values[1].update(revision=2, supersedes=first["digest"])
    with pytest.raises(ValueError, match="predecessor"):
        create(values)
    second = create(values, previous=first)
    assert second["id"] == first["id"] and second["revision"] == 2
    assert second["supersedes"] == first["digest"] and second["digest"] != first["digest"]
    assert second["tasks"][0]["criterion"]["digest"] != first["tasks"][0]["criterion"]["digest"]
    assert current(second, values, previous=first)["status"] == "current"


def test_proposal_grants_no_execution_or_delivery_authority() -> None:
    proposal = create()
    assert all(value is False for value in proposal["claims"].values())
    assert "authorization" not in proposal and "command" not in proposal
    assert "coding_agent" not in proposal and proposal["advisory"] is True
    assert all(value is False for value in current(proposal, inputs())["claims"].values())


def test_approved_behavior_gap_is_preserved_alongside_unknown_observations() -> None:
    values = inputs()
    values[1]["goals"][0]["findings"].append(
        {
            "id": "no-observations",
            "kind": "observation_gap",
            "criteria": [0],
            "reason": "No current test ran.",
            "review": None,
            "evidence": [],
        }
    )
    proposal = create(values)
    assert proposal["observations"][0]["state"] == "not_checked"
    assert len(proposal["tasks"]) == 1 and proposal["tasks"][0]["finding"]["kind"] == "approved_behavior_gap"


@pytest.mark.parametrize("state", ["failed", "inconclusive", "not_checked"])
def test_unsatisfactory_observation_alone_never_creates_a_repair_task(state: str) -> None:
    values = inputs()
    values[0]["state"] = documents()["state"]
    assessment = values[0]["state"]["records"]["REQ-1"]["assessments"][0]
    assessment.update(state=state, reason="A producer observation requires investigation.")
    if state == "not_checked":
        assessment["evidence"] = []
    finding = values[1]["goals"][0]["findings"][0]
    finding.update(kind="observation_gap", review=None, evidence=[])
    proposal = create(values)
    assert proposal["tasks"] == []
    if state == "failed":
        assert proposal["observations"][0]["classification"] == "investigate_failure"


@pytest.mark.parametrize("change", ["expired", "wrong_revision", "skipped"])
def test_inapplicable_or_inconclusive_evidence_is_an_observation_gap(change: str) -> None:
    values = inputs()
    values[0]["state"] = documents()["state"]
    assessment = values[0]["state"]["records"]["REQ-1"]["assessments"][0]
    if change == "expired":
        assessment["evidence"][0]["expires_at"] = "2026-10-25"
    elif change == "wrong_revision":
        assessment["subject"]["revision"] = B
    else:
        assessment.update(state="inconclusive", reason="Only skipped tests were observed.")
    finding = values[1]["goals"][0]["findings"][0]
    finding.update(kind="observation_gap", review=None, evidence=[])
    proposal = create(values)
    assert proposal["tasks"] == [] and proposal["observations"][0]["state"] != "passed"


@pytest.mark.parametrize("field", ["review", "evidence"])
def test_behavior_findings_require_explicit_review_and_retained_support(field: str) -> None:
    values = inputs()
    values[1]["goals"][0]["findings"][0][field] = None if field == "review" else []
    with pytest.raises(ValueError, match="reviewed behavior gap"):
        create(values)


def test_investigated_finding_generates_one_task_per_selected_criterion() -> None:
    values = inputs()
    values[0]["config"]["records"][-1]["acceptance_criteria"].append("The second condition is true.")
    values[2]["model"]["digest"] = admission.config_digest(values[0])
    values[1]["goals"][0]["criteria"] = [0, 1]
    values[1]["goals"][0]["findings"][0].update(kind="investigated_finding", criteria=[0, 1])
    proposal = create(values)
    assert len(proposal["tasks"]) == 2 and len({task["id"] for task in proposal["tasks"]}) == 2
    assert {task["criterion"]["index"] for task in proposal["tasks"]} == {0, 1}


def test_uncovered_criteria_and_unresolved_choices_block_a_proposal() -> None:
    values = inputs()
    values[1]["goals"][0].update(findings=[], unresolved_decisions=["Choose the externally approved storage API."])
    proposal = create(values)
    assert proposal["status"] == "blocked" and proposal["tasks"] == []
    assert {item["code"] for item in proposal["blockers"]} == {"uninvestigated_criterion", "unresolved_decision"}
    assert current(proposal, values)["ready_for_separate_execution_review"] is False


@pytest.mark.parametrize("kind", ["acceptance_criteria", "tests", "observation_bindings", "policy"])
def test_generated_contract_changes_remain_unapproved_proposals(kind: str) -> None:
    values = inputs()
    values[1]["proposed_contract_changes"] = [
        {
            "kind": kind,
            "record": "REQ-1",
            "description": "Proposed change for separate review.",
            "content": {"example": True},
        }
    ]
    proposal = create(values)
    assert proposal["status"] == "blocked"
    assert proposal["proposed_contract_changes"][0]["review_status"] == "unapproved"
    assert proposal["claims"]["contract_changes_approved"] is False
    assert current(proposal, values)["ready_for_separate_execution_review"] is False


@pytest.mark.parametrize("bad", ["self", "cycle", "unknown"])
def test_invalid_dependencies_cannot_hide_inside_the_generated_graph(bad: str) -> None:
    values = inputs(two_goals=True)
    values[1]["goals"][0]["depends_on"] = [{"self": "REQ-1", "cycle": "REQ-2", "unknown": "REQ-99"}[bad]]
    with pytest.raises(ValueError, match="dependenc"):
        create(values)


def test_model_runtime_dependencies_are_not_coerced_into_implementation_ordering() -> None:
    values = inputs(two_goals=True)
    model = values[0]["config"]
    second = copy.deepcopy(next(record for record in model["records"] if record["id"] == "APP-1"))
    second["id"] = "APP-2"
    model["records"].append(second)
    model["relationships"] = [
        edge for edge in model["relationships"] if not (edge["type"] == "implements" and edge["target"] == "REQ-2")
    ]
    model["relationships"].extend(
        [
            {"source": "APP-2", "type": "implements", "target": "REQ-2"},
            {"source": "APP-2", "type": "depends_on", "target": "APP-1"},
        ]
    )
    values[2]["model"]["digest"] = admission.config_digest(values[0])
    values[1]["goals"][1]["depends_on"] = []
    first, second_task = create(values)["tasks"]
    assert first["depends_on"] == second_task["depends_on"] == []


def test_dependency_without_implementation_or_passed_observation_stays_blocked() -> None:
    values = inputs(two_goals=True)
    values[1]["goals"][0]["findings"][0].update(kind="observation_gap", review=None, evidence=[])
    proposal = create(values)
    assert proposal["status"] == "blocked" and len(proposal["tasks"]) == 1
    assert any(item["code"] == "unresolved_dependency" for item in proposal["blockers"])


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "src/*.py", "src/./feature.py", ".git/config", "src\\feature.py"]
)
def test_output_scope_is_literal_and_cannot_escape_the_repository(path: str) -> None:
    values = inputs()
    values[1]["goals"][0]["output_paths"] = [path]
    values[2]["allowed_output_paths"] = [path]
    with pytest.raises(ValueError, match="literal relative"):
        create(values)


def test_policy_scope_and_independently_supplied_pins_cannot_be_selected_by_intent() -> None:
    docs, intent, policy, chosen = inputs()
    with pytest.raises(ValueError, match="approval pin"):
        implementation.create(
            docs,
            intent,
            policy,
            chosen,
            WHEN,
            **{**pins(intent, policy), "approved_intent_digest": "sha256:" + "0" * 64},
        )
    intent["goals"][0]["output_paths"] = ["src/another.py"]
    with pytest.raises(ValueError, match="approved output"):
        create((docs, intent, policy, chosen))
    intent["goals"][0]["output_paths"] = ["src/feature1.py"]
    with pytest.raises(ValueError, match="approval identity"):
        implementation.create(
            docs, intent, policy, chosen, WHEN, **{**pins(intent, policy), "issuer": "self-approving-planner"}
        )


@pytest.mark.parametrize("change", ["intent", "policy", "target", "state", "criteria", "binding", "proposal"])
def test_changed_inputs_and_modified_artifacts_need_explicit_revision(change: str) -> None:
    values = inputs()
    proposal = create(values)
    if change == "intent":
        values[1]["goals"][0]["findings"][0]["reason"] = "Changed independently approved intent."
    elif change == "policy":
        values[2]["version"] = "2"
    elif change == "target":
        values = (*values[:3], target(revision=B))
    elif change == "state":
        values[0]["state"] = documents()["state"]
    elif change in {"criteria", "binding"}:
        record = values[0]["config"]["records"][-1]
        if change == "criteria":
            record["acceptance_criteria"].append("Changed approved contract.")
        else:
            record["observe"]["tests"]["job"] = "new-observer"
        values[2]["model"]["digest"] = admission.config_digest(values[0])
    else:
        proposal["tasks"][0]["completion"]["requires_independent_review"] = False
        proposal.pop("digest")
        proposal["digest"] = planning.digest(proposal)
    with pytest.raises(ValueError):
        current(proposal, values)


def test_mapping_order_filenames_and_clock_alone_do_not_change_task_identity() -> None:
    values = inputs()
    first = create(values)
    renamed = ({"renamed-file.yaml": values[0]["config"]}, *values[1:])
    second = create(renamed)
    assert first == second
    check = current(first, values, date=WHEN + dt.timedelta(days=1))
    assert check["status"] == "current"
    assert check["checked_as_of"] != first["as_of"]


def test_expired_scope_or_changed_predecessor_cannot_be_reused() -> None:
    values = inputs()
    first = create(values)
    with pytest.raises(ValueError, match="not current"):
        create(values, date=dt.date(2026, 12, 1))
    values[1].update(revision=2, supersedes="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="exact preceding"):
        create(values, previous=first)
    values[1]["supersedes"] = first["digest"]
    first["tasks"][0]["outputs"] = ["unreviewed.py"]
    with pytest.raises(ValueError, match="content changed"):
        create(values, previous=first)


@pytest.mark.parametrize("which", ["intent", "policy", "target"])
def test_unknown_authority_fields_are_rejected(which: str) -> None:
    values = inputs()
    selected = {"intent": values[1], "policy": values[2], "target": values[3].data}[which]
    selected["merge_authorized"] = True
    with pytest.raises(ValueError):
        create(values)


def test_input_limits_reject_cycles_before_recursive_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    values = inputs()
    values[1]["proposed_contract_changes"] = []
    values[1]["proposed_contract_changes"].append(values[1])
    with pytest.raises(ValueError, match="nesting"):
        create(values)
    values = inputs()
    monkeypatch.setattr(implementation, "MAX_INPUT_BYTES", 100)
    with pytest.raises(ValueError, match="size limit"):
        implementation.create(values[0], values[1], values[2], values[3], WHEN, **pins(values[1], values[2]))


def test_model_and_selected_criteria_must_be_approved_and_conforming() -> None:
    values = inputs()
    values[1]["goals"][0]["criteria"] = [99]
    with pytest.raises(ValueError, match="nonexistent"):
        create(values)
    values = inputs()
    values[0]["config"]["records"][-1]["statement"] = "Unapproved changed meaning."
    with pytest.raises(ValueError, match="approved content"):
        create(values)
    values = inputs()
    values[0]["config"]["relationships"] = []
    with pytest.raises(ValueError, match="conforming"):
        create(values)


@pytest.mark.parametrize("field", ["revision", "identifier", "date", "digest"])
def test_identity_components_are_canonical_without_newline_regex_aliases(field: str) -> None:
    values = inputs()
    if field == "revision":
        for document in (values[1], values[2]):
            document["target"]["revision"] = B + "\n"
        values = (*values[:3], Target(values[1]["target"]))
    elif field == "identifier":
        values[1]["id"] += "\n"
    elif field == "date":
        values[2]["not_before"] = "20261001"
    else:
        values[1]["goals"][0]["findings"][0]["evidence"][0]["digest"] += "\n"
    with pytest.raises(ValueError):
        create(values)


def test_generated_graph_and_content_have_independent_output_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    values = inputs(two_goals=True)
    monkeypatch.setattr(implementation, "MAX_TASKS", 1)
    with pytest.raises(ValueError, match="task limit"):
        create(values)
    monkeypatch.setattr(implementation, "MAX_TASKS", 512)
    monkeypatch.setattr(implementation, "MAX_PROPOSAL_BYTES", 100)
    with pytest.raises(ValueError, match="generated content size"):
        create(values)


@pytest.mark.parametrize("changed", ["target", "model", "policy", "observations"])
def test_initial_input_changes_cannot_reset_the_same_approved_lineage(changed: str) -> None:
    values = inputs()
    first = create(values)
    if changed == "target":
        values[1]["target"]["revision"] = B
        values[2]["target"]["revision"] = B
        values = (*values[:3], Target(values[1]["target"]))
    elif changed == "model":
        values[0]["config"]["records"][-1]["acceptance_criteria"][0] = "A changed approved criterion."
        values[2]["model"]["digest"] = admission.config_digest(values[0])
    elif changed == "policy":
        values[2]["version"] = "new-reviewed-policy"
    else:
        values[0]["state"] = documents()["state"]
    docs, intent, policy, chosen = values
    original_genesis = policy["genesis_contract_digest"]
    with pytest.raises(ValueError, match="explicit revision or new genesis"):
        implementation.create(docs, intent, policy, chosen, WHEN, **pins(intent, policy))
    policy["genesis_contract_digest"] = implementation.genesis_digest(docs, intent, policy, chosen, WHEN)
    assert policy["genesis_contract_digest"] != original_genesis
    distinct = implementation.create(docs, intent, policy, chosen, WHEN, **pins(intent, policy))
    assert distinct["id"] != first["id"] and distinct["revision"] == 1


def test_explicit_target_revision_preserves_the_reviewed_genesis_and_predecessor() -> None:
    values = inputs()
    first = create(values)
    values[1].update(revision=2, supersedes=first["digest"])
    values[1]["target"]["revision"] = B
    values[2]["target"]["revision"] = B
    values = (*values[:3], Target(values[1]["target"]))
    second = create(values, previous=first)
    assert second["id"] == first["id"] and second["revision"] == 2
    assert second["policy"]["genesis_contract_digest"] == first["policy"]["genesis_contract_digest"]
    assert second["supersedes"] == first["digest"]
    assert current(second, values, previous=first)["status"] == "current"


def test_genesis_fingerprinting_does_not_approve_or_execute_inputs() -> None:
    docs, intent, policy, chosen = inputs()
    policy.pop("genesis_contract_digest")
    fingerprint = implementation.genesis_digest(docs, intent, policy, chosen, WHEN)
    assert fingerprint.startswith("sha256:")
    with pytest.raises(ValueError, match="policy"):
        implementation.create(docs, intent, policy, chosen, WHEN, **pins(intent, policy))


def test_expiration_that_changes_observation_applicability_requires_revision() -> None:
    values = inputs()
    values[0]["state"] = documents()["state"]
    values[0]["state"]["records"]["REQ-1"]["assessments"][0]["evidence"][0]["expires_at"] = WHEN.isoformat()
    first = create(values)
    assert first["observations"][0]["state"] == "passed"
    with pytest.raises(ValueError, match="explicit revision"):
        current(first, values, date=WHEN + dt.timedelta(days=1))


def invalid_observation_inputs() -> Inputs:
    """Conforming config with a passed prerequisite inside semantically invalid observed state."""
    values = inputs(two_goals=True)
    values[0]["config"] = config(
        requirement("REQ-1", {"workflow": "ci", "job": "unit", "cases": ["unit::test"]}),
        requirement("REQ-2"),
    )
    values[0]["state"] = documents()["state"]
    values[0]["state"]["records"]["REQ-MISSING"] = copy.deepcopy(values[0]["state"]["records"]["REQ-1"])
    values[1]["goals"][0]["findings"][0].update(kind="observation_gap", review=None, evidence=[])
    values[2]["model"]["digest"] = admission.config_digest({"config": values[0]["config"]})
    values[2]["genesis_contract_digest"] = implementation.genesis_digest(*values, WHEN)
    return values


def test_invalid_observed_state_cannot_promote_a_positive_prerequisite() -> None:
    values = invalid_observation_inputs()
    proposal = create(values)
    assert proposal["status"] == "blocked"
    assert len(proposal["tasks"]) == 1 and proposal["tasks"][0]["record"] == "REQ-2"
    observed = proposal["observations"][0]
    assert observed["state"] == "passed" and observed["native_classification"] == "satisfied"
    assert observed["classification"] == "blocked_observations"
    assert observed["validation"]["conforms"] is False and observed["usable_as_prerequisite"] is False
    errors = next(item for item in proposal["blockers"] if item["code"] == "invalid_observations")
    assert errors["findings"] and any("REQ-MISSING" in item["message"] for item in errors["findings"])
    assert all("document" not in item for item in errors["findings"])
    assert any(item["code"] == "unresolved_dependency" for item in proposal["blockers"])
    assert proposal["tasks"][0]["blockers"]
    assert current(proposal, values)["status"] == "blocked"
    assert current(proposal, values)["ready_for_separate_execution_review"] is False


def test_invalid_observation_diagnostics_reproduce_after_file_renaming() -> None:
    values = invalid_observation_inputs()
    first = create(values)
    renamed = ({"model-snapshot.json": values[0]["config"], "observations.json": values[0]["state"]}, *values[1:])
    assert create(renamed) == first
    assert current(first, renamed)["status"] == "blocked"
