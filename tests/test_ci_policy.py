"""The repository's readiness policy covers every active requirement, without requiring delivery."""

from pathlib import Path

import yaml
from exacly.cli import load

ROOT = Path(__file__).resolve().parents[1]


def test_ci_policy_requires_verification_for_every_active_requirement() -> None:
    policy = yaml.safe_load((ROOT / "ci" / "verification.yaml").read_text())
    documents = load([ROOT / "spec/exacly/*.yaml"])
    active = {
        r["id"]
        for doc in documents.values()
        for r in doc.get("records", [])
        if r["type"] == "Requirement" and r["lifecycle"] == "active"
    }
    assert {r["record"] for r in policy["requirements"]} == active
    assert all(r["dimensions"] == ["verification"] for r in policy["requirements"])


def test_release_workflow_can_supply_the_same_bound_test_results() -> None:
    import datetime as dt

    from exacly import policy, query
    from exacly.applicability import Target
    from exacly.core import bindings
    from exacly.refresh import Run, refresh

    documents = load([ROOT / "spec/exacly/*.yaml"])
    cases = {
        pattern.replace("*", "case"): "passed"
        for doc in documents.values()
        for record in doc.get("records", [])
        for binding in bindings(record, "tests")
        for pattern in binding.get("cases", [])
    }
    revision = "a" * 40
    observed = refresh(
        documents,
        cases,
        Run(
            revision=revision,
            context="main",
            observed_at=dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
            workflow=".github/workflows/release.yml",
            job="build",
            job_status="success",
        ),
    )
    model = query.load({**documents, "observed": observed}, dt.date(2026, 10, 26))
    selected = Target({"context": "main", "model_revision": revision, "revision": revision})
    readiness = policy.evaluate(model, yaml.safe_load((ROOT / "ci/verification.yaml").read_text()), selected)
    assert readiness["result"] == "satisfied", [(c["record"], c["reason"]) for c in readiness["checks"]]
