"""Approved dispatch keeps one external effect separate from verification and completion."""

from __future__ import annotations

import base64
import copy
import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import FakeIssues, Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import dispatch, dispatch_store, executions, github_dispatch, planning, state_writer


def test_preview_is_read_only_and_does_not_require_remote_credentials(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    before = item.state_path.read_bytes(), item.journal.read_bytes()
    report = dispatch.preview(item.documents, item.state_path, item.request)
    assert report["status"] == "ready" and report["dry_run"] is True
    assert report["authorization_template"]["dispatch_id"] == report["dispatch_id"]
    assert report["effect"]["body"].endswith(f"<!-- exacly-dispatch:v1 {report['dispatch_id']} -->")
    assert (item.state_path.read_bytes(), item.journal.read_bytes()) == before
    assert not item.ledger.exists() and item.client.calls == []


def test_apply_creates_one_task_and_one_pending_binding(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    before = item.state
    report = item.apply()
    assert report["status"] == "registered", report
    receipt = report["receipt"]
    binding = executions.get(item.ledger, receipt["binding_id"])["binding"]
    assert item.client.calls.count("create") == 1
    assert binding["contract"]["pending"]["task"] == receipt["remote"]["url"]
    assert binding["claims"]["task_completion_authorized"] is False
    after = item.state
    assert after["serial"] == before["serial"] + 1
    assert after["records"] == before["records"]
    assert after["pending"] == [
        {
            "record": "REQ-1",
            "dimension": "verification",
            "task": receipt["remote"]["url"],
            "opened_at": "2026-10-30T09:00:00+00:00",
        }
    ]
    assert report["claims"]["verification_established"] is False


def test_repeated_apply_returns_original_receipt_without_remote_access(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    first = item.apply()
    before = item.state_path.read_bytes(), item.journal.read_bytes(), item.ledger.read_bytes()
    calls = list(item.client.calls)
    repeated = item.apply()
    assert repeated["status"] == "registered" and repeated["receipt"] == first["receipt"]
    assert item.client.calls == calls
    assert (item.state_path.read_bytes(), item.journal.read_bytes(), item.ledger.read_bytes()) == before


@pytest.mark.parametrize(
    "field", ["dispatch_id", "action_id", "policy_digest", "effect_digest", "repository", "effect"]
)
def test_wrong_authorization_never_creates_work(dispatch_scenario: Scenario, field: str) -> None:
    item = dispatch_scenario
    authorization = item.authorization
    authorization[field] = {"full_name": "other/repository", "id": 999} if field == "repository" else "wrong"
    before = item.journal.read_bytes(), item.state_path.read_bytes()
    with pytest.raises(ValueError):
        item.apply(authorization=authorization)
    assert item.client.calls == []
    assert (item.journal.read_bytes(), item.state_path.read_bytes()) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter", "unknown"),
        ("max_attempts", 0),
        ("max_attempts", 11),
        ("extra", True),
        ("issue", {"title": "", "body": "x"}),
    ],
)
def test_invalid_dispatch_policies_are_closed(dispatch_scenario: Scenario, field: str, value: Any) -> None:
    item = dispatch_scenario
    chosen = {**item.policy, field: value}
    with pytest.raises(ValueError):
        dispatch.preview(item.documents, item.state_path, dataclasses.replace(item.request, dispatch_policy=chosen))
    assert item.client.calls == []


def test_policy_change_does_not_create_another_identity(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    first = item.apply()
    chosen = copy.deepcopy(item.policy)
    chosen["version"] = "2"
    changed = dataclasses.replace(item.request, dispatch_policy=chosen)
    preview = dispatch.preview(item.documents, item.state_path, changed)
    assert preview["dispatch_id"] == first["dispatch_id"]
    report = dispatch.apply(
        item.journal,
        item.ledger,
        item.documents,
        item.state_path,
        changed,
        authorization=preview["authorization_template"],
        client=item.client,
    )
    assert report["status"] == "conflict"
    assert item.client.calls.count("create") == 1


def test_ambiguous_create_reconciles_positive_remote_identity_without_second_post(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario

    class TimedOutAfterCreation(FakeIssues):
        def create(self, repository: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
            super().create(repository, effect)
            raise github_dispatch.GitHubDispatchError("response lost")

    client = TimedOutAfterCreation()
    report = item.apply(client=client)
    assert report["status"] == "ambiguous", report
    assert not item.ledger.exists() and "pending" not in item.state
    recovered = item.apply(client=client, recover=True)
    assert recovered["status"] == "registered", recovered
    assert client.calls.count("create") == 1


def test_zero_matches_after_create_never_authorize_retry(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario

    class UnknownCreate(FakeIssues):
        def create(self, repository: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
            self.calls.append("create")
            raise github_dispatch.GitHubDispatchError("unknown outcome")

    client = UnknownCreate()
    assert item.apply(client=client)["status"] == "ambiguous"
    for _ in range(3):
        assert item.apply(client=client)["status"] == "ambiguous"
    assert item.apply(client=client)["status"] == "attempts_exhausted"
    assert client.calls.count("create") == 1


def test_repository_rename_preserves_an_ambiguous_create_barrier(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario

    class UnknownCreate(FakeIssues):
        def create(self, repository: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
            self.calls.append("create")
            raise github_dispatch.GitHubDispatchError("unknown outcome")

    client = UnknownCreate()
    first = item.apply(client=client)
    assert first["status"] == "ambiguous"
    chosen = copy.deepcopy(item.policy)
    chosen["repository"]["full_name"] = "acme/renamed"
    renamed = dataclasses.replace(item.request, dispatch_policy=chosen)
    preview = dispatch.preview(item.documents, item.state_path, renamed)
    assert preview["dispatch_id"] == first["dispatch_id"]
    report = dispatch.apply(
        item.journal,
        item.ledger,
        item.documents,
        item.state_path,
        renamed,
        authorization=preview["authorization_template"],
        client=client,
    )
    assert report["status"] == "conflict"
    assert client.calls.count("create") == 1
    assert not item.ledger.exists()


def test_different_numeric_repository_id_has_a_distinct_dispatch_identity(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    original = dispatch.preview(item.documents, item.state_path, item.request)
    chosen = copy.deepcopy(item.policy)
    chosen["repository"]["id"] += 1
    changed = dispatch.preview(
        item.documents, item.state_path, dataclasses.replace(item.request, dispatch_policy=chosen)
    )
    assert changed["dispatch_id"] != original["dispatch_id"]
    assert item.client.calls == []


def test_duplicate_remote_matches_conflict_without_creating(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    effect = dispatch.preview(item.documents, item.state_path, item.request)["effect"]
    item.client.create(item.policy["repository"], effect)
    item.client.create(item.policy["repository"], effect)
    item.client.calls.clear()
    report = item.apply()
    assert report["status"] == "conflict"
    assert "create" not in item.client.calls and not item.ledger.exists()


def test_discovered_existing_task_is_registered_without_post(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    effect = dispatch.preview(item.documents, item.state_path, item.request)["effect"]
    item.client.create(item.policy["repository"], effect)
    item.client.calls.clear()
    report = item.apply()
    assert report["status"] == "registered", report
    assert "create" not in item.client.calls


def test_existing_pending_work_blocks_task_creation(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    state = item.state
    state["pending"] = [
        {"record": "REQ-1", "dimension": "verification", "task": "other-task", "opened_at": "2026-10-30T09:00:00Z"}
    ]
    item.state_path.write_bytes(state_writer.serialize(state))
    with pytest.raises(ValueError, match="pending"):
        item.apply()
    assert "create" not in item.client.calls


def test_explicit_supersession_preserves_history_and_never_recreates(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    first = item.apply()
    before = item.state_path.read_bytes(), item.ledger.read_bytes()
    with pytest.raises(ValueError, match="authorization"):
        dispatch.supersede(item.journal, first["dispatch_id"], reason="replanned")
    report = dispatch.supersede(item.journal, first["dispatch_id"], reason="replanned", authorize=True)
    assert report["status"] == "superseded"
    assert item.apply()["status"] == "superseded"
    assert (item.state_path.read_bytes(), item.ledger.read_bytes()) == before
    assert item.client.calls.count("create") == 1
    shown = dispatch.show(item.journal, first["dispatch_id"])
    assert shown["record"]["events"][-1]["data"]["reason"] == "replanned"


def test_changed_current_contract_blocks_before_journal_or_remote_work(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    approval = item.authorization
    documents = copy.deepcopy(item.documents)
    documents[item.source.model_file]["records"][0]["statement"] = "changed approved contract"
    before = item.journal.read_bytes()
    with pytest.raises(ValueError):
        dispatch.apply(
            item.journal,
            item.ledger,
            documents,
            item.state_path,
            item.request,
            authorization=approval,
            client=item.client,
        )
    assert item.journal.read_bytes() == before and item.client.calls == []


def test_journal_and_ledger_overlap_is_rejected(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    with pytest.raises(ValueError, match="storage"):
        dispatch.apply(
            item.journal,
            item.journal,
            item.documents,
            item.state_path,
            item.request,
            authorization=item.authorization,
            client=item.client,
        )
    assert item.client.calls == []


def test_reports_hash_complete_public_facts(dispatch_scenario: Scenario) -> None:
    report = dispatch_scenario.apply()
    digest = report.pop("digest")
    assert digest == planning.digest(report)
    assert "registered" in dispatch.as_text(report)


def test_uninitialized_journal_is_not_implicitly_created(dispatch_scenario: Scenario, tmp_path: Path) -> None:
    item = dispatch_scenario
    missing = item.journal.parent / "missing.sqlite3"
    with pytest.raises(ValueError):
        dispatch.apply(
            missing,
            item.ledger,
            item.documents,
            item.state_path,
            item.request,
            authorization=item.authorization,
            client=item.client,
        )
    assert not missing.exists() and item.client.calls == []


def test_show_missing_identity_is_read_only(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    before = item.journal.read_bytes()
    report = dispatch.show(item.journal, "sha256:" + "a" * 64)
    assert report["status"] == "missing" and item.journal.read_bytes() == before


def test_intent_is_committed_before_remote_create(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario

    class InspectingCreate(FakeIssues):
        def create(self, repository: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
            # Independent read-only SQLite inspection does not acquire the coordinator's lock.
            import sqlite3

            with sqlite3.connect(item.journal) as connection:
                rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                assert rows
            return super().create(repository, effect)

    assert item.apply(client=InspectingCreate())["status"] == "registered"


@pytest.mark.parametrize("case", ["control_title", "oversized_utf8_body"])
def test_invalid_effect_is_rejected_before_preview_or_journal_write(dispatch_scenario: Scenario, case: str) -> None:
    item = dispatch_scenario
    chosen = copy.deepcopy(item.policy)
    if case == "control_title":
        chosen["issue"]["title"] = "hidden\x1b[2Jtitle"
    else:
        chosen["issue"]["body"] = "😀" * 20000
    request = dataclasses.replace(item.request, dispatch_policy=chosen)
    before = item.journal.read_bytes()
    with pytest.raises(ValueError, match="issue effect"):
        dispatch.preview(item.documents, item.state_path, request)
    assert item.journal.read_bytes() == before and item.client.calls == []


def test_preview_renders_selected_goal_and_current_preconditions(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    report = dispatch.preview(item.documents, item.state_path, item.request)
    assert report["action"]["record"] == "REQ-1"
    assert report["target"] == item.request.target.data
    assert report["storage"] == item.policy["storage"]
    assert report["preconditions"]["state"]["serial"] == item.state["serial"]
    assert report["preconditions"]["state"]["lineage"] == item.state["lineage"]
    assert report["authorization_template"]["storage"] == item.policy["storage"]
    rendered = dispatch.as_text(report)
    assert "REQ-1" in rendered and item.policy["issue"]["title"] in rendered
    assert str(item.state_path) in rendered and item.request.target.data["revision"] in rendered


def test_authorization_cannot_be_replayed_against_a_fresh_journal(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    authorization = item.authorization
    other = item.journal.parent / "other.sqlite3"
    dispatch_store.initialize(other)
    with pytest.raises(ValueError, match="reviewed dispatch storage"):
        dispatch.apply(
            other,
            item.ledger,
            item.documents,
            item.state_path,
            item.request,
            authorization=authorization,
            client=item.client,
        )
    assert item.client.calls == []


def test_reinitialized_journal_at_same_path_cannot_erase_create_barrier(dispatch_scenario: Scenario) -> None:
    item = dispatch_scenario
    authorization = item.authorization
    item.journal.unlink()
    replacement = dispatch_store.initialize(item.journal)
    assert replacement["journal_id"] != item.policy["storage"]["journal_id"]
    with pytest.raises(ValueError, match="journal instance"):
        item.apply(authorization=authorization)
    assert item.client.calls == []


@pytest.mark.parametrize(
    "field", ["dispatch_id", "binding_id", "pending_key", "remote", "state", "claims", "registered_at"]
)
def test_receipt_semantics_are_checked_beyond_its_hash(dispatch_scenario: Scenario, field: str) -> None:
    item = dispatch_scenario
    identity = item.apply()["dispatch_id"]
    record = dispatch.show(item.journal, identity)["record"]
    registered = dispatch._event(record, "registered")
    assert registered is not None
    receipt = registered["receipt"]
    if field in {"dispatch_id", "binding_id", "pending_key"}:
        receipt[field] = "sha256:" + "f" * 64
    elif field == "remote":
        receipt["remote"]["id"] += 1
    elif field == "state":
        receipt["state"]["digest"] = "sha256:" + "f" * 64
    elif field == "claims":
        receipt["claims"]["verification_established"] = True
    else:
        receipt["registered_at"] = "2099-01-01T00:00:00+00:00"
    receipt["digest"] = planning.digest({key: value for key, value in receipt.items() if key != "digest"})
    with pytest.raises(ValueError):
        dispatch._validate_history(record, item.request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("record", "REQ-other"),
        ("dimension", "delivery"),
        ("task", "https://github.com/acme/demo/issues/999"),
        ("opened_at", "2026-10-30T08:59:59+00:00"),
    ],
)
def test_prepared_pending_entry_must_be_the_exact_approved_task(
    dispatch_scenario: Scenario, field: str, value: str
) -> None:
    item = dispatch_scenario
    identity = item.apply()["dispatch_id"]
    record = dispatch.show(item.journal, identity)["record"]
    preparation = dispatch._event(record, "registration_prepared")
    assert preparation is not None
    preparation["pending"][field] = value
    after = state_writer.decode(base64.b64decode(preparation["after"]))
    after["pending"][-1] = copy.deepcopy(preparation["pending"])
    preparation["after"] = base64.b64encode(state_writer.serialize(after)).decode()
    confirmed = dispatch._event(record, "remote_confirmed")
    assert confirmed is not None
    remote = confirmed["receipt"]
    with pytest.raises(ValueError, match="pending"):
        dispatch._prepared(preparation, {**record["intent"], "remote": remote}, item.request)


def test_satisfied_goal_cannot_abandon_a_published_registration(
    dispatch_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exacly.refresh import refresh
    from test_executions import run

    item = dispatch_scenario
    publish = state_writer.publish_locked

    def interrupted(*args: Any, **kwargs: Any) -> None:
        publish(*args, **kwargs)
        raise RuntimeError("process stopped after publication")

    monkeypatch.setattr(state_writer, "publish_locked", interrupted)
    with pytest.raises(RuntimeError, match="stopped"):
        item.apply()
    monkeypatch.setattr(state_writer, "publish_locked", publish)
    identity = item.authorization["dispatch_id"]
    with pytest.raises(ValueError, match="recovery before superseding"):
        dispatch.supersede(item.journal, identity, reason="cancel", authorize=True)
    observed = dataclasses.replace(
        run(item.source, item.request.date),
        revision=item.request.target.data["revision"],
        observed_at=dt.datetime(2026, 10, 30, 10, 1, tzinfo=dt.UTC),
    )
    current = item.state
    after = refresh(item.documents, {item.source.case: "passed"}, observed, previous=current)
    state_writer.publish(state_writer.read(item.state_path), after)
    assert dispatch.preview(item.documents, item.state_path, item.request)["status"] == "superseded"
    report = item.apply()
    assert report["status"] == "registered", report
    assert item.state["pending"] == current["pending"]
    assert item.state["records"] == after["records"]
    assert executions.get(item.ledger, report["receipt"]["binding_id"])["active"] is True
    assert item.client.calls.count("create") == 1


def test_already_satisfied_goal_is_retained_as_superseded_without_creating_work(dispatch_scenario: Scenario) -> None:
    from exacly.refresh import refresh
    from test_executions import run

    item = dispatch_scenario
    observed = dataclasses.replace(run(item.source, item.request.date), revision=item.request.target.data["revision"])
    state = refresh(item.documents, {item.source.case: "passed"}, observed, previous=item.state)
    state_writer.publish(state_writer.read(item.state_path), state)
    before = item.state_path.read_bytes()
    report = item.apply()
    assert report["status"] == "superseded"
    assert not item.ledger.exists() and item.client.calls == []
    assert item.state_path.read_bytes() == before
    assert dispatch.show(item.journal, report["dispatch_id"])["record"]["phase"] == "superseded"
