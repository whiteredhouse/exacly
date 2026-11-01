"""Actual opt-in REQ-039 acceptance; unconfigured cases establish no successful verification."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest
from exacly import live_pilot_evidence as live
from exacly.github_evidence import GitHubClient

Doc = dict[str, Any]


@pytest.fixture(scope="session")
def live_acceptance() -> Doc:
    checkout = Path(__file__).resolve().parents[2]
    reference = live.configured_reference(checkout)
    if reference is None:
        pytest.skip(
            "No actual coordinator/producer live acceptance artifact was selected; controlled tests are insufficient."
        )
    token = os.environ.get("EXACLY_LIVE_EVIDENCE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    assert token, "The selected live evidence requires an authenticated read credential."
    source = GitHubClient(token)
    runner = importlib.import_module("ci.req039_pilot")

    def original_human_approval(packet: Doc, receipt: Doc) -> None:
        # Fixed trusted reader re-fetches both REST and original GraphQL edit/author facts.
        runner.verify_approval(runner.ApprovalReader(source), packet, receipt)

    return live.verify_live(
        source, reference, live.current_implementation(checkout), original_human_approval, timeout=300
    )


def test_live_isolated_output_reaches_fresh_native_completion(live_acceptance: Doc) -> None:
    assert live_acceptance["status"] == "verified_live_chain"
    receipt = live_acceptance["evidence"]["native"]["completion"]["receipt"]
    assert receipt["status"] == "completed" and receipt["claims"]["operator_authorized"] is True
    assert live_acceptance["fresh_admission"]["status"] == "admitted"


def test_live_failed_baseline_preserves_the_original_pending_goal(live_acceptance: Doc) -> None:
    observed = live_acceptance["evidence"]["baseline"]["receipt"]["result"]
    assert observed["producer"]["conclusion"] == "failure"
    assert observed["pending_unchanged"] is True
    assert observed["before_state_digest"] == observed["after_state_digest"]
