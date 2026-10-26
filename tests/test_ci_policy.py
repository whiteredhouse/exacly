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
