"""A reconciliation preview needs a current admitted observation for one unchanged plan goal."""

import copy
import datetime as dt
from typing import Any

import jsonschema
import pytest
import yaml
from exacly import admission, planning, reconciliation
from exacly.applicability import Target
from exacly.github_evidence import GitHubError
from exacly.refresh import Run, refresh
from test_admission import (
    ARTIFACT_ID,
    ATTEMPT,
    BASE,
    CASE,
    IMPLEMENTATION_TREE,
    MODEL_TREE,
    READINESS,
    REPOSITORY,
    RUN_ID,
    Source,
    sha256,
)
from test_applicability import WHEN, A, B
from test_refresh import config, requirement


def selected(revision: str = B, **fields: Any) -> Target:
    return Target({"context": "main", "model_revision": A, "revision": revision, **fields})


def make_plan(
    source: Source,
    *,
    readiness: dict[str, Any] | None = None,
    documents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return planning.create(documents or {source.model_file: source.config}, readiness or READINESS, selected(A), WHEN)


def preview(
    source: Source,
    plan: dict[str, Any] | None = None,
    *,
    readiness: dict[str, Any] | None = None,
    target: Target | None = None,
    action_id: str | None = None,
    date: dt.date = WHEN,
) -> dict[str, Any]:
    plan = plan if plan is not None else make_plan(source, readiness=readiness)
    return reconciliation.preview(
        source,
        plan,
        action_id or plan["actions"][0]["id"],
        readiness or READINESS,
        source.policy,
        target or selected(),
        RUN_ID,
        ATTEMPT,
        date,
    )


def rehash(plan: dict[str, Any]) -> None:
    plan.pop("digest", None)
    plan["digest"] = planning.digest(plan)


def update_model(source: Source, document: dict[str, Any]) -> None:
    source.config = document
    source.pages[f"{BASE}/git/trees/{MODEL_TREE}"]["tree"] = [
        source.add_blob(source.model_file, yaml.safe_dump(document).encode())
    ]
    source.policy["model"]["digest"] = admission.config_digest({source.model_file: document})


def change_live_outcome(source: Source, outcome: str) -> None:
    source.run["conclusion"] = source.job["conclusion"] = source.job["steps"][0]["conclusion"] = outcome
    source.manifest["check_outcome"] = outcome
    source.manifest["report_digest"] = sha256(source.report)
    source.rebundle()


def test_a_new_implementation_can_satisfy_the_selected_unchanged_goal() -> None:
    source = Source()
    plan = make_plan(source)
    before = copy.deepcopy((plan, source.policy, source.pages, READINESS))
    report = preview(source, plan)
    assert report["status"] == "ready", report
    assert report["advisory"] is True and report["dry_run"] is True
    assert report["claims"] == {
        "registered_binding_verified": False,
        "task_completion_authorized": False,
        "executor_output_link_verified": False,
        "self_authenticating": False,
        "test_honesty_verified": False,
        "coverage_verified": False,
    }
    assert report["original_target"] == selected(A).data
    assert report["target"] == selected(B).data
    assert report["plan"] == {"digest": plan["digest"], "action_id": plan["actions"][0]["id"]}
    assert report["goal"] == {
        "record": "REQ-1",
        "dimension": "verification",
        "criteria_digest": plan["actions"][0]["criteria_digest"],
        "bindings_digest": plan["actions"][0]["bindings_digest"],
    }
    assert report["admission"]["status"] == "admitted"
    assert report["readiness"]["result"] == "satisfied"
    assert report["policy"]["digest"] == planning.digest(READINESS)
    assert report["trust_policy"]["digest"] == planning.digest(source.policy)
    assert (plan, source.policy, source.pages, READINESS) == before
    jsonschema.validate(report, reconciliation.SCHEMA)
    payload = {key: value for key, value in report.items() if key != "digest"}
    assert report["digest"] == planning.digest(payload)
    rendered = reconciliation.as_text(report)
    assert "REQ-1" in rendered and "ready" in rendered.lower()
    assert "advisory" in rendered.lower() and "dry" in rendered.lower()


def test_reconciliation_can_confirm_the_same_implementation_revision() -> None:
    source = Source()
    source.pages[f"{BASE}/git/commits/{A}"]["tree"] = {"sha": IMPLEMENTATION_TREE}
    source.run["head_sha"] = source.job["head_sha"] = A
    source.artifact["workflow_run"]["head_sha"] = A
    source.manifest["implementation_revision"] = A
    source.rebundle()
    assert preview(source, target=selected(A))["status"] == "ready"


@pytest.mark.parametrize("case", ["failed", "skipped", "unrelated_case", "partial"])
def test_admitted_incomplete_or_failed_checks_leave_the_goal_pending(case: str) -> None:
    source = Source("failure") if case == "failed" else Source()
    if case == "skipped":
        source.report = source.report.replace(b"</testcase>", b"<skipped/></testcase>")
    elif case == "unrelated_case":
        source.report = b'<testsuite><testcase name="unrelated"/></testsuite>'
    elif case == "partial":
        document = copy.deepcopy(source.config)
        requirement_record = next(record for record in document["records"] if record["id"] == "REQ-1")
        requirement_record["observe"]["tests"]["cases"].append("python/tests/test_other.py::test_other")
        update_model(source, document)
    change_live_outcome(source, "failure" if case == "failed" else "success")
    report = preview(source)
    assert report["status"] == "pending", report
    assert report["admission"]["status"] == "admitted"
    assert report["readiness"]["result"] != "satisfied"


def test_rejected_live_origin_cannot_be_replaced_by_plan_observations() -> None:
    source = Source()
    source.pages[f"{BASE}/branches/main"]["protected"] = False
    report = preview(source)
    assert report["status"] == "pending", report
    assert report["admission"]["status"] == "rejected"
    assert report["readiness"] is None


def test_a_model_refetch_failure_after_admission_leaves_the_preview_pending() -> None:
    class FailsOnRefetch(Source):
        reads = 0

        def get_json(self, relative_path: str) -> dict[str, Any]:
            if relative_path.lstrip("/") == f"{BASE}/git/commits/{A}":
                self.reads += 1
                if self.reads == 2:
                    raise GitHubError("the immutable model could not be fetched again")
            return super().get_json(relative_path)

    report = preview(FailsOnRefetch())
    assert report["status"] == "pending", report
    assert report["admission"]["status"] == "admitted"
    assert report["readiness"] is None


def test_old_failed_plan_checks_are_replaced_by_a_live_admitted_observation() -> None:
    source = Source()
    documents = {source.model_file: source.config}
    documents["state"] = refresh(
        documents,
        {CASE: "failed"},
        Run(
            revision=A,
            model_revision=A,
            context="main",
            observed_at=dt.datetime.combine(WHEN, dt.time(8), tzinfo=dt.UTC),
            workflow=source.workflow_path,
            job=source.job_id,
            job_status="failure",
            url="https://old.invalid/runs/1",
        ),
    )
    plan = make_plan(source, documents=documents)
    assert plan["actions"][0]["kind"] == "investigate_failure"
    assert plan["checks"][0]["state"] == "failed"
    assert preview(source, plan)["status"] == "ready"


def test_selected_goal_can_be_ready_while_another_policy_goal_is_unresolved() -> None:
    source = Source()
    binding = {"workflow": source.workflow_path, "job": source.job_id, "cases": [CASE]}
    update_model(
        source,
        config(
            requirement("REQ-1", binding),
            requirement("REQ-2", {**binding, "cases": ["python/tests/test_other.py::test_other"]}),
        ),
    )
    readiness = copy.deepcopy(READINESS)
    readiness["requirements"].append({"record": "REQ-2", "dimensions": ["verification"]})
    report = preview(source, readiness=readiness)
    assert report["status"] == "ready", report
    assert report["goal"]["record"] == "REQ-1"
    assert report["readiness"]["result"] != "satisfied"
    assert {check["record"] for check in report["readiness"]["checks"]} == {"REQ-1", "REQ-2"}


def test_every_preview_rechecks_live_evidence_without_mutating_the_plan() -> None:
    source = Source()
    plan = make_plan(source)
    before = copy.deepcopy(plan)
    assert preview(source, plan)["status"] == "ready"
    assert preview(source, plan)["status"] == "ready"
    source.report = source.report.replace(b"</testcase>", b"<failure/></testcase>")
    change_live_outcome(source, "failure")
    assert preview(source, plan)["status"] == "pending"
    assert source.downloads == [(REPOSITORY, ARTIFACT_ID)] * 3
    assert plan == before


@pytest.mark.parametrize("field,value", [("model_revision", "c" * 40), ("context", "release")])
def test_model_or_context_changes_make_the_plan_stale(field: str, value: str) -> None:
    report = preview(Source(), target=selected(**{field: value}))
    assert report["status"] == "stale", report


@pytest.mark.parametrize("case", ["version", "name", "requirements"])
def test_readiness_policy_changes_make_the_plan_stale(case: str) -> None:
    source = Source()
    plan = make_plan(source)
    readiness = copy.deepcopy(READINESS)
    if case == "requirements":
        readiness["requirements"].append({"record": "REQ-OTHER", "dimensions": ["verification"]})
    else:
        readiness[case] += "-changed"
    assert preview(source, plan, readiness=readiness)["status"] == "stale"


@pytest.mark.parametrize("case", ["namespace", "config", "criteria", "bindings"])
def test_a_valid_plan_from_changed_model_inputs_is_stale(case: str) -> None:
    source = Source()
    changed = copy.deepcopy(source.config)
    record = next(record for record in changed["records"] if record["id"] == "REQ-1")
    if case == "namespace":
        changed["model"]["namespace"] = "other-model"
    elif case == "config":
        changed["model"]["product"] = "Another product"
    elif case == "criteria":
        record["acceptance_criteria"].append("A new behavior is required.")
    else:
        record["observe"]["tests"]["cases"] = ["python/tests/test_new.py::test_new"]
    plan = make_plan(source, documents={source.model_file: changed})
    assert preview(source, plan)["status"] == "stale"


@pytest.mark.parametrize("case", ["criteria", "bindings"])
def test_goal_fingerprints_are_rechecked_even_when_the_config_fingerprint_matches(case: str) -> None:
    source = Source()
    changed = copy.deepcopy(source.config)
    record = next(record for record in changed["records"] if record["id"] == "REQ-1")
    if case == "criteria":
        record["statement"] = "A different behavior is required."
    else:
        record["observe"]["tests"]["cases"].append("python/tests/test_added.py::test_added")
    plan = make_plan(source, documents={source.model_file: changed})
    plan["inputs"]["config"]["digest"] = make_plan(source)["inputs"]["config"]["digest"]
    rehash(plan)
    assert preview(source, plan)["status"] == "stale"


@pytest.mark.parametrize(
    "case",
    [
        "schema",
        "digest",
        "duplicate_action",
        "missing_action",
        "duplicate_check",
        "duplicate_goal",
        "missing_check",
        "record",
        "dimension",
        "kind",
        "completion",
        "completion_record",
        "identity",
    ],
)
def test_invalid_or_internally_inconsistent_plan_artifacts_are_invalid_input(case: str) -> None:
    source = Source()
    plan = make_plan(source)
    action_id = plan["actions"][0]["id"]
    if case == "schema":
        plan["unrecognized"] = True
    elif case == "digest":
        plan["digest"] = "sha256:" + "0" * 64
    elif case == "duplicate_action":
        plan["actions"].append(copy.deepcopy(plan["actions"][0]))
    elif case == "missing_action":
        plan["actions"] = []
    elif case == "duplicate_check":
        plan["checks"].append(copy.deepcopy(plan["checks"][0]))
    elif case == "duplicate_goal":
        duplicate = copy.deepcopy(plan["checks"][0])
        duplicate.pop("action_id")
        plan["checks"].append(duplicate)
    elif case == "missing_check":
        plan["checks"] = []
    elif case == "record":
        plan["checks"][0]["record"] = "REQ-OTHER"
    elif case == "dimension":
        plan["checks"][0]["dimension"] = "delivery"
    elif case == "kind":
        plan["checks"][0]["classification"] = "investigate_failure"
    elif case == "completion":
        plan["actions"][0]["completion"]["target"] = selected(B).data
    elif case == "completion_record":
        plan["actions"][0]["completion"]["record"] = "REQ-OTHER"
    else:
        action_id = "sha256:" + "0" * 64
        plan["actions"][0]["id"] = plan["checks"][0]["action_id"] = action_id
    if case != "digest":
        rehash(plan)
    with pytest.raises(ValueError):
        preview(source, plan, action_id=action_id)


def test_an_unknown_selected_action_is_invalid_input() -> None:
    with pytest.raises(ValueError):
        preview(Source(), action_id="sha256:" + "0" * 64)


def test_cyclic_saved_plan_evidence_is_invalid_before_any_source_access() -> None:
    source = Source()
    plan = make_plan(source)
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    plan["checks"][0]["evidence"] = [cycle]
    with pytest.raises(ValueError):
        preview(source, plan)
    assert source.calls == [] and source.downloads == []


def test_an_invalid_current_trust_policy_is_invalid_input() -> None:
    source = Source()
    source.policy["implicit_trust"] = True
    with pytest.raises(ValueError):
        preview(source)


def test_the_evaluation_date_cannot_precede_the_plan() -> None:
    with pytest.raises(ValueError):
        preview(Source(), date=WHEN - dt.timedelta(days=1))


@pytest.mark.parametrize("field,value", [("revision", "b" * 7), ("environment", "production")])
def test_unsupported_or_abbreviated_output_targets_are_invalid_input(field: str, value: str) -> None:
    fields = {"context": "main", "model_revision": A, "revision": B, field: value}
    with pytest.raises(ValueError):
        preview(Source(), target=Target(fields))
