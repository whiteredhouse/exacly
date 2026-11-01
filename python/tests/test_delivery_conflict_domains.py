"""Different pull requests cannot progress past an uncertain write to their shared base."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from delivery_engine_fixtures import make_case, read
from exacly import delivery_automation as contracts
from test_delivery_contracts import approval, pin


@pytest.mark.parametrize("base_changed", [False, True])
def test_uncertain_merge_blocks_another_pull_request_on_same_base(tmp_path: Path, base_changed: bool) -> None:
    case = make_case(tmp_path, mode="after")
    first = case.apply()
    assert first["status"] == "uncertain"
    selected = copy.deepcopy(case.request)
    selected["operation_key"] = contracts.digest("independently reviewed next pull request")
    selected["subject"]["pull_request"] += 1
    if base_changed:
        selected["subject"]["base_revision"] = "f" * 40
    reviewed = approval(selected)
    for field in ("journal", "limits", "adapter"):
        reviewed[field] = copy.deepcopy(case.approval[field])
    before, remote = case.journal.read_bytes(), read(case.remote)
    with pytest.raises(ValueError, match=r"unresolved.*resource"):
        case.engine.apply(selected, case.payload, approval=reviewed, pin=pin(reviewed))
    assert case.journal.read_bytes() == before
    assert read(case.remote) == remote
