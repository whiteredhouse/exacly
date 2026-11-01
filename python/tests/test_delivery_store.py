"""Original journal identity, immutable reservations and exact historical result storage."""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any

import pytest
from delivery_engine_fixtures import Case, make_case, read
from exacly import delivery_automation as contracts
from exacly import delivery_runtime, delivery_store
from exacly import execution_store as store
from test_delivery_contracts import NOW, approval, pin, request

Doc = dict[str, Any]


def prepare(case: Case) -> Doc:
    intended = case.engine.preview(case.request, case.payload, approval=case.approval, pin=pin(case.approval))[
        "packet"
    ]["intent"]
    with delivery_store.opened(case.journal, case.journal_id, write=True) as journal:
        return journal.prepare(intended, case.payload, case.approval, pin(case.approval), NOW)


def rewrite(path: Path, table: str, transform: Any) -> None:
    with sqlite3.connect(path) as connection:
        text = connection.execute("SELECT document FROM " + table).fetchone()[0]
        value = dict(json.loads(text))
        transform(value)
        connection.execute(
            "UPDATE " + table + " SET document=?, digest=?", (store.canonical(value), contracts.digest(value))
        )


def test_initialize_is_explicit_private_and_read_does_not_create_or_reset(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        delivery_store.show(path, uuid.uuid4().hex)
    assert list(parent.iterdir()) == []
    selected = request()
    parameters = {
        "objective": selected["objective"],
        "limits": approval(selected)["limits"],
        "native_limits": {"wall_time_ms": 5000, "read_calls": 10, "write_calls": 2},
    }
    initialized = delivery_store.initialize(path, **parameters)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert initialized["status"] == "initialized" and not any(initialized["claims"].values())
    with pytest.raises(FileExistsError):
        delivery_store.initialize(path, **parameters)
    with pytest.raises(ValueError, match="identity"):
        delivery_store.show(path, uuid.uuid4().hex)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == delivery_store.APPLICATION_ID
    assert not delivery_store.show(path, initialized["id"])["operations"]


def test_journal_copies_symlinks_and_hardlinks_do_not_alias_original_budget(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    copied = case.journal.with_name("copy.sqlite3")
    shutil.copy2(case.journal, copied)
    with pytest.raises(ValueError, match="identity"):
        delivery_store.show(copied, case.journal_id)
    copied.unlink()
    copied.symlink_to(case.journal)
    with pytest.raises(OSError):
        delivery_store.show(copied, case.journal_id)
    copied.unlink()
    copied.hardlink_to(case.journal)
    with pytest.raises(ValueError, match="single-link"):
        delivery_store.show(case.journal, case.journal_id)


@pytest.mark.parametrize("field", ["objective", "limits", "native_limits"])
def test_original_scope_anchor_rejects_changed_metadata_even_with_recomputed_document_digest(
    tmp_path: Path, field: str
) -> None:
    case = make_case(tmp_path)

    def alter(value: Doc) -> None:
        if field == "objective":
            value[field]["original_goal"] = contracts.digest("a newly invented original goal")
        else:
            value[field]["wall_time_ms"] += 1

    rewrite(case.journal, "metadata", alter)
    with pytest.raises(ValueError, match="identity"):
        delivery_store.show(case.journal, case.journal_id)


@pytest.mark.parametrize("change", ["sidecar", "schema", "mode", "fence"])
def test_unknown_or_incomplete_history_is_not_repaired(tmp_path: Path, change: str) -> None:
    case = make_case(tmp_path)
    if change == "sidecar":
        Path(str(case.journal) + "-journal").write_bytes(b"retained interrupted writer")
    elif change == "schema":
        with sqlite3.connect(case.journal) as connection:
            connection.execute("CREATE TABLE unexpected (value TEXT)")
    elif change == "mode":
        case.journal.chmod(0o644)
    else:
        with delivery_store.opened(case.journal, case.journal_id) as journal:
            path, _content = delivery_runtime._identity(journal.metadata)
        path.unlink()
    before = case.journal.read_bytes()
    with pytest.raises((ValueError, FileNotFoundError)):
        delivery_store.show(case.journal, case.journal_id)
    assert case.journal.read_bytes() == before


def test_preparation_reserves_original_cost_and_blocks_same_resource_under_a_new_operation_key(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    saved = prepare(case)
    first = delivery_store.show(case.journal, case.journal_id)
    assert first["reserved"] == saved["intent"]["cost"]
    assert prepare(case) == saved
    case.request["operation_key"] = contracts.digest("different logical delivery item same provider resource")
    case.approval.update(operation_key=case.request["operation_key"], request_digest=contracts.digest(case.request))
    with pytest.raises(ValueError, match="unresolved"):
        prepare(case)
    assert delivery_store.show(case.journal, case.journal_id)["reserved"] == first["reserved"]
    assert read(case.remote)["writes"] == read(case.remote)["reads"] == 0


def test_inspection_slots_are_conservatively_charged_and_never_refunded(tmp_path: Path) -> None:
    case = make_case(
        tmp_path, mode="blocked", limits={"operations": 1, "wall_time_ms": 9000, "reads": 7, "rollbacks": 0}
    )
    assert case.apply()["status"] == "prepared"
    assert case.apply()["status"] == "prepared"
    history = delivery_store.show(case.journal, case.journal_id)
    assert history["reserved"] == {"operations": 1, "wall_time_ms": 9000, "reads": 7, "rollbacks": 0}
    before = read(case.remote)
    with pytest.raises(ValueError, match="budget"):
        case.apply()
    assert read(case.remote) == before
    assert delivery_store.show(case.journal, case.journal_id)["reserved"] == history["reserved"]


def test_retained_receipts_and_call_facts_cannot_be_rewritten(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    result = case.apply()
    saved = delivery_store.show(case.journal, case.journal_id, case.request["operation_key"])["operation"]
    last = saved["calls"][-1]
    with delivery_store.opened(case.journal, case.journal_id, write=True) as journal:
        with pytest.raises(ValueError, match="immutable"):
            journal.update_call(case.request["operation_key"], last["id"], lambda item: item.update(cleanup=False))
        changed = copy.deepcopy(last["result"])
        changed["reason"] = "substitute a more convenient historical description"
        with pytest.raises(ValueError, match="immutable"):
            journal.finish_call(case.request["operation_key"], last["id"], changed, last["receipt"])
    assert case.apply()["receipt"] == result["receipt"]


def test_receipt_must_equal_the_actual_retained_result_not_only_its_provenance_digest(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    assert case.apply()["status"] == "applied"

    def alter(value: Doc) -> None:
        value["calls"][-1]["result"]["effect"]["commit"] = "f" * 40

    rewrite(case.journal, "operations", alter)
    with pytest.raises(ValueError, match="differs from"):
        delivery_store.show(case.journal, case.journal_id)


def test_watchdog_cleanup_fact_publication_preserves_an_existing_owner_decision(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    with delivery_store.opened(case.journal, case.journal_id) as journal:
        metadata = copy.deepcopy(journal.metadata)
    call_id = contracts.digest("same durable call key")
    delivery_runtime.retain_cleanup(metadata, call_id, False)
    path = delivery_runtime._cleanup_path(metadata, call_id)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        delivery_runtime.retain_cleanup(metadata, call_id, True)
    assert path.read_bytes() == before
    assert delivery_runtime.read_cleanup(metadata, call_id)["cleaned"] is False  # type: ignore[index]
