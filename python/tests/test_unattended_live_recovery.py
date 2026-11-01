"""Live failure/recovery acceptance requires native retained facts, never simulated success."""

from __future__ import annotations

from typing import Any

import pytest
from exacly import live_pilot_evidence as live
from test_unattended_live_end_to_end import live_acceptance as live_acceptance

Doc = dict[str, Any]


@pytest.mark.parametrize(
    "case",
    [
        "dispatch_restart",
        "publication_response_loss",
        "duplicate_events",
        "divergent_ref",
        "producer_unavailable",
        "wrong_revision",
        "wrong_attempt",
        "criteria_changed",
        "verifier_changed",
        "delegation_revoked",
        "delegation_expired",
        "objective_budget_exhausted",
    ],
)
def test_live_failure_or_recovery_preserves_exact_history_and_authority(live_acceptance: Doc, case: str) -> None:
    live.validate_recovery_case(live_acceptance["evidence"], case)
