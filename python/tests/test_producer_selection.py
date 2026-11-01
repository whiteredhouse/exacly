"""Exact approved producer association with controlled native-admission source metadata."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from exacly import admission, planning
from exacly import producer_selection as producers
from test_admission import BASE, RUN_ID, Source
from test_applicability import B

Doc = dict[str, Any]


@pytest.fixture
def source() -> Source:
    value = Source()
    value.pages[f"{BASE}/actions/runs/{RUN_ID}"] = value.run
    value.pages[f"{BASE}/actions/workflows/99/runs"] = {"total_count": 1, "workflow_runs": [value.run]}
    return value


def contract(source: Source) -> Doc:
    return {
        "schema_version": "0.1.0",
        "workflow_id": 99,
        "policy": copy.deepcopy(source.policy),
        "policy_digest": planning.digest(source.policy),
    }


def selected(source: Source, approved: Doc | None = None) -> Doc:
    report = producers.select(source, approved or contract(source), B)
    assert report["status"] == "selected", report
    value: Doc = report["selection"]
    return value


def test_discovery_pins_exact_run_attempt_and_definition_without_admission(source: Source) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    assert choice["run_id"] == RUN_ID and choice["attempt"] == 1
    assert choice["revision"] == B and choice["workflow_id"] == 99
    assert choice["repository"] == source.policy["repository"]
    assert choice["contract_digest"] == planning.digest(approved)
    assert choice["policy_digest"] == approved["policy_digest"]
    assert source.downloads == []
    assert choice["digest"] == planning.digest({key: value for key, value in choice.items() if key != "digest"})
    result = producers.poll(source, approved, choice)
    assert result["status"] == "succeeded"
    assert result["selection"] == choice
    assert result["claims"] == {"completion_authorized": False, "evidence_admitted": False, "rerun_authorized": False}
    assert source.downloads == []


def test_failed_producer_is_pinned_and_reported_without_searching_for_green(source: Source) -> None:
    source.run["conclusion"] = source.job["conclusion"] = "failure"
    choice = selected(source)
    source.calls.clear()
    result = producers.wait(source, contract(source), choice)
    assert result["status"] == "failed" and result["selection"] == choice
    assert not any("/workflows/99/runs?" in path for path in source.calls)
    assert source.downloads == []


def test_missing_producer_is_pending_and_multiple_exact_runs_are_ambiguous(source: Source) -> None:
    listing = source.pages[f"{BASE}/actions/workflows/99/runs"]
    listing.update(total_count=0, workflow_runs=[])
    result = producers.select(source, contract(source), B)
    assert result["status"] == "pending" and result["selection"] is None
    other = copy.deepcopy(source.run)
    other.update(id=RUN_ID + 1, conclusion="failure")
    listing.update(total_count=2, workflow_runs=[source.run, other])
    result = producers.select(source, contract(source), B)
    assert result["status"] == "ambiguous" and result["selection"] is None
    # An independently selected exact run is usable; display order and green status do not select it.
    assert producers.select(source, contract(source), B, run_id=RUN_ID, attempt=1)["status"] == "selected"


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "pull_request"),
        ("head_branch", "other"),
        ("head_sha", "a" * 40),
        ("workflow_id", 100),
        ("path", ".github/workflows/other.yml"),
        ("run_attempt", 2),
        ("repository", {"id": 9, "full_name": "acme/demo"}),
        ("head_repository", {"id": 9, "full_name": "other/repo"}),
    ],
)
def test_wrong_explicit_run_contract_fails_closed(source: Source, field: str, value: Any) -> None:
    source.run[field] = value
    result = producers.select(source, contract(source), B, run_id=RUN_ID, attempt=1)
    assert result["status"] == "blocked" and result["selection"] is None


def test_pinned_attempt_drift_blocks_without_falling_back_to_old_success(source: Source) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    current = copy.deepcopy(source.run)
    current.update(run_attempt=2)
    source.pages[f"{BASE}/actions/runs/{RUN_ID}"] = current
    source.calls.clear()
    assert producers.poll(source, approved, choice)["status"] == "blocked"
    assert not any("/workflows/99/runs?" in path for path in source.calls)
    with pytest.raises(ValueError, match="rerun"):
        producers.select(source, approved, B, run_id=RUN_ID, attempt=2)


def test_exact_attempt_endpoint_cannot_differ_from_selected_current_run(source: Source) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    different = copy.deepcopy(source.run)
    different.update(run_attempt=2)
    source.pages[f"{BASE}/actions/runs/{RUN_ID}/attempts/1"] = different
    assert producers.poll(source, approved, choice)["status"] == "blocked"


def test_even_pinned_matrix_definition_is_rejected_by_native_supported_workflow_contract(source: Source) -> None:
    document = yaml.safe_load(source.workflow)
    document["jobs"][source.job_id]["strategy"] = {"matrix": {"python": ["3.12"]}}
    changed = yaml.safe_dump(document).encode()
    entry = next(item for item in source.tree if item["path"] == source.workflow_path)
    entry.update(source.add_blob(source.workflow_path, changed))
    source.policy["workflow"]["digest"] = admission.sha256(changed)
    source.policy["observer"]["digest"] = admission.observer_digest(source.tree, source.observer_paths)
    result = producers.select(source, contract(source), B)
    assert result["status"] == "blocked" and result["selection"] is None


@pytest.mark.parametrize("damage", ["repository", "workflow_id", "workflow_path", "definition", "observer"])
def test_producer_repository_definition_and_verifier_are_freshly_rechecked(source: Source, damage: str) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    if damage == "repository":
        source.pages[BASE]["id"] = 999
    elif damage == "workflow_id":
        source.pages[f"{BASE}/actions/workflows/99"]["id"] = 100
    elif damage == "workflow_path":
        source.pages[f"{BASE}/actions/workflows/99"]["path"] = ".github/workflows/other.yml"
    elif damage == "definition":
        original = next(entry for entry in source.tree if entry["path"] == source.workflow_path)
        replacement = source.add_blob(source.workflow_path, source.workflow + b"\n# changed independently\n")
        original.update(replacement)
    else:
        source.tree.append(source.add_blob("tests/new-check.py", b"# changed verifier closure\n"))
    assert producers.poll(source, approved, choice)["status"] == "blocked"


def test_changed_policy_or_selection_cannot_reuse_retained_identity(source: Source) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    changed = copy.deepcopy(approved)
    changed["policy"]["version"] = "2"
    with pytest.raises(ValueError, match="policy digest"):
        producers.poll(source, changed, choice)
    changed["policy_digest"] = planning.digest(changed["policy"])
    with pytest.raises(ValueError, match="selection"):
        producers.poll(source, changed, choice)
    choice["run_id"] += 1
    with pytest.raises(ValueError, match="selection"):
        producers.poll(source, approved, choice)


@pytest.mark.parametrize("damage", ["multiple", "wrong_name", "wrong_revision", "wrong_attempt", "outcome_conflict"])
def test_terminal_single_job_contract_is_checked_without_importing_state(source: Source, damage: str) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    if damage == "multiple":
        source.pages[f"{BASE}/actions/runs/{RUN_ID}/attempts/1/jobs?page=1"].update(
            total_count=2, jobs=[source.job, source.job]
        )
    elif damage == "wrong_name":
        source.job["name"] = "Another check with a similar display name"
    elif damage == "wrong_revision":
        source.job["head_sha"] = "a" * 40
    elif damage == "wrong_attempt":
        source.job["run_attempt"] = 2
    else:
        source.job["conclusion"] = "failure"
    assert producers.poll(source, approved, choice)["status"] == "blocked"
    assert source.downloads == []


def test_wait_is_bounded_and_keeps_pinned_pending_run(source: Source, monkeypatch: pytest.MonkeyPatch) -> None:
    approved = contract(source)
    choice = selected(source, approved)
    source.run.update(status="in_progress", conclusion=None)
    clock = [0.0]
    monkeypatch.setattr("exacly.producer_selection.time.monotonic", lambda: clock[0])

    def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("exacly.producer_selection.time.sleep", advance)
    result = producers.wait(source, approved, choice, timeout=3, poll_interval=1)
    assert result["status"] == "timeout" and result["selection"] == choice
    assert clock[0] == 3
    assert source.downloads == []


def test_expired_source_read_cannot_release_selection_or_success(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = contract(source)
    clock = [0.0]
    original = source.get_json
    monkeypatch.setattr("exacly.producer_selection.time.monotonic", lambda: clock[0])

    def delayed(path: str) -> Doc:
        clock[0] += 2
        return original(path)

    monkeypatch.setattr(source, "get_json", delayed)
    result = producers.select(source, approved, B, timeout=1)
    assert result["status"] == "timeout" and result["selection"] is None


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 3601])
def test_invalid_or_unbounded_wait_deadline_is_rejected(source: Source, timeout: float) -> None:
    choice = selected(source)
    with pytest.raises(ValueError, match="timeout"):
        producers.wait(source, contract(source), choice, timeout=timeout)
