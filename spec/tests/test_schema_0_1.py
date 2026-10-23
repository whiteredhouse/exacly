"""Checks the 0.1 product-map schema: each iteration is a valid JSON Schema
with an $id matching its path, every example validates, and every case in
0.1/cases.yaml gives the stated result and reports the stated errors.

Run with `mise run test`, or from the repository root: uv run python -m unittest discover -s spec/tests
"""

import json
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

import jsonpatch
import jsonschema
import yaml

TESTS = Path(__file__).resolve().parent
SPEC = TESTS.parent
SCHEMA_DIR = SPEC / "schema" / "0.1"
EXAMPLES = sorted((SPEC / "examples" / "0.1").glob("*.yaml"))
CASES = TESTS / "0.1" / "cases.yaml"


def latest_schema_path() -> Path:
    """The latest dated iteration is the correct one."""
    return sorted(SCHEMA_DIR.glob("*.json"))[-1]


def pointer(path: Iterable[str | int]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def matches(error: jsonschema.ValidationError, expected: dict[str, Any]) -> bool:
    if pointer(error.absolute_path) != expected["at"] or error.validator != expected["keyword"]:
        return False
    prop = expected.get("property")
    return prop is None or f"'{prop}'" in error.message


class SchemaTest(unittest.TestCase):
    validator: ClassVar[jsonschema.Draft202012Validator]

    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = jsonschema.Draft202012Validator(
            json.loads(latest_schema_path().read_text()),
            format_checker=jsonschema.FormatChecker(),
        )

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
                errors = [e.message for e in self.validator.iter_errors(yaml.safe_load(path.read_text()))]
                self.assertEqual(errors, [])

    def test_cases(self) -> None:
        suite = yaml.safe_load(CASES.read_text())
        base = yaml.safe_load((CASES.parent / suite["base"]).read_text())
        for case in suite["cases"]:
            with self.subTest(case["description"]):
                doc = jsonpatch.apply_patch(base, case["patch"])
                errors = list(self.validator.iter_errors(doc))
                found = [f"{pointer(e.absolute_path) or '/'} {e.validator}: {e.message}" for e in errors]
                if case["valid"]:
                    self.assertEqual(found, [])
                    continue
                self.assertTrue(case.get("errors"), "an invalid case must list its expected errors")
                for expected in case["errors"]:
                    self.assertTrue(
                        any(matches(e, expected) for e in errors),
                        f"expected {expected}, got {found}",
                    )


if __name__ == "__main__":
    unittest.main()
