"""Read-only readiness decisions use evidence for the selected implementation."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from exacly import query
from exacly.cli import exacly_main
from test_applicability import WHEN, B, documents, target

POLICY: dict[str, Any] = {
    "name": "main-readiness",
    "version": "1",
    "requirements": [{"record": "REQ-1", "dimensions": ["verification", "delivery"]}],
}


def evaluate(docs: dict[str, Any], policy: Any = None, **selection: Any) -> dict[str, Any]:
    from exacly import policy as gates

    return gates.evaluate(query.load(docs, WHEN), POLICY if policy is None else policy, target(**selection))


def test_satisfied_gate_identifies_policy_target_date_and_support() -> None:
    docs = documents()
    before = copy.deepcopy(docs)
    result = evaluate(docs)
    assert result["result"] == "satisfied"
    assert result["policy"] == {"name": "main-readiness", "version": "1"}
    assert result["target"] == target().data
    assert result["as_of"] == "2026-10-26"
    assert all(check["evidence"] for check in result["checks"])
    assert docs == before
    assert evaluate(docs) == result


def test_missing_delivery_is_unknown_and_does_not_invalidate_verification() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"] = docs["state"]["records"]["REQ-1"]["assessments"][:1]
    result = evaluate(docs)
    assert result["result"] == "unknown"
    assert {c["dimension"]: c["result"] for c in result["checks"]} == {
        "delivery": "unknown",
        "verification": "satisfied",
    }


def test_current_test_failure_blocks_gate() -> None:
    docs = documents()
    a = docs["state"]["records"]["REQ-1"]["assessments"][0]
    a.update(state="failed", reason="Invoice total assertion failed")
    result = evaluate(docs)
    assert result["result"] == "not_satisfied"
    assert any("Invoice total assertion failed" in c["reason"] for c in result["checks"])


def test_negative_result_dominates_missing_delivery() -> None:
    docs = documents()
    a = docs["state"]["records"]["REQ-1"]["assessments"][0]
    a.update(state="failed", reason="Failure")
    docs["state"]["records"]["REQ-1"]["assessments"] = [a]
    assert evaluate(docs)["result"] == "not_satisfied"


def test_nonconforming_model_cannot_pass_a_gate() -> None:
    docs = documents()
    docs["config"]["relationships"] = [r for r in docs["config"]["relationships"] if r["type"] != "implements"]
    result = evaluate(docs)
    assert result["result"] == "unknown"
    assert result["findings"]["errors"]


def test_unrelated_revision_never_satisfies_policy() -> None:
    assert evaluate(documents(), revision=B)["result"] == "unknown"


def test_expired_positive_support_is_unknown() -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][0]["evidence"][0]["expires_at"] = "2026-10-25"
    result = evaluate(docs)
    assert result["result"] == "unknown"
    assert any(c["excluded"] for c in result["checks"])


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {**POLICY, "version": ""},
        {**POLICY, "requirements": []},
        {**POLICY, "typo": True},
        {**POLICY, "requirements": [{"record": "REQ-1", "dimensions": ["operation"]}]},
        {**POLICY, "requirements": [POLICY["requirements"][0], POLICY["requirements"][0]]},
        {**POLICY, "requirements": [{"record": "REQ-1", "dimensions": ["verification", "verification"]}]},
        {**POLICY, "requirements": [{"record": "REQ-1", "dimensions": []}]},
        {**POLICY, "requirements": [{"record": "APP-1", "dimensions": ["verification"]}]},
        {**POLICY, "requirements": [{"record": "UNKNOWN", "dimensions": ["verification"]}]},
    ],
)
def test_invalid_policy_does_not_become_a_readiness_decision(bad: Any) -> None:
    with pytest.raises(ValueError):
        evaluate(documents(), bad)


@pytest.mark.parametrize(("state", "exit_code"), [("passed", 0), ("failed", 1), ("not_checked", 1)])
def test_gate_command_exit_codes_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
    exit_code: int,
) -> None:
    docs = documents()
    a = docs["state"]["records"]["REQ-1"]["assessments"][0]
    a["state"] = state
    if state != "passed":
        a["reason"] = "Test run result"
    folder = tmp_path / "model"
    folder.mkdir()
    for name, doc in docs.items():
        (folder / f"{name}.json").write_text(json.dumps(doc))
    policy_file, selection = tmp_path / "policy.json", tmp_path / "target.json"
    policy_file.write_text(json.dumps(POLICY))
    selection.write_text(json.dumps(target().data))
    args = ["gate", "-m", str(folder), "--policy", str(policy_file), "--target", str(selection), "--date", str(WHEN)]
    assert exacly_main([*args, "--json"]) == exit_code
    assert (
        json.loads(capsys.readouterr().out)["result"]
        == {
            "passed": "satisfied",
            "failed": "not_satisfied",
            "not_checked": "unknown",
        }[state]
    )
    before = policy_file.read_text()
    assert exacly_main(args) == exit_code
    assert "main-readiness" in capsys.readouterr().out
    assert policy_file.read_text() == before
    policy_file.write_text("{}")
    assert exacly_main(args) == 2
    assert "invalid policy" in capsys.readouterr().err


def test_expired_failure_does_not_assert_a_current_failure() -> None:
    docs = documents()
    a = docs["state"]["records"]["REQ-1"]["assessments"][0]
    a.update(state="failed", reason="Historical failure")
    a["evidence"][0]["expires_at"] = "2026-10-25"
    assert evaluate(docs)["result"] == "unknown"


def test_overlapping_checks_through_qualified_record_are_rejected() -> None:
    chosen = copy.deepcopy(POLICY)
    chosen["requirements"].append({"record": "demo:REQ-1", "dimensions": ["verification"]})
    with pytest.raises(ValueError, match="duplicate check"):
        evaluate(documents(), chosen)


def test_known_incomplete_delivery_is_not_satisfied() -> None:
    docs = documents()
    delivery = docs["state"]["records"]["REQ-1"]["assessments"][1]
    delivery["state"] = "in_progress"
    assert evaluate(docs)["result"] == "not_satisfied"


@pytest.mark.parametrize(("scope", "value"), [("environment", "staging"), ("platform", "ios"), ("release", "1.0")])
def test_delivery_scope_cannot_satisfy_a_different_or_unspecified_target(scope: str, value: str) -> None:
    docs = documents()
    docs["state"]["records"]["REQ-1"]["assessments"][1]["subject"][scope] = value
    delivery_only = {**POLICY, "requirements": [{"record": "REQ-1", "dimensions": ["delivery"]}]}
    assert evaluate(docs, delivery_only)["result"] == "unknown"
    assert evaluate(docs, delivery_only, **{scope: value})["result"] == "satisfied"


def test_public_policy_preflight_checks_references_without_observation_evaluation() -> None:
    from exacly import policy as gates

    docs = documents()
    docs.pop("state")
    model = query.load(docs, WHEN)
    validated = gates.validate_policy(model, POLICY)
    assert validated == POLICY
    validated["requirements"][0]["dimensions"].clear()
    assert POLICY["requirements"][0]["dimensions"] == ["verification", "delivery"]
    invalid = {**POLICY, "requirements": [{"record": "REQ-MISSING", "dimensions": ["verification"]}]}
    with pytest.raises(ValueError, match="invalid policy"):
        gates.validate_policy(model, invalid)
