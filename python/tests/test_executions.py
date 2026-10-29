"""Local registration binds one pending item to an output without granting completion authority."""

import copy
import datetime as dt
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from exacly import executions, planning, reconciliation
from exacly.applicability import Target
from exacly.refresh import Run, refresh
from test_admission import ATTEMPT, BASE, READINESS, RUN_ID, Source
from test_applicability import WHEN, A, B
from test_reconciliation import change_live_outcome, make_plan, selected

OPENED = "2026-10-26T08:00:00Z"
TASK = "issues/1"


def run(source: Source, day: dt.date = WHEN) -> Run:
    return Run(
        revision=A,
        model_revision=A,
        context="main",
        observed_at=dt.datetime.combine(day, dt.time(9), tzinfo=dt.UTC),
        workflow=source.workflow_path,
        job=source.job_id,
        job_status="success",
        url="https://example.invalid/observations/1",
    )


@dataclass
class Scenario:
    source: Source
    plan: dict[str, Any]
    state: dict[str, Any]
    ledger: Path

    def request(self, **changes: Any) -> executions.Registration:
        fields: dict[str, Any] = {
            "plan": self.plan,
            "action_id": self.plan["actions"][0]["id"],
            "state": self.state,
            "task": TASK,
            "opened_at": OPENED,
            "readiness_policy": READINESS,
            "trust_policy": self.source.policy,
            "target": selected(B),
            "date": WHEN,
        }
        return executions.Registration(**{**fields, **changes})

    def preview(
        self,
        registered_id: str,
        *,
        output: Target | None = None,
        readiness: dict[str, Any] | None = None,
        trust: dict[str, Any] | None = None,
        plan_doc: dict[str, Any] | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "ledger": self.ledger,
            "binding_id": registered_id,
            "state_doc": self.state,
        }
        plan = self.plan if plan_doc is None else plan_doc
        return reconciliation.preview(
            self.source,
            plan,
            plan["actions"][0]["id"],
            READINESS if readiness is None else readiness,
            self.source.policy if trust is None else trust,
            selected(B) if output is None else output,
            RUN_ID,
            ATTEMPT,
            WHEN,
            **{**fields, **changes},
        )

    def refreshed(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        return refresh(
            {self.source.model_file: self.source.config},
            {},
            run(self.source),
            previous=self.state if state is None else state,
        )


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    source = Source()
    state = refresh({source.model_file: source.config}, {}, run(source))
    state["pending"] = [{"record": "REQ-1", "task": TASK, "opened_at": OPENED, "dimension": "verification"}]
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return Scenario(source, make_plan(source), state, private / "executions.sqlite3")


def test_registration_is_local_and_preserves_supplied_artifacts(scenario: Scenario) -> None:
    request = scenario.request()
    before = copy.deepcopy((scenario.plan, scenario.state, scenario.source.policy, READINESS))
    record = executions.register(scenario.ledger, request)
    envelope = executions.get(scenario.ledger, record["id"])
    assert envelope["binding"] == record
    assert envelope["active"] is True and envelope["superseded_by"] is None
    assert record["contract"]["target"] == selected(B).data
    assert record["contract"]["original_target"] == selected(A).data
    assert record["state_snapshot"] == {
        "serial": scenario.state["serial"],
        "digest": planning.digest(scenario.state),
        "config_revision": A,
    }
    assert scenario.source.calls == [] and scenario.source.downloads == []
    assert (scenario.plan, scenario.state, scenario.source.policy, READINESS) == before
    assert scenario.ledger.stat().st_mode & 0o777 == 0o600


def test_pending_selection_canonicalizes_equivalent_utc_instants(scenario: Scenario) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    repeated = executions.register(scenario.ledger, scenario.request(opened_at="2026-10-26T10:00:00+02:00"))
    assert repeated == first


@pytest.mark.parametrize("opened", ["2026-10-26T08:00:00", "not-a-timestamp"])
def test_pending_selection_requires_an_explicit_valid_timezone(scenario: Scenario, opened: str) -> None:
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request(opened_at=opened))
    assert not scenario.ledger.exists()


def test_repeated_contract_registration_retains_the_first_immutable_record(scenario: Scenario) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    assert executions.register(scenario.ledger, scenario.request()) == first
    later = scenario.refreshed()
    repeated = executions.register(scenario.ledger, scenario.request(state=later, date=WHEN + dt.timedelta(days=1)))
    assert repeated == first
    assert executions.get(scenario.ledger, first["id"])["binding"] == first


@pytest.mark.parametrize("case", ["lower_serial", "same_serial_changed_content"])
def test_registration_rejects_state_rollback_or_rewrite_at_a_seen_serial(scenario: Scenario, case: str) -> None:
    current = scenario.refreshed()
    executions.register(scenario.ledger, scenario.request(state=current, date=WHEN + dt.timedelta(days=1)))
    if case == "lower_serial":
        changed = scenario.state
    else:
        changed = copy.deepcopy(current)
        changed["x-note"] = "different content at the same serial"
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request(state=changed, date=WHEN + dt.timedelta(days=1)))


@pytest.mark.parametrize("case", ["missing", "task", "opened", "dimension", "duplicate"])
def test_registration_selects_exactly_one_pending_verification_item(scenario: Scenario, case: str) -> None:
    if case == "missing":
        scenario.state["pending"] = []
    elif case == "task":
        scenario.state["pending"][0]["task"] = "issues/other"
    elif case == "opened":
        scenario.state["pending"][0]["opened_at"] = "2026-10-26T07:00:00Z"
    elif case == "dimension":
        scenario.state["pending"][0]["dimension"] = "delivery"
    else:
        scenario.state["pending"].append(copy.deepcopy(scenario.state["pending"][0]))
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request())
    assert not scenario.ledger.exists()


def test_missing_pending_dimension_requires_explicit_verification_adoption(scenario: Scenario) -> None:
    scenario.state["pending"][0].pop("dimension")
    before = copy.deepcopy(scenario.state)
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request())
    record = executions.register(scenario.ledger, scenario.request(adopt_verification=True))
    assert executions.get(scenario.ledger, record["id"])["active"] is True
    assert scenario.state == before


@pytest.mark.parametrize("field,value", [("namespace", "other"), ("context", "other"), ("config_revision", "c" * 40)])
def test_registration_state_scope_must_match_the_plan(scenario: Scenario, field: str, value: str) -> None:
    scenario.state[field] = value
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request())


@pytest.mark.parametrize("case", ["serial_bool", "timestamp_object", "invalid_timestamp", "cycle"])
def test_registration_validates_original_state_values_and_formats(scenario: Scenario, case: str) -> None:
    if case == "serial_bool":
        scenario.state["serial"] = True
    elif case == "timestamp_object":
        scenario.state["pending"][0]["opened_at"] = dt.datetime(2026, 10, 26, 8, tzinfo=dt.UTC)
    elif case == "invalid_timestamp":
        scenario.state["written_at"] = "not-a-timestamp"
    else:
        scenario.state["x-cycle"] = scenario.state
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request())
    assert not scenario.ledger.exists()


@pytest.mark.parametrize("case", ["output", "readiness", "trust", "action"])
def test_conflicting_contracts_require_explicit_supersession(scenario: Scenario, case: str) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    changes: dict[str, Any] = {}
    if case == "output":
        changes["target"] = selected("c" * 40)
    elif case == "trust":
        changed = copy.deepcopy(scenario.source.policy)
        changed["version"] = "2"
        changes["trust_policy"] = changed
    elif case == "readiness":
        changed = copy.deepcopy(READINESS)
        changed["version"] = "2"
        plan = make_plan(scenario.source, readiness=changed)
        changes.update(readiness_policy=changed, plan=plan, action_id=plan["actions"][0]["id"])
    else:
        document = copy.deepcopy(scenario.source.config)
        next(item for item in document["records"] if item["id"] == "REQ-1")["statement"] = "Changed behavior."
        plan = make_plan(scenario.source, documents={scenario.source.model_file: document})
        changes.update(plan=plan, action_id=plan["actions"][0]["id"])
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request(**changes))
    assert executions.get(scenario.ledger, record["id"])["active"] is True


def test_explicit_supersession_appends_history_and_deactivates_the_exact_predecessor(scenario: Scenario) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    replacement = scenario.request(target=selected("c" * 40), supersedes=first["id"])
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, replace(replacement, supersedes="sha256:" + "0" * 64))
    second = executions.register(scenario.ledger, replacement)
    assert first["id"] != second["id"]
    old = executions.get(scenario.ledger, first["id"])
    assert old["binding"] == first and old["active"] is False
    assert old["superseded_by"] == second["id"]
    assert executions.get(scenario.ledger, second["id"])["active"] is True
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request())
    report = scenario.preview(first["id"])
    assert report["status"] == "stale"
    assert scenario.source.calls == []


@pytest.mark.parametrize("case", ["task", "lineage"])
def test_a_new_pending_identity_or_lineage_needs_its_own_explicit_registration(scenario: Scenario, case: str) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    if case == "task":
        scenario.state["pending"][0]["task"] = "issues/2"
        request = scenario.request(task="issues/2")
    else:
        scenario.state["lineage"] = "another-state-lineage"
        request = scenario.request()
    assert scenario.preview(first["id"])["status"] == "stale"
    assert scenario.source.calls == []
    second = executions.register(scenario.ledger, request)
    assert first["id"] != second["id"]
    assert executions.get(scenario.ledger, first["id"])["active"] is True
    assert executions.get(scenario.ledger, second["id"])["active"] is True


def test_valid_bound_preview_is_read_only_and_still_requires_live_admission(scenario: Scenario) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    ledger_before = scenario.ledger.read_bytes()
    inputs_before = copy.deepcopy((scenario.plan, scenario.state, scenario.source.policy))
    files_before = sorted(path.name for path in scenario.ledger.parent.iterdir())
    report = scenario.preview(record["id"])
    assert report["status"] == "ready", report
    assert report["binding"]["id"] == record["id"]
    assert report["admission"]["status"] == "admitted"
    assert report["claims"]["task_completion_authorized"] is False
    assert report["claims"]["executor_output_link_verified"] is False
    assert report["claims"]["registered_binding_verified"] is True
    assert scenario.source.downloads
    assert scenario.ledger.read_bytes() == ledger_before
    assert sorted(path.name for path in scenario.ledger.parent.iterdir()) == files_before
    assert (scenario.plan, scenario.state, scenario.source.policy) == inputs_before
    scenario.source.pages[f"{BASE}/branches/main"]["protected"] = False
    assert scenario.preview(record["id"])["status"] == "pending"
    assert scenario.ledger.read_bytes() == ledger_before


@pytest.mark.parametrize("protection", [None, "required", "not_required"])
def test_bound_preview_honors_the_registered_branch_protection_policy(
    scenario: Scenario, protection: str | None
) -> None:
    scenario.source.pages[f"{BASE}/branches/main"]["protected"] = False
    if protection is not None:
        scenario.source.policy["branch_protection"] = protection
    record = executions.register(scenario.ledger, scenario.request())
    before = scenario.ledger.read_bytes(), copy.deepcopy(scenario.state)
    report = scenario.preview(record["id"])
    assert report["status"] == ("ready" if protection == "not_required" else "pending"), report
    assert report["admission"]["status"] == ("admitted" if protection == "not_required" else "rejected")
    assert report["admission"]["claims"]["branch_protection_verified"] is False
    assert report["claims"]["registered_binding_verified"] is True
    assert (scenario.ledger.read_bytes(), scenario.state) == before


def test_waiving_protection_requires_explicit_binding_supersession(scenario: Scenario) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    opted_out = copy.deepcopy(scenario.source.policy)
    opted_out["branch_protection"] = "not_required"
    before = scenario.ledger.read_bytes(), copy.deepcopy(scenario.state)
    report = scenario.preview(first["id"], trust=opted_out)
    assert report["status"] == "stale" and report["admission"] is None
    assert scenario.source.calls == [] and scenario.source.downloads == []
    assert (scenario.ledger.read_bytes(), scenario.state) == before
    with pytest.raises(ValueError):
        executions.register(scenario.ledger, scenario.request(trust_policy=opted_out))
    replacement = executions.register(scenario.ledger, scenario.request(trust_policy=opted_out, supersedes=first["id"]))
    assert replacement["contract"]["trust_policy"]["digest"] != first["contract"]["trust_policy"]["digest"]
    assert executions.get(scenario.ledger, first["id"])["active"] is False
    scenario.source.policy = opted_out
    scenario.source.pages[f"{BASE}/branches/main"]["protected"] = False
    assert scenario.preview(replacement["id"])["status"] == "ready"


def test_supersession_during_live_admission_invalidates_the_preflight_binding(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    before = copy.deepcopy((scenario.plan, scenario.state))
    original = scenario.source.get_json
    replacement: dict[str, Any] | None = None

    def supersede_on_first_fetch(path: str) -> dict[str, Any]:
        nonlocal replacement
        if replacement is None:
            replacement = executions.register(
                scenario.ledger, scenario.request(target=selected("c" * 40), supersedes=first["id"])
            )
        return original(path)

    monkeypatch.setattr(scenario.source, "get_json", supersede_on_first_fetch)
    report = scenario.preview(first["id"])
    assert replacement is not None
    assert report["admission"]["status"] == "admitted"
    assert report["status"] == "stale", report
    assert report["binding"]["active"] is False
    assert report["binding"]["superseded_by"] == replacement["id"]
    assert report["claims"]["registered_binding_verified"] is False
    assert (scenario.plan, scenario.state) == before


def test_watermark_advance_during_live_admission_invalidates_an_older_state_snapshot(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    later = scenario.refreshed()
    before = copy.deepcopy((scenario.plan, scenario.state, later))
    original = scenario.source.get_json
    advanced = False

    def advance_on_first_fetch(path: str) -> dict[str, Any]:
        nonlocal advanced
        if not advanced:
            advanced = True
            assert executions.register(scenario.ledger, scenario.request(state=later)) == first
        return original(path)

    monkeypatch.setattr(scenario.source, "get_json", advance_on_first_fetch)
    report = scenario.preview(first["id"])
    assert advanced
    assert report["admission"]["status"] == "admitted"
    assert report["status"] == "stale", report
    assert report["binding"]["active"] is True
    assert report["binding"]["state_watermark"]["serial"] == later["serial"]
    assert report["claims"]["registered_binding_verified"] is False
    assert (scenario.plan, scenario.state, later) == before


def test_bound_preview_does_not_accept_failed_verification(scenario: Scenario) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    scenario.source.report = scenario.source.report.replace(b"</testcase>", b"<failure/></testcase>")
    change_live_outcome(scenario.source, "failure")
    report = scenario.preview(record["id"])
    assert report["status"] == "pending"
    assert report["admission"]["status"] == "admitted"


@pytest.mark.parametrize(
    "case", ["pending", "pending_metadata", "namespace", "context", "config", "lineage", "serial", "same_serial"]
)
def test_bound_preview_rejects_changed_or_rolled_back_state_before_source_access(scenario: Scenario, case: str) -> None:
    current = scenario.refreshed()
    record = executions.register(scenario.ledger, scenario.request(state=current, date=WHEN + dt.timedelta(days=1)))
    if case == "serial":
        state = scenario.state
    else:
        state = copy.deepcopy(current)
        if case == "pending":
            state["pending"] = []
        elif case == "pending_metadata":
            state["pending"][0]["x-owner"] = "someone else"
            state["serial"] += 1
        elif case == "same_serial":
            state["x-other"] = "changed at the captured serial"
        else:
            state[{"config": "config_revision"}.get(case, case)] = "changed"
    report = scenario.preview(record["id"], state_doc=state)
    assert report["status"] == "stale", report
    assert scenario.source.calls == [] and report["admission"] is None


def test_later_refresh_with_the_same_pending_entry_can_use_the_registered_binding(scenario: Scenario) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    later = scenario.refreshed()
    report = scenario.preview(record["id"], state_doc=later)
    assert report["status"] == "ready", report


def test_idempotent_registration_advances_the_rollback_guard_without_rewriting_the_binding(scenario: Scenario) -> None:
    first = executions.register(scenario.ledger, scenario.request())
    later = scenario.refreshed()
    assert executions.register(scenario.ledger, scenario.request(state=later)) == first
    report = scenario.preview(first["id"])
    assert report["status"] == "stale", report
    assert scenario.source.calls == []
    assert scenario.preview(first["id"], state_doc=later)["status"] == "ready"


@pytest.mark.parametrize("case", ["target", "readiness", "trust", "goal"])
def test_bound_preview_requires_the_registered_goal_policies_and_output(scenario: Scenario, case: str) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    changes: dict[str, Any] = {}
    if case == "target":
        changes["output"] = selected("c" * 40)
    elif case == "readiness":
        readiness = copy.deepcopy(READINESS)
        readiness["version"] = "2"
        changes.update(readiness=readiness, plan_doc=make_plan(scenario.source, readiness=readiness))
    elif case == "trust":
        trust = copy.deepcopy(scenario.source.policy)
        trust["version"] = "2"
        changes["trust"] = trust
    else:
        document = copy.deepcopy(scenario.source.config)
        next(item for item in document["records"] if item["id"] == "REQ-1")["statement"] = "Changed behavior."
        changes["plan_doc"] = make_plan(scenario.source, documents={scenario.source.model_file: document})
    report = scenario.preview(record["id"], **changes)
    assert report["status"] == "stale", report
    assert scenario.source.calls == [] and report["admission"] is None


@pytest.mark.parametrize("missing", ["ledger", "binding_id", "state_doc"])
def test_bound_preview_arguments_are_required_together(scenario: Scenario, missing: str) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    with pytest.raises(ValueError):
        scenario.preview(record["id"], **{missing: None})
    assert scenario.source.calls == []


def test_bound_preview_does_not_create_or_recover_a_missing_ledger(scenario: Scenario) -> None:
    with pytest.raises(ValueError):
        scenario.preview("sha256:" + "0" * 64)
    assert not scenario.ledger.exists()
    assert scenario.source.calls == []


def test_bound_preview_rejects_an_unsafe_ledger_before_source_access(scenario: Scenario) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    scenario.ledger.chmod(0o644)
    with pytest.raises(ValueError):
        scenario.preview(record["id"])
    assert scenario.source.calls == []


def test_bound_preview_rejects_malformed_current_state_before_source_access(scenario: Scenario) -> None:
    record = executions.register(scenario.ledger, scenario.request())
    invalid = copy.deepcopy(scenario.state)
    invalid["serial"] = True
    with pytest.raises(ValueError):
        scenario.preview(record["id"], state_doc=invalid)
    assert scenario.source.calls == []
