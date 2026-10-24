"""Checks the 0.2 config and state schemas: each iteration is a valid JSON Schema
with an $id matching its path, every example validates, and every case in
0.2/cases.yaml gives the stated result and reports the stated errors.

Run with `mise run test`, or from the repository root: uv run pytest spec/tests
"""

import json
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import jsonpatch
import jsonschema
import pytest
import yaml

TESTS = Path(__file__).resolve().parent
SPEC = TESTS.parent
ROOT = SPEC.parent
SCHEMA_DIR = SPEC / "schema" / "0.2"
KINDS = ("config", "state")
EXAMPLES = SPEC / "examples" / "0.2"
CASES = TESTS / "0.2" / "cases.yaml"


def latest_schema_path(kind: str) -> Path:
    """The latest dated iteration is the correct one."""
    return sorted((SCHEMA_DIR / kind).glob("*.json"))[-1]


def validator(kind: str) -> jsonschema.Draft202012Validator:
    schema = json.loads(latest_schema_path(kind).read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def load(path: Path) -> Any:
    return json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())


def pointer(path: Iterable[str | int]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def matches(error: jsonschema.ValidationError, expected: dict[str, Any]) -> bool:
    if pointer(error.absolute_path) != expected["at"] or error.validator != expected["keyword"]:
        return False
    prop = expected.get("property")
    return prop is None or f"'{prop}'" in error.message


class SchemaTest(unittest.TestCase):
    def test_schemas_are_valid_json_schema(self) -> None:
        for kind in KINDS:
            for path in sorted((SCHEMA_DIR / kind).glob("*.json")):
                with self.subTest(schema=f"{kind}/{path.name}"):
                    jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))

    def test_schema_ids_match_their_paths(self) -> None:
        for kind in KINDS:
            for path in sorted((SCHEMA_DIR / kind).glob("*.json")):
                with self.subTest(schema=f"{kind}/{path.name}"):
                    schema_id = json.loads(path.read_text())["$id"]
                    self.assertTrue(schema_id.endswith(f"/schema/0.2/{kind}/{path.stem}"), schema_id)

    def test_examples_validate(self) -> None:
        configs = sorted(EXAMPLES.glob("*/*.yaml"))
        states = sorted(EXAMPLES.glob("*/state/*.json"))
        self.assertTrue(configs)
        for kind, paths in (("config", configs), ("state", states)):
            check = validator(kind)
            for path in paths:
                with self.subTest(example=str(path.relative_to(EXAMPLES))):
                    self.assertEqual([e.message for e in check.iter_errors(load(path))], [])


def load_cases() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(suite, case) for suite in yaml.safe_load(CASES.read_text())["suites"] for case in suite["cases"]]


CASE_LIST = load_cases()


@pytest.mark.parametrize(("suite", "case"), CASE_LIST, ids=[f"{s['schema']}: {c['description']}" for s, c in CASE_LIST])
def test_case(suite: dict[str, Any], case: dict[str, Any], record_property: Callable[[str, object], None]) -> None:
    # Bindings in spec/exacly select test cases by this ID.
    record_property("exacly-case", f"{CASES.relative_to(ROOT)}::{case['description']}")
    doc = jsonpatch.apply_patch(load(CASES.parent / suite["base"]), case["patch"])
    errors = list(validator(suite["schema"]).iter_errors(doc))
    found = [f"{pointer(e.absolute_path) or '/'} {e.validator}: {e.message}" for e in errors]
    if case["valid"]:
        assert found == []
        return
    assert case.get("errors"), "an invalid case must list its expected errors"
    for expected in case["errors"]:
        assert any(matches(e, expected) for e in errors), f"expected {expected}, got {found}"
