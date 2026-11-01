"""Opt-in real GitHub observation; controlled unit tests do not satisfy this acceptance binding."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from exacly import automatic_merge_live


def test_live_github_exact_merge_with_original_authority_and_authenticated_marker() -> None:
    configured = os.environ.get("EXACLY_DELIVERY_MERGE_LIVE_PROOF")
    if configured is None:
        pytest.skip("Approved live merge observation packet is unavailable; controlled cases establish no live claim.")
    selected = os.environ.get("EXACLY_DELIVERY_MERGE_LIVE_PROOF_DIGEST")
    assert selected, "Select the operator-approved packet digest independently of the proof JSON."
    packet = automatic_merge_live.read_packet(Path(configured), packet_digest=selected)
    receipt = automatic_merge_live.observe(packet, packet_digest=selected)
    assert receipt["accepted"] is True, "Fresh GitHub reads did not establish the exact authorized historical merge."
    assert receipt["provider_writes"] == 0
    assert receipt["original_effect_budget_charged"] is False
