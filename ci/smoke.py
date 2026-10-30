"""Exercise the installed CLI, including bundled state schemas, outside the source checkout."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from exacly.cli import exacly_main


def call(args: list[str], expected: int = 0) -> str:
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = exacly_main(args)
    if code != expected:
        raise RuntimeError(f"{args[0]} exited {code}, expected {expected}: {output.getvalue()} {errors.getvalue()}")
    return output.getvalue()


def run(example: Path) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        model = root / "model"
        shutil.copytree(example, model)
        state, junit, target, policy = (
            root / name for name in ("state.json", "junit.xml", "target.json", "policy.json")
        )
        revision = "a" * 40
        target.write_text(json.dumps({"context": "ci", "model_revision": revision, "revision": revision}))
        policy.write_text(
            json.dumps(
                {
                    "name": "wheel-smoke",
                    "version": "1",
                    "requirements": [
                        {"record": "REQ-001", "dimensions": ["verification"]},
                    ],
                }
            )
        )
        question = ["-m", str(model), "-m", str(state)]
        gate = ["gate", *question, "--target", str(target), "--policy", str(policy), "--json"]
        readiness_xml = root / "readiness.xml"

        def gated(expected: int = 0) -> dict[str, Any]:
            native = json.loads(call(gate, expected))
            reported: dict[str, Any] = json.loads(
                call([*gate, "--junit", str(readiness_xml), "--junit-id", "installed-wheel"], expected)
            )
            if native != reported:
                raise RuntimeError("JUnit output changed the native readiness report")
            suites = ET.parse(readiness_xml).getroot()
            cases = list(suites.iter("testcase"))
            failures = list(suites.iter("failure"))
            if len(cases) != 1 or list(suites.iter("skipped")) or len(failures) != expected:
                raise RuntimeError("JUnit did not faithfully report the readiness decision")
            if expected and not failures[0].get("type", "").endswith(str(reported["result"])):
                raise RuntimeError("JUnit lost the native blocking readiness status")
            return reported

        def observe(result: str, observed_revision: str = revision, *, include_skipped: bool = False) -> None:
            skipped_case = (
                '<testcase classname="tests/reminders/test_total" name="skipped"><skipped/></testcase>'
                if include_skipped
                else ""
            )
            junit.write_text(
                '<testsuite><testcase classname="tests/reminders/test_total" name="total">'
                + result
                + "</testcase>"
                + skipped_case
                + "</testsuite>"
            )
            call(
                [
                    "refresh",
                    str(model),
                    "--context",
                    "ci",
                    "--state",
                    str(state),
                    "--junit",
                    str(junit),
                    "--revision",
                    observed_revision,
                    "--workflow",
                    ".github/workflows/ci.yml",
                    "--job",
                    "smoke",
                ]
            )

        observe("")
        call(["validate", str(model), str(state)])
        assessment = json.loads(call(["assess", "REQ-001", *question, "--target", str(target), "--json"]))
        good = gated()
        if assessment["status"] != {"verification": "passed", "delivery": "not_observed"}:
            raise RuntimeError(f"unexpected pre-delivery state: {assessment['status']}")
        if good["result"] != "satisfied":
            raise RuntimeError(f"verification gate did not pass: {good}")
        observe('<failure message="incorrect invoice total"/>')
        failed = gated(1)
        observe("", "b" * 40)
        changed = gated(1)
        observe("<skipped/>")
        skipped = gated(1)
        observe("", include_skipped=True)
        call(["validate", str(model), str(state)])
        mixed = gated(1)
        incomplete = json.loads(call(["assess", "REQ-001", *question, "--target", str(target), "--json"]))
        gaps = json.loads(call(["gaps", *question, "--json"]))
        if incomplete["status"]["verification"] != "inconclusive" or not any(
            r["id"] == "REQ-001" for r in gaps["coverage"]["unverified"]
        ):
            raise RuntimeError("mixed execution was not exposed as incomplete verification")
        summary = {
            **assessment["status"],
            "failed_gate": failed["result"],
            "revision_mismatch": changed["result"],
            "skipped_gate": skipped["result"],
            "mixed_gate": mixed["result"],
        }
        if (failed["result"], changed["result"], skipped["result"]) != ("not_satisfied", "unknown", "unknown"):
            raise RuntimeError(f"unexpected blocking gate outcomes: {summary}")
        if mixed["result"] != "unknown":
            raise RuntimeError(f"mixed execution satisfied the gate: {summary}")
        return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().example), indent=2))
