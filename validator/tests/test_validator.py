"""Runs every case in cases.yaml through the validator and compares the report.

Run from the repository root: python -m unittest discover -s validator/tests
"""

import contextlib
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path

import jsonpatch
import yaml

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

from exacly_validate import main, validate  # noqa: E402

CASES = TESTS / "cases.yaml"
EXAMPLE = TESTS.parent.parent / "spec" / "examples" / "0.1" / "invoice-reminders.yaml"


def finding_key(f: dict) -> tuple:
    return (f["severity"], f["rule"], f["record"])


class ValidatorTest(unittest.TestCase):
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
                report = validate(doc, self.date).as_dict()
                found = sorted(finding_key(f) for f in report["errors"] + report["warnings"])
                expected = sorted(finding_key(f) for f in case["findings"])
                self.assertEqual(found, expected, "\n".join(f["message"] for f in report["errors"] + report["warnings"]))
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
