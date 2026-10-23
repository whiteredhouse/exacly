"""Runs every case in cases.yaml through the validator and compares the report.

Run with `mise run test`, or from the repository root: uv run python -m unittest discover -s validator/tests
"""

import contextlib
import copy
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

import jsonpatch
import yaml
from exacly_validate import validate
from exacly_validate.cli import main

TESTS = Path(__file__).resolve().parent
CASES = TESTS / "cases.yaml"
EXAMPLE = TESTS.parent.parent / "spec" / "examples" / "0.1" / "invoice-reminders.yaml"


def finding_key(f: dict[str, Any]) -> tuple[str, str, str | None]:
    return (f["severity"], f["rule"], f["record"])


class ValidatorTest(unittest.TestCase):
    suite: ClassVar[dict[str, Any]]
    date: ClassVar[dt.date]
    bases: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = yaml.safe_load(CASES.read_text())
        cls.date = dt.date.fromisoformat(cls.suite["date"])
        cls.bases = {
            name: jsonpatch.apply_patch(yaml.safe_load((CASES.parent / base["from"]).read_text()), base["patch"])
            for name, base in cls.suite["bases"].items()
        }

    def test_cases(self) -> None:
        for case in self.suite["cases"]:
            with self.subTest(case["description"]):
                doc = jsonpatch.apply_patch(self.bases[case["base"]], case["patch"])
                before = copy.deepcopy(doc)
                report = validate(doc, self.date).as_dict()
                self.assertEqual(doc, before, "the validator must not change its input")
                found = sorted(finding_key(f) for f in report["errors"] + report["warnings"])
                expected = sorted(finding_key(f) for f in case["findings"])
                self.assertEqual(
                    found, expected, "\n".join(f["message"] for f in report["errors"] + report["warnings"])
                )
                waived = sorted((f["rule"], f["record"]) for f in report["exceptions_applied"])
                self.assertEqual(waived, sorted((w["rule"], w["record"]) for w in case.get("waived", [])))
                self.assertEqual(report["result"], case["result"])

    def test_cli_exit_codes(self) -> None:
        doc = yaml.safe_load(EXAMPLE.read_text())
        doc["records"][5]["lifecycle"] = "active"
        doc["relationships"] = [r for r in doc["relationships"] if r["type"] not in {"uses", "implements"}]
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            broken = Path(tmp) / "broken.yaml"
            broken.write_text(yaml.safe_dump(doc))
            self.assertEqual(main([str(EXAMPLE), "--date", "2026-10-23"]), 0)
            self.assertEqual(main([str(broken), "--date", "2026-10-23", "--json"]), 1)


if __name__ == "__main__":
    unittest.main()
