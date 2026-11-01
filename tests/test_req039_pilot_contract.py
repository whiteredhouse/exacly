"""The fixed verifier and separate producer are reviewed before any pilot authority is granted."""

from __future__ import annotations

import hashlib
import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from exacly import admission

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = runpy.run_path(str(ROOT / "verification/req039_inventory_test.py"))
SOURCE = str(VERIFIER["SOURCE"])
RESULT = str(VERIFIER["RESULT"])
verify_inventory: Callable[[Path], None] = VERIFIER["verify_inventory"]
PRODUCER: dict[str, Any] = json.loads((ROOT / "ci/req039-producer.json").read_bytes())


def inventory(root: Path) -> tuple[Path, dict[str, Any]]:
    source = root / SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes(b"bounded nonproduction fixture\n")
    result = root / RESULT
    result.parent.mkdir(parents=True)
    value = {
        "schema_version": "0.1.0",
        "recipe": "snapshot-inventory-v1",
        "snapshot_digest": "sha256:" + "1" * 64,
        "files": [
            {
                "path": SOURCE,
                "size": source.stat().st_size,
                "digest": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ],
        "verification_established": False,
        "evidence_admitted": False,
    }
    result.write_text(json.dumps(value))
    return result, value


def test_fixed_observer_verifies_actual_fixture_bytes(tmp_path: Path) -> None:
    inventory(tmp_path)
    verify_inventory(tmp_path)
    (tmp_path / SOURCE).write_bytes(b"changed after inventory\n")
    with pytest.raises(ValueError, match="does not match"):
        verify_inventory(tmp_path)


@pytest.mark.parametrize(
    "case", ["missing", "wrong_digest", "extra_file", "claims_success", "extra_field", "duplicate"]
)
def test_fixed_observer_rejects_incomplete_or_untrusted_output(tmp_path: Path, case: str) -> None:
    result, value = inventory(tmp_path)
    if case == "missing":
        result.unlink()
    elif case == "duplicate":
        result.write_text('{"schema_version":"0.1.0",' + result.read_text()[1:])
    else:
        if case == "wrong_digest":
            value["files"][0]["digest"] = "sha256:" + "f" * 64
        elif case == "extra_file":
            value["files"].append(dict(value["files"][0]))
        elif case == "claims_success":
            value["verification_established"] = True
        else:
            value["unapproved"] = True
        result.write_text(json.dumps(value))
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_inventory(tmp_path)


def test_fixed_observer_refuses_aliased_fixture_output(tmp_path: Path) -> None:
    result, _ = inventory(tmp_path)
    original = result.with_suffix(".retained")
    result.rename(original)
    result.symlink_to(original)
    with pytest.raises(ValueError, match="aliases"):
        verify_inventory(tmp_path)


def test_separate_pilot_producer_preserves_native_single_job_push_contract() -> None:
    contract = admission.validate_producer(PRODUCER)
    raw = (ROOT / contract["workflow"]["path"]).read_bytes()
    admission.workflow_contract(raw, contract)
    document = yaml.safe_load(raw)
    assert list(document["jobs"]) == ["observe"]
    assert document["permissions"] == {"contents": "read"}
    assert document["jobs"]["observe"]["runs-on"] == "ubuntu-24.04"
