"""Checks the 0.1 product-map schema: it is a valid JSON Schema, every example
validates, and each broken variant of the worked example is rejected.

Run from the repository root: python -m unittest discover -s spec/tests
"""

import copy
import json
import unittest
from pathlib import Path

import jsonschema
import yaml

SPEC = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SPEC / "schema" / "0.1"
EXAMPLES = sorted((SPEC / "examples" / "0.1").glob("*.yaml"))
WORKED_EXAMPLE = SPEC / "examples" / "0.1" / "invoice-reminders.yaml"

EVIDENCE = {
    "locator": "reports/q4.csv",
    "kind": "measurement",
    "claim": "Median payment time was 13 days",
    "observed_at": "2026-12-31",
    "applies_to": "2026-Q4",
}


def latest_schema_path() -> Path:
    """The latest dated iteration is the correct one."""
    return sorted(SCHEMA_DIR.glob("*.json"))[-1]


def record(doc: dict, record_id: str) -> dict:
    return next(r for r in doc["records"] if r["id"] == record_id)


def assessment(dimension: str, state: str, evidence: list) -> dict:
    return {
        "subject": "OUT-001",
        "dimension": dimension,
        "state": state,
        "context": "release 1",
        "at": "2026-Q4",
        "evidence": evidence,
    }


def add_assessment(doc: dict, dimension: str, state: str, evidence: list | None = None) -> None:
    doc.setdefault("assessments", []).append(assessment(dimension, state, evidence or []))


def activate_all(doc: dict) -> None:
    for r in doc["records"]:
        r["lifecycle"] = "active"


# Each changes the worked example into a document the schema must reject.
INVALID = {
    "wrong spec version": lambda d: d.update(exacly="0.2.0"),
    "proposed state without base revision": lambda d: d.pop("base_revision"),
    "unknown top-level field": lambda d: d.update(owner="billing-team"),
    "record missing last_reviewed_at": lambda d: record(d, "APP-001").pop("last_reviewed_at"),
    "record with an unknown field": lambda d: record(d, "APP-001").update(foo="bar"),
    "unknown record type": lambda d: record(d, "APP-001").update(type="Service"),
    "unknown relationship type": lambda d: d["relationships"].append(
        {"source": "APP-001", "type": "calls", "target": "INF-001"}
    ),
    "invalid date": lambda d: record(d, "APP-001").update(last_reviewed_at="yesterday"),
    "unknown evidence kind": lambda d: record(d, "APP-001")["evidence"].append(dict(EVIDENCE, kind="ticket")),
    "blank record name": lambda d: record(d, "APP-001").update(name="  "),
    "active requirement without acceptance criteria": lambda d: (
        record(d, "REQ-001").update(lifecycle="active"),
        record(d, "REQ-001").pop("acceptance_criteria"),
    ),
    "active requirement with empty content": lambda d: record(d, "REQ-001").update(
        lifecycle="active", statement="", acceptance_criteria=[""], verification_method=""
    ),
    "active process without actors": lambda d: record(d, "PROC-001").update(lifecycle="active", actors=[]),
    "active information without identifiers": lambda d: record(d, "INFO-001").update(
        lifecycle="active", identifiers=[]
    ),
    "active application without implementation reference": lambda d: (
        record(d, "APP-001").update(lifecycle="active"),
        record(d, "APP-001").pop("implementation_ref"),
    ),
    "unknown baseline without owner or action": lambda d: record(d, "MEAS-001").update(baseline="unknown"),
    "unknown baseline with blank owner and action": lambda d: record(d, "MEAS-001").update(
        baseline="unknown", baseline_owner="", baseline_action=""
    ),
    "assessment state from another dimension": lambda d: add_assessment(d, "verification", "healthy", [EVIDENCE]),
    "healthy operation without evidence": lambda d: add_assessment(d, "operation", "healthy"),
    "target met without evidence": lambda d: add_assessment(d, "outcome_evaluation", "target_met"),
    "passed verification without evidence": lambda d: add_assessment(d, "verification", "passed"),
}

# Each changes the worked example into a document the schema must accept.
VALID = {
    "every record active": activate_all,
    "extension fields": lambda d: (
        d.update({"x-generated-by": "tool"}),
        record(d, "APP-001").update({"x-team": "billing"}),
    ),
    "extension record type": lambda d: record(d, "APP-001").update(type="ops:Runbook"),
    "unknown baseline with owner and action": lambda d: record(d, "MEAS-001").update(
        baseline="unknown", baseline_owner="analytics", baseline_action="Run the cohort query"
    ),
    "unknown operation without evidence": lambda d: add_assessment(d, "operation", "unknown"),
    "not evaluated outcome without evidence": lambda d: add_assessment(d, "outcome_evaluation", "not_evaluated"),
    "not checked verification without evidence": lambda d: add_assessment(d, "verification", "not_checked"),
    "target met with evidence": lambda d: add_assessment(d, "outcome_evaluation", "target_met", [EVIDENCE]),
    "current baseline without base revision": lambda d: (d.update(state="current"), d.pop("base_revision")),
}


class SchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(latest_schema_path().read_text())
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema, format_checker=jsonschema.FormatChecker()
        )
        cls.worked_example = yaml.safe_load(WORKED_EXAMPLE.read_text())

    def errors(self, doc: dict) -> list[str]:
        return [e.message for e in self.validator.iter_errors(doc)]

    def test_schema_is_valid_json_schema(self) -> None:
        for path in sorted(SCHEMA_DIR.glob("*.json")):
            with self.subTest(schema=path.name):
                jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))

    def test_schema_id_matches_its_path(self) -> None:
        for path in sorted(SCHEMA_DIR.glob("*.json")):
            with self.subTest(schema=path.name):
                schema_id = json.loads(path.read_text())["$id"]
                self.assertTrue(schema_id.endswith(f"/schema/0.1/{path.stem}"), schema_id)

    def test_examples_validate(self) -> None:
        self.assertTrue(EXAMPLES)
        for path in EXAMPLES:
            with self.subTest(example=path.name):
                self.assertEqual(self.errors(yaml.safe_load(path.read_text())), [])

    def test_invalid_variants_are_rejected(self) -> None:
        for name, change in INVALID.items():
            with self.subTest(name):
                doc = copy.deepcopy(self.worked_example)
                change(doc)
                self.assertNotEqual(self.errors(doc), [], f"accepted: {name}")

    def test_valid_variants_are_accepted(self) -> None:
        for name, change in VALID.items():
            with self.subTest(name):
                doc = copy.deepcopy(self.worked_example)
                change(doc)
                self.assertEqual(self.errors(doc), [])


if __name__ == "__main__":
    unittest.main()
