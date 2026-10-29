"""The installed-package smoke exercise must cover scoped verification and blocking gates."""

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_smoke_exercises_observations_and_readiness() -> None:
    smoke = runpy.run_path(str(ROOT / "ci" / "smoke.py"))
    assert smoke["run"](ROOT / "spec/examples/0.2/invoice-reminders") == {
        "verification": "passed",
        "delivery": "not_observed",
        "failed_gate": "not_satisfied",
        "revision_mismatch": "unknown",
        "skipped_gate": "unknown",
        "mixed_gate": "unknown",
    }
