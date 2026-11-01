"""Fixed pilot observer. Worker grants must never permit changes to this file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

SOURCE = "fixtures/unattended-pilot/inputs/source.txt"
RESULT = "fixtures/unattended-pilot/results/source-digests.json"
LIMIT = 64 * 1024


def _read(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.resolve() != path.absolute():
        raise ValueError("pilot observer refuses path aliases")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT:
            raise ValueError("pilot observer requires bounded regular fixture files")
        data = os.read(descriptor, LIMIT + 1)
        after = os.fstat(descriptor)
        current = path.lstat()
        identities = {
            (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            for value in (before, after, current)
        }
        if len(data) > LIMIT or len(identities) != 1 or len(data) != before.st_size:
            raise ValueError("pilot fixture changed during observation")
        return data
    finally:
        os.close(descriptor)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate inventory fields")
        result[key] = value
    return result


def verify_inventory(root: Path) -> None:
    """Verify source-byte inventory only; no candidate phase or revision claim is trusted."""
    source = _read(root, SOURCE)
    result = json.loads(_read(root, RESULT), object_pairs_hook=_object)
    expected = {
        "schema_version": "0.1.0",
        "recipe": "snapshot-inventory-v1",
        "files": [{"path": SOURCE, "size": len(source), "digest": "sha256:" + hashlib.sha256(source).hexdigest()}],
        "verification_established": False,
        "evidence_admitted": False,
    }
    if not isinstance(result, dict) or set(result) != {*expected, "snapshot_digest"}:
        raise ValueError("inventory has an unsupported contract")
    if not isinstance(result["snapshot_digest"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", result["snapshot_digest"]
    ):
        raise ValueError("inventory has no content snapshot identity")
    if any(result[key] != value for key, value in expected.items()):
        raise ValueError("inventory does not match the selected fixture bytes")
    if result["verification_established"] is not False or result["evidence_admitted"] is not False:
        raise ValueError("inventory cannot establish evidence or verification")
    if type(result["files"][0]["size"]) is not int:
        raise ValueError("inventory byte count must be an integer")


def test_pilot_inventory_matches_approved_fixture() -> None:
    verify_inventory(Path(__file__).resolve().parents[1])
