"""Delivery envelopes bind exact stage identities without approving or performing effects."""

from __future__ import annotations

import copy
import datetime as dt
import socket
from pathlib import Path
from typing import Any

import pytest
from exacly import delivery_automation as delivery

Doc = dict[str, Any]
NOW = dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC)
REPOSITORY = {"id": 123, "full_name": "owner/product"}
SOURCE = {"repository": REPOSITORY, "revision": "a" * 40}
WORKFLOW = {
    "repository": REPOSITORY,
    "id": ".github/workflows/approved-delivery.yml",
    "revision": "b" * 40,
    "contract_digest": delivery.digest("approved workflow contract"),
}
ARTIFACT = {"name": "product.whl", "digest": delivery.digest("exact approved bytes"), "size": 17}
PREVIOUS_ARTIFACT = {**ARTIFACT, "digest": delivery.digest("exact permitted prior bytes")}
SUBJECTS = {
    "plan": {"source": SOURCE, "intent_digest": delivery.digest("approved product intent")},
    "merge": {
        "repository": REPOSITORY,
        "head_repository": {"id": 456, "full_name": "author/product"},
        "pull_request": 42,
        "head_revision": "c" * 40,
        "base_revision": "d" * 40,
        "base_ref": "refs/heads/main",
        "method": "merge",
    },
    "publish": {
        "workflow": WORKFLOW,
        "source": SOURCE,
        "version": "1.2.3",
        "destination": {"registry": "approved-staging-registry", "package": "product"},
        "artifacts": [ARTIFACT],
    },
    "deploy": {
        "workflow": WORKFLOW,
        "application": "product-api",
        "environment": "staging-1",
        "artifact": ARTIFACT,
        "expected_generation": "resource-version-4",
        "strategy": "approved-canary",
    },
    "rollback": {
        "workflow": WORKFLOW,
        "application": "product-api",
        "environment": "staging-1",
        "artifact": PREVIOUS_ARTIFACT,
        "observed_artifact": ARTIFACT,
        "expected_generation": "resource-version-5",
        "strategy": "approved-bounded-rollback",
        "conditions_digest": delivery.digest("independently approved partial rollout conditions"),
    },
}


def request(stage: str = "merge") -> Doc:
    return copy.deepcopy(
        {
            "schema_version": "0.1.0",
            "profile": "product-delivery-v1",
            "kind": "delivery-request",
            "objective": {
                "id": delivery.digest("original delivery objective"),
                "original_goal": delivery.digest("unchanged original product goal"),
            },
            "operation_key": delivery.digest({"logical_item": "approved change", "stage": stage}),
            "stage": stage,
            "subject": SUBJECTS[stage],
            "native_payload_digest": delivery.digest({"complete reviewed native payload": stage}),
        }
    )


def approval(selected: Doc) -> Doc:
    return {
        "schema_version": "0.1.0",
        "profile": "product-delivery-v1",
        "kind": "delivery-approval",
        "issuer": "independently selected operator",
        "approval_id": "separate delivery review 7",
        "objective": copy.deepcopy(selected["objective"]),
        "stage": selected["stage"],
        "operation_key": selected["operation_key"],
        "request_digest": delivery.digest(selected),
        "subject_digest": delivery.digest(selected["subject"]),
        "resource_key": delivery.resource_key(selected),
        "policy_digest": delivery.digest("approved delivery policy"),
        "adapter": {
            "id": "reviewed-stage-adapter-v1",
            "configuration_digest": delivery.digest("actual adapter config"),
        },
        "contract": {key: delivery.digest(key) for key in ("model", "criteria", "bindings", "observer")},
        "journal": {"path": "/private/operator/delivery.sqlite3", "id": "f" * 12 + "4" + "f" * 3 + "a" + "f" * 15},
        "limits": {"operations": 3, "wall_time_ms": 10000, "reads": 20, "rollbacks": 1},
        "not_before": "2026-11-01T00:00:00Z",
        "expires_at": "2026-11-02T00:00:00Z",
    }


def pin(value: Doc) -> delivery.ApprovalPin:
    return delivery.ApprovalPin(value["issuer"], value["approval_id"], delivery.digest(value))


def identify(value: Doc) -> Doc:
    value["id"] = delivery.digest({key: item for key, item in value.items() if key != "id"})
    return value


def intent(selected: Doc, approved: Doc) -> Doc:
    return identify(
        {
            "schema_version": "0.1.0",
            "profile": "product-delivery-v1",
            "kind": "delivery-intent",
            "request": copy.deepcopy(selected),
            "request_digest": delivery.digest(selected),
            "subject_digest": delivery.digest(selected["subject"]),
            "resource_key": delivery.resource_key(selected),
            "authority_digest": delivery.digest(approved),
            "approval_id": approved["approval_id"],
            "policy_digest": approved["policy_digest"],
            "adapter": copy.deepcopy(approved["adapter"]),
            "journal": copy.deepcopy(approved["journal"]),
            "cost": {
                "operations": 1,
                "wall_time_ms": 5000,
                "reads": 4,
                "rollbacks": int(selected["stage"] == "rollback"),
            },
        }
    )


def effect(selected: Doc) -> Doc:
    subject, stage = selected["subject"], selected["stage"]
    if stage == "plan":
        return {"proposal_id": delivery.digest("retained advisory proposal"), "proposal_revision": 1}
    if stage == "merge":
        return {
            **{
                key: copy.deepcopy(subject[key])
                for key in ("repository", "pull_request", "head_revision", "base_revision", "method")
            },
            "commit": "e" * 40,
            "remote_receipt_digest": delivery.digest("actual provider receipt"),
        }
    if stage == "publish":
        return {
            **copy.deepcopy(subject),
            "publisher_run": "run-123-attempt-1",
            "remote_receipt_digest": delivery.digest("publication"),
        }
    return {
        **{key: copy.deepcopy(subject[key]) for key in ("workflow", "application", "environment", "artifact")},
        "generation": "resource-version-after-rollout",
        "rollout_id": "rollout-789",
        "remote_receipt_digest": delivery.digest("actual rollout provider receipt"),
    }


def receipt(prepared: Doc, outcome: str = "applied") -> Doc:
    return identify(
        {
            "schema_version": "0.1.0",
            "profile": "product-delivery-v1",
            "kind": "delivery-receipt",
            "intent_id": prepared["id"],
            "operation_key": prepared["request"]["operation_key"],
            "request_digest": prepared["request_digest"],
            "authority_digest": prepared["authority_digest"],
            "stage": prepared["request"]["stage"],
            "adapter": copy.deepcopy(prepared["adapter"]),
            "outcome": outcome,
            "effect": effect(prepared["request"]) if outcome == "applied" else None,
            "provenance_digest": delivery.digest("retained exact provider provenance"),
            "reason": "Retained stage-specific facts; no health or later-stage authority claimed.",
            "claims": copy.deepcopy(delivery.CLAIMS),
        }
    )


@pytest.mark.parametrize("stage", delivery.STAGES)
def test_each_stage_has_closed_exact_subject_intent_and_receipt(stage: str) -> None:
    selected = request(stage)
    approved = approval(selected)
    assert delivery.validate_request(selected) == selected
    assert delivery.validate_approval(approved, pin=pin(approved), now=NOW) == approved
    prepared = intent(selected, approved)
    assert delivery.validate_intent(prepared, approval=approved) == prepared
    observed = receipt(prepared)
    assert delivery.validate_receipt(observed, intent=prepared) == observed
    assert not any(observed["claims"].values())


def test_pure_content_validation_neither_reads_files_nor_invokes_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = request()
    approved = approval(selected)
    prepared = intent(selected, approved)
    observed = receipt(prepared)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a pure delivery contract validator crossed an effect boundary")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert delivery.validate_approval(approved, pin=pin(approved), now=NOW) == approved
    assert delivery.validate_intent(prepared, approval=approved) == prepared
    assert delivery.validate_receipt(observed, intent=prepared) == observed
    assert not any(hasattr(delivery, name) for name in ("execute", "authorize", "publish", "deploy", "merge"))


def test_matching_pin_is_content_selection_not_self_approval_or_a_grant() -> None:
    approved = approval(request())
    trusted_pin = pin(approved)
    changed = copy.deepcopy(approved)
    changed["adapter"]["configuration_digest"] = delivery.digest("different installed configuration")
    with pytest.raises(ValueError, match="independently selected pin"):
        delivery.validate_approval(changed, pin=trusted_pin, now=NOW)
    changed = copy.deepcopy(approved)
    changed["approved"] = True
    with pytest.raises(ValueError):
        delivery.validate_approval(changed, pin=pin(changed), now=NOW)
    with pytest.raises(ValueError, match="ApprovalPin"):
        delivery.validate_approval(approved, pin=approved, now=NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["profile", "kind", "stage"])
def test_verification_grants_and_cross_stage_documents_are_not_delivery_approval(field: str) -> None:
    approved = approval(request())
    approved[field] = {"profile": "verification-pilot-v1", "kind": "standing-delegation", "stage": "complete"}[field]
    with pytest.raises(ValueError):
        delivery.validate_approval(approved, pin=pin(approved), now=NOW)


@pytest.mark.parametrize("time", [NOW - dt.timedelta(days=1), NOW + dt.timedelta(days=1)])
def test_approval_must_be_current_under_explicit_aware_clock(time: dt.datetime) -> None:
    approved = approval(request())
    with pytest.raises(ValueError, match="not current"):
        delivery.validate_approval(approved, pin=pin(approved), now=time)


def test_exact_lifetime_end_is_exclusive_and_naive_clock_is_rejected() -> None:
    approved = approval(request())
    for current in (dt.datetime(2026, 11, 2, tzinfo=dt.UTC), NOW.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            delivery.validate_approval(approved, pin=pin(approved), now=current)
    approved["expires_at"] = approved["not_before"]
    with pytest.raises(ValueError, match="lifetime"):
        delivery.validate_approval(approved, pin=pin(approved), now=NOW)


@pytest.mark.parametrize(
    "change",
    ["request", "objective", "stage", "operation", "policy", "adapter", "journal", "scope"],
)
def test_intent_cannot_reuse_approval_after_identity_changes(change: str) -> None:
    selected = request()
    approved = approval(selected)
    prepared = intent(selected, approved)
    if change == "request":
        prepared["request"]["native_payload_digest"] = delivery.digest("changed native request")
    elif change == "objective":
        prepared["request"]["objective"]["original_goal"] = delivery.digest("different objective")
    elif change == "stage":
        prepared["request"] = request("publish")
    elif change == "operation":
        prepared["request"]["operation_key"] = delivery.digest("alias operation")
    elif change == "policy":
        prepared["policy_digest"] = delivery.digest("weaker delivery policy")
    elif change == "adapter":
        prepared["adapter"]["configuration_digest"] = delivery.digest("changed actual adapter")
    elif change == "journal":
        prepared["journal"]["path"] = "/another/private/delivery.sqlite3"
    else:
        prepared["resource_key"] = delivery.digest("unrelated destination")
    identify(prepared)
    with pytest.raises(ValueError):
        delivery.validate_intent(prepared, approval=approved)


def test_resource_conflict_domain_survives_aliases_revisions_and_deployment_rollback() -> None:
    original = request()
    changed = copy.deepcopy(original)
    changed["operation_key"] = delivery.digest("another approval operation key")
    changed["native_payload_digest"] = delivery.digest("updated payload")
    changed["subject"]["head_revision"] = "f" * 40
    assert delivery.resource_key(original) == delivery.resource_key(changed)
    changed["subject"]["pull_request"] += 1
    assert delivery.resource_key(original) == delivery.resource_key(changed)
    changed["subject"]["base_ref"] = "refs/heads/another-protected-base"
    assert delivery.resource_key(original) != delivery.resource_key(changed)
    assert delivery.resource_key(request("deploy")) == delivery.resource_key(request("rollback"))
    publication = request("publish")
    renamed_workflow = copy.deepcopy(publication)
    renamed_workflow["subject"]["workflow"]["id"] = "replacement owning workflow"
    assert delivery.resource_key(publication) == delivery.resource_key(renamed_workflow)


@pytest.mark.parametrize(
    "field,value",
    [("operations", 0), ("operations", True), ("wall_time_ms", 0), ("reads", -1), ("rollbacks", 1)],
)
def test_declared_cost_is_bounded_but_does_not_reserve_or_reset_capacity(field: str, value: int | bool) -> None:
    selected = request()
    approved = approval(selected)
    prepared = intent(selected, approved)
    prepared["cost"][field] = value
    identify(prepared)
    with pytest.raises(ValueError):
        delivery.validate_intent(prepared, approval=approved)


def test_rollback_consumes_its_separately_declared_budget_in_a_future_engine() -> None:
    selected = request("rollback")
    approved = approval(selected)
    prepared = intent(selected, approved)
    approved["limits"]["rollbacks"] = 0
    prepared["authority_digest"] = delivery.digest(approved)
    identify(prepared)
    with pytest.raises(ValueError, match="budget"):
        delivery.validate_intent(prepared, approval=approved)


@pytest.mark.parametrize("outcome", ["uncertain", "rejected", "applied"])
def test_receipts_preserve_exact_outcomes_without_retry_or_fresh_observation_claim(outcome: str) -> None:
    selected = request()
    approved = approval(selected)
    prepared = intent(selected, approved)
    observed = receipt(prepared, outcome)
    assert delivery.validate_receipt(observed, intent=prepared) == observed
    assert delivery.validate_receipt(observed, intent=prepared) == observed
    with pytest.raises(ValueError, match="not current"):
        delivery.validate_approval(approved, pin=pin(approved), now=NOW + dt.timedelta(days=30))
    assert delivery.validate_receipt(observed, intent=prepared) == observed
    assert observed["claims"]["fresh_observation"] is False


@pytest.mark.parametrize("stage", ["merge", "publish", "deploy", "rollback"])
def test_receipt_cannot_substitute_another_effect_or_escalate_to_a_later_stage(stage: str) -> None:
    selected = request(stage)
    prepared = intent(selected, approval(selected))
    observed = receipt(prepared)
    if stage == "merge":
        observed["effect"]["base_revision"] = "f" * 40
    elif stage == "publish":
        observed["effect"]["artifacts"][0]["digest"] = delivery.digest("rebuilt or substituted bytes")
    else:
        observed["effect"]["environment"] = "production"
    identify(observed)
    with pytest.raises(ValueError, match="subject"):
        delivery.validate_receipt(observed, intent=prepared)
    observed = receipt(prepared)
    observed["claims"]["later_stage_authorized"] = True
    identify(observed)
    with pytest.raises(ValueError):
        delivery.validate_receipt(observed, intent=prepared)


@pytest.mark.parametrize("field", ["stage", "operation_key", "authority_digest", "intent_id", "adapter"])
def test_receipt_must_match_every_retained_intent_identity(field: str) -> None:
    selected = request()
    prepared = intent(selected, approval(selected))
    observed = receipt(prepared)
    observed[field] = "deploy" if field == "stage" else delivery.digest("different identity")
    if field == "adapter":
        observed[field] = {"id": "different adapter", "configuration_digest": delivery.digest("different config")}
    identify(observed)
    with pytest.raises(ValueError):
        delivery.validate_receipt(observed, intent=prepared)


def test_applied_requires_an_effect_and_rejected_cannot_claim_one() -> None:
    selected = request()
    prepared = intent(selected, approval(selected))
    observed = receipt(prepared)
    observed["effect"] = None
    identify(observed)
    with pytest.raises(ValueError, match="actual stage effect"):
        delivery.validate_receipt(observed, intent=prepared)
    observed = receipt(prepared)
    observed["outcome"] = "rejected"
    identify(observed)
    with pytest.raises(ValueError, match="cannot claim"):
        delivery.validate_receipt(observed, intent=prepared)


def test_uncertain_rollout_retains_unexpected_artifact_without_health_or_compensation_authority() -> None:
    selected = request("deploy")
    prepared = intent(selected, approval(selected))
    observed = receipt(prepared)
    observed["outcome"] = "uncertain"
    observed["effect"]["artifact"] = copy.deepcopy(PREVIOUS_ARTIFACT)
    observed["reason"] = "Actual rollout bytes differ; retain evidence and reconcile without retry."
    identify(observed)
    assert delivery.validate_receipt(observed, intent=prepared) == observed
    assert not any(observed["claims"].values())
    observed["outcome"] = "applied"
    identify(observed)
    with pytest.raises(ValueError, match="subject"):
        delivery.validate_receipt(observed, intent=prepared)


def test_recomputed_receipt_does_not_hide_an_internally_inconsistent_intent() -> None:
    selected = request()
    prepared = intent(selected, approval(selected))
    prepared["subject_digest"] = delivery.digest("another subject")
    identify(prepared)
    observed = receipt(prepared)
    with pytest.raises(ValueError, match="intent and request"):
        delivery.validate_receipt(observed, intent=prepared)


def test_unknown_payload_fields_and_arbitrary_adapter_loading_are_rejected() -> None:
    selected = request()
    selected["subject"]["bypass_protection"] = True
    with pytest.raises(ValueError):
        delivery.validate_request(selected)
    selected = request()
    selected["native_payload"] = {"arbitrary": "unreviewed effects"}
    with pytest.raises(ValueError):
        delivery.validate_request(selected)
    approved = approval(request())
    approved["adapter"]["module"] = "candidate.module:arbitrary_provider"
    with pytest.raises(ValueError):
        delivery.validate_approval(approved, pin=pin(approved), now=NOW)


@pytest.mark.parametrize("reference", ["main", "refs/heads/main\n", "refs/heads/a.lock/b", "refs/heads/a/../b"])
def test_branch_and_text_identities_cannot_alias_through_malformed_spelling(reference: str) -> None:
    selected = request()
    selected["subject"]["base_ref"] = reference
    with pytest.raises(ValueError):
        delivery.validate_request(selected)


@pytest.mark.parametrize(
    "path",
    ["relative.sqlite3", "/private//delivery.sqlite3", "/private/../delivery.sqlite3", "//private/delivery.sqlite3"],
)
def test_copied_or_aliased_journal_locator_does_not_select_a_new_authority_domain(path: str) -> None:
    approved = approval(request())
    approved["journal"]["path"] = path
    with pytest.raises(ValueError):
        delivery.validate_approval(approved, pin=pin(approved), now=NOW)


def test_documents_are_copied_and_content_identity_detects_later_mutation() -> None:
    selected = request()
    approved = approval(selected)
    prepared = intent(selected, approved)
    retained = delivery.validate_intent(prepared, approval=approved)
    prepared["request"]["subject"]["head_revision"] = "f" * 40
    assert retained["request"]["subject"]["head_revision"] == "c" * 40
    observed = receipt(retained)
    observed["reason"] = "A changed retained receipt cannot replay under the old digest."
    with pytest.raises(ValueError, match="content identity"):
        delivery.validate_receipt(observed, intent=retained)


def test_document_size_and_nonfinite_or_nonjson_inputs_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = request()
    selected["native_payload_digest"] = float("nan")
    with pytest.raises(ValueError):
        delivery.validate_request(selected)
    monkeypatch.setattr(delivery, "MAX_DOCUMENT_BYTES", 100)
    with pytest.raises(ValueError, match="byte limit"):
        delivery.validate_request(request())
