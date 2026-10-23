"""Runs every case in 0.1/cases.yaml and 0.2/cases.yaml through the validator and compares the report.

Run with `mise run test`, or from the repository root: uv run python -m unittest discover -s validator/tests
"""

import contextlib
import copy
import datetime as dt
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

import jsonpatch
import yaml
from exacly_validate import validate, validate_documents
from exacly_validate.cli import main

TESTS = Path(__file__).resolve().parent
EXAMPLES = TESTS.parent.parent / "spec" / "examples"
EXAMPLE_0_1 = EXAMPLES / "0.1" / "invoice-reminders.yaml"
EXAMPLE_0_2 = EXAMPLES / "0.2" / "invoice-reminders"


def load_suite(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The suite and its bases, each patched. A 0.2 base is a set of named documents."""
    suite = yaml.safe_load(path.read_text())
    bases = {}
    for name, base in suite["bases"].items():
        if "documents" in base:
            doc = {key: yaml.safe_load((path.parent / src).read_text()) for key, src in base["documents"].items()}
        else:
            doc = yaml.safe_load((path.parent / base["from"]).read_text())
        bases[name] = jsonpatch.apply_patch(doc, base["patch"])
    return suite, bases


def finding_key(f: dict[str, Any]) -> tuple[str, str, str]:
    return (f["severity"], f["rule"], str(f["record"]))


class ValidatorTest(unittest.TestCase):
    suites: ClassVar[dict[str, tuple[dict[str, Any], dict[str, Any]]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.suites = {v: load_suite(TESTS / v / "cases.yaml") for v in ("0.1", "0.2")}

    def check_report(self, case: dict[str, Any], report: dict[str, Any]) -> None:
        reported = report["errors"] + report["warnings"]
        found = sorted(reported, key=lambda f: (*finding_key(f), str(f["document"])))
        expected = sorted(case["findings"], key=lambda f: (*finding_key(f), str(f.get("document"))))
        messages = "\n".join(f"{f['document']}: {f['message']}" for f in reported)
        self.assertEqual([finding_key(f) for f in found], [finding_key(f) for f in expected], messages)
        for f, e in zip(found, expected, strict=True):
            if "document" in e:
                self.assertEqual(f["document"], e["document"], f["message"])
        waived = sorted((f["rule"], f["record"]) for f in report["exceptions_applied"])
        self.assertEqual(waived, sorted((w["rule"], w["record"]) for w in case.get("waived", [])))
        self.assertEqual(report["result"], case["result"])

    def test_cases_0_1(self) -> None:
        suite, bases = self.suites["0.1"]
        date = dt.date.fromisoformat(suite["date"])
        for case in suite["cases"]:
            with self.subTest(case["description"]):
                doc = jsonpatch.apply_patch(bases[case["base"]], case["patch"])
                before = copy.deepcopy(doc)
                report = validate(doc, date).as_dict()
                self.assertEqual(doc, before, "the validator must not change its input")
                self.check_report(case, report)

    def test_cases_0_2(self) -> None:
        suite, bases = self.suites["0.2"]
        date = dt.date.fromisoformat(suite["date"])
        for case in suite["cases"]:
            with self.subTest(case["description"]):
                docs = jsonpatch.apply_patch(bases[case["base"]], case["patch"])
                before = copy.deepcopy(docs)
                report = validate_documents(docs, date, case.get("revision", suite["revision"])).as_dict()
                self.assertEqual(docs, before, "the validator must not change its input")
                self.check_report(case, report)

    def test_reports_render_for_malformed_metadata(self) -> None:
        _, bases = self.suites["0.1"]
        for model in (None, "invoicing", {"namespace": "invoicing", "scope": "everything"}):
            with self.subTest(model=model):
                doc = copy.deepcopy(bases["active"])
                doc["model"] = model
                report = validate(doc, dt.date(2026, 10, 23))
                self.assertEqual(report.as_dict()["result"], "does not conform")
                self.assertIn("Result: does not conform", report.as_text())

    def test_one_0_2_config_document_validates_on_its_own(self) -> None:
        doc = yaml.safe_load((EXAMPLE_0_2 / "model.yaml").read_text())
        doc.update(yaml.safe_load((EXAMPLE_0_2 / "billing.yaml").read_text()))
        self.assertEqual(validate(doc, dt.date(2026, 10, 23)).result, "conforms")

    def test_report_names_documents_and_state(self) -> None:
        _, bases = self.suites["0.2"]
        report = validate_documents(bases["active"], dt.date(2026, 10, 23), "9f2c41e")
        d = report.as_dict()
        self.assertEqual(d["revision"], "9f2c41e")
        self.assertEqual(d["documents"], ["billing", "model", "production"])
        self.assertEqual(
            d["states"],
            [{"document": "production", "context": "production", "serial": 3, "config_revision": "9f2c41e"}],
        )
        self.assertIn("State: production, serial 3, config 9f2c41e (production)", report.as_text())

    def test_cli_exit_codes(self) -> None:
        doc = yaml.safe_load(EXAMPLE_0_1.read_text())
        doc["records"][5]["lifecycle"] = "active"
        doc["relationships"] = [r for r in doc["relationships"] if r["type"] not in {"uses", "implements"}]
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            broken = Path(tmp) / "broken.yaml"
            broken.write_text(yaml.safe_dump(doc))
            self.assertEqual(main([str(EXAMPLE_0_1), "--date", "2026-10-23"]), 0)
            self.assertEqual(main([str(broken), "--date", "2026-10-23", "--json"]), 1)

    def test_cli_reads_a_config_folder_with_state(self) -> None:
        _, bases = self.suites["0.2"]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "exacly"
            shutil.copytree(EXAMPLE_0_2, folder)
            (folder / "README.md").write_text("Not a document the validator reads.\n")
            (folder / "state").mkdir()
            state = folder / "state" / "production.json"
            state.write_text(json.dumps(bases["active"]["production"]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main([str(folder), "--date", "2026-10-23", "--revision", "9f2c41e", "--json"]), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["states"][0]["document"], str(state))
            state.write_text(json.dumps({**bases["active"]["production"], "records": {"REQ-404": {}}}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(folder), "--date", "2026-10-23", "--revision", "9f2c41e"]), 1)
                self.assertEqual(main([str(folder / "model.yaml"), str(folder / "billing.yaml")]), 0)


if __name__ == "__main__":
    unittest.main()
