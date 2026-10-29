"""Measure offline completion validation against growing, valid retained histories.

Development-only; uses the completion test fixtures and their synthetic aborted history builder.
Run from the repository root with:

    PYTHONPATH=python/src:python/tests python benchmarks/ledger_validation.py --counts 1 10 30

No network requests are made. Fixture creation and restoration are excluded from operation timings.
Elapsed times are diagnostic; the deterministic proof-derivation counts are the regression signal.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from exacly import admission
from test_completion_validation import history, operation


def measure(counts: Sequence[int], repeats: int) -> list[dict[str, Any]]:
    """Return median elapsed time and proof derivations for each public offline path."""
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="exacly-ledger-benchmark-") as directory:
        root = Path(directory)
        for count in counts:
            for name in ("show", "replay", "recover"):
                folder = root / f"{count}-{name}"
                folder.mkdir(mode=0o700)
                case, identity = history(folder, count, prepared=name == "recover")
                original = case.ledger.read_bytes()
                elapsed: list[float] = []
                derivations: list[int] = []
                invoke = operation(case, identity, name)
                for _ in range(repeats):
                    case.ledger.write_bytes(original)
                    with patch.object(admission, "observe_report", wraps=admission.observe_report) as observed:
                        started = time.perf_counter()
                        result = invoke()
                        elapsed.append(time.perf_counter() - started)
                        derivations.append(observed.call_count)
                    assert result["status"] == "completed", result
                results.append(
                    {
                        "retained_intents": count,
                        "operation": name,
                        "median_seconds": round(statistics.median(elapsed), 6),
                        "proof_derivations": derivations,
                        "ledger_bytes": len(original),
                    }
                )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[1, 10, 30])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if min(args.counts) < 1 or args.repeats < 1:
        parser.error("counts and repeats must be positive")
    print(json.dumps({"results": measure(args.counts, args.repeats)}, indent=2))


if __name__ == "__main__":
    main()
