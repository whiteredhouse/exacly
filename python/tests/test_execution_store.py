from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import sqlite3
import stat
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from exacly import execution_store as store

Doc = dict[str, Any]


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def binding(name: str = "first", *, serial: int = 1, state: str = "state1", supersedes: str | None = None) -> Doc:
    return {
        "schema_version": "0.1.0",
        "id": digest(name),
        "pending_key": digest("pending"),
        "contract": {"plan": digest(name), "literal": "'; DROP TABLE bindings; --"},
        "state_snapshot": {"serial": serial, "digest": digest(state), "config_revision": "a" * 40},
        "registered_at": "2026-10-28T10:00:00Z",
        "as_of": "2026-10-28",
        "supersedes": supersedes,
        "claims": {"execution_authorized": False},
    }


@pytest.fixture
def path(tmp_path: Path) -> Path:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    return private / "executions.sqlite3"


def files(directory: Path) -> dict[str, bytes | str]:
    return {
        item.name: os.readlink(item) if item.is_symlink() else item.read_bytes() if item.is_file() else "directory"
        for item in directory.iterdir()
    }


def both_reject(path: Path, value: Doc | None = None) -> None:
    before = files(path.parent)
    with pytest.raises(store.StoreError):
        store.get(path, digest("first"))
    assert files(path.parent) == before
    with pytest.raises(store.StoreError):
        store.put(path, value or binding())
    assert files(path.parent) == before


def test_creation_and_reads_are_private_canonical_and_have_no_sidecars(path: Path) -> None:
    proposed = binding()
    original = copy.deepcopy(proposed)
    assert store.put(path, proposed) == original
    assert proposed == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = files(path.parent)
    assert set(before) == {path.name}
    result = store.get(path, proposed["id"])
    assert result == {
        "binding": original,
        "active": True,
        "superseded_by": None,
        "state_watermark": {"serial": 1, "digest": digest("state1")},
    }
    result["binding"]["contract"]["literal"] = "caller mutation"
    assert store.get(path, proposed["id"])["binding"] == original
    assert files(path.parent) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (store.APPLICATION_ID,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        text = connection.execute("SELECT record_json FROM bindings").fetchone()[0]
    assert text == json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_idempotency_returns_original_and_advances_only_the_watermark(path: Path) -> None:
    original = binding()
    store.put(path, original)
    retry = binding(serial=7, state="state7")
    retry["registered_at"] = "2026-10-29T12:34:56+00:00"
    retry["as_of"] = "2026-10-29"
    assert store.put(path, retry) == original
    assert store.get(path, original["id"]) == {
        "binding": original,
        "active": True,
        "superseded_by": None,
        "state_watermark": {"serial": 7, "digest": digest("state7")},
    }
    assert store.put(path, retry) == original
    before = files(path.parent)
    for invalid in (binding(serial=6, state="state6"), binding(serial=7, state="different")):
        with pytest.raises(store.StoreError):
            store.put(path, invalid)
        assert files(path.parent) == before


@pytest.mark.parametrize("field", ["contract", "pending_key", "supersedes"])
def test_same_identity_cannot_change_its_binding_contract(path: Path, field: str) -> None:
    original = binding()
    store.put(path, original)
    changed = copy.deepcopy(original)
    changed[field] = {"changed": True} if field == "contract" else digest("other")
    before = files(path.parent)
    with pytest.raises(store.StoreError, match="collides"):
        store.put(path, changed)
    assert files(path.parent) == before


def test_explicit_supersession_preserves_original_records_and_watermark(path: Path) -> None:
    first = binding()
    store.put(path, first)
    store.put(path, binding(serial=5, state="state5"))
    second = binding("second", serial=6, state="state6", supersedes=first["id"])
    assert store.put(path, second) == second
    assert store.get(path, first["id"]) == {
        "binding": first,
        "active": False,
        "superseded_by": second["id"],
        "state_watermark": {"serial": 6, "digest": digest("state6")},
    }
    assert store.get(path, second["id"])["active"] is True
    assert store.put(path, second) == second
    third = binding("third", serial=7, state="state7", supersedes=second["id"])
    store.put(path, third)
    assert store.get(path, first["id"])["superseded_by"] == second["id"]
    assert store.get(path, second["id"])["superseded_by"] == third["id"]
    assert store.get(path, first["id"])["state_watermark"]["serial"] == 7
    before = files(path.parent)
    with pytest.raises(store.StoreError, match="superseded"):
        store.put(path, first)
    assert files(path.parent) == before


@pytest.mark.parametrize("case", ["missing_cas", "wrong_cas", "rollback", "changed_digest"])
def test_conflicting_or_stale_supersession_has_no_effect(path: Path, case: str) -> None:
    first = binding(serial=5, state="state5")
    store.put(path, first)
    second = binding("second", serial=6, state="state6", supersedes=first["id"])
    if case == "missing_cas":
        second["supersedes"] = None
    elif case == "wrong_cas":
        second["supersedes"] = digest("unknown")
    elif case == "rollback":
        second["state_snapshot"]["serial"] = 4
    else:
        second["state_snapshot"]["serial"] = 5
    before = files(path.parent)
    with pytest.raises(store.StoreError):
        store.put(path, second)
    assert files(path.parent) == before
    assert store.get(path, first["id"])["active"] is True
    with pytest.raises(store.StoreError, match="does not exist"):
        store.get(path, second["id"])


def test_distinct_pending_keys_have_independent_watermarks(path: Path) -> None:
    first = binding(serial=99, state="state99")
    second = binding("second")
    second["pending_key"] = digest("another pending")
    store.put(path, first)
    store.put(path, second)
    assert store.get(path, second["id"])["state_watermark"]["serial"] == 1


def test_exception_after_partial_sql_writes_rolls_back_every_change(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = binding()
    store.put(path, first)
    original_connect = sqlite3.connect

    class FailedWatermark(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO watermarks"):
                raise sqlite3.OperationalError("injected failure after binding and active writes")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=FailedWatermark)

    before = files(path.parent)
    monkeypatch.setattr(sqlite3, "connect", connect)
    second = binding("second", serial=2, state="state2", supersedes=first["id"])
    with pytest.raises(store.StoreError, match="registration failed"):
        store.put(path, second)
    assert files(path.parent) == before
    assert store.get(path, first["id"])["active"] is True
    with pytest.raises(store.StoreError, match="does not exist"):
        store.get(path, second["id"])
    fresh = path.parent / "new.sqlite3"
    with pytest.raises(store.StoreError):
        store.put(fresh, binding())
    assert not fresh.exists()
    assert files(path.parent) == before


def concurrent_put(path: Path, values: list[Doc]) -> list[Doc | str]:
    barrier = threading.Barrier(len(values))

    def register(value: Doc) -> Doc | str:
        barrier.wait(timeout=10)
        try:
            return store.put(path, value)
        except store.StoreError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        return list(executor.map(register, values))


def test_simultaneous_fresh_idempotent_registrations_return_the_first_record(path: Path) -> None:
    first, second = binding(), binding()
    second["registered_at"] = "2026-10-28T11:00:00Z"
    result = concurrent_put(path, [first, second])
    assert isinstance(result[0], dict) and result[0] == result[1]
    assert result[0] in [first, second]
    assert store.get(path, first["id"])["binding"] == result[0]
    assert set(files(path.parent)) == {path.name}


@pytest.mark.parametrize("supersession", [False, True])
def test_competing_registrations_have_one_winner_and_no_orphan_rows(path: Path, supersession: bool) -> None:
    first = binding()
    if supersession:
        store.put(path, first)
    prior = first["id"] if supersession else None
    candidates = [binding(name, serial=2, state="state2", supersedes=prior) for name in ("second", "third")]
    results = concurrent_put(path, candidates)
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert sum(isinstance(result, str) for result in results) == 1
    assert store.get(path, winners[0]["id"])["active"] is True
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM bindings").fetchone() == (2 if supersession else 1,)
    assert set(files(path.parent)) == {path.name}


def process_put(args: tuple[Path, Doc]) -> Doc:
    return store.put(*args)


def test_registration_serializes_across_processes(path: Path) -> None:
    proposed = binding()
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("fork")) as executor:
        results = list(executor.map(process_put, [(path, proposed), (path, proposed)]))
    assert results == [proposed, proposed]
    assert store.get(path, proposed["id"])["active"] is True


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "text",
        "unrelated_sqlite",
        "newer",
        "wrong_app",
        "extra_table",
        "trigger",
        "wal",
        "corrupt",
        "invalid_record",
        "invalid_active",
        "invalid_watermark",
    ],
)
def test_reads_and_writes_reject_unknown_or_corrupt_stores_without_touching_files(path: Path, case: str) -> None:
    if case == "missing":
        before = files(path.parent)
        with pytest.raises(store.StoreError, match="does not exist"):
            store.get(path, digest("first"))
        assert files(path.parent) == before
        return
    if case in {"empty", "text"}:
        path.write_bytes(b"" if case == "empty" else b"not a sqlite database")
        path.chmod(0o600)
    elif case == "unrelated_sqlite":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
        path.chmod(0o600)
    else:
        store.put(path, binding())
        changes = {
            "newer": "PRAGMA user_version=2",
            "wrong_app": "PRAGMA application_id=123",
            "extra_table": "CREATE TABLE extra (id TEXT)",
            "trigger": "CREATE TRIGGER surprise BEFORE INSERT ON bindings BEGIN SELECT RAISE(ABORT, 'unexpected'); END",
            "wal": "PRAGMA journal_mode=WAL",
            "invalid_record": "UPDATE bindings SET record_json='{}'",
            "invalid_active": "DELETE FROM active",
            "invalid_watermark": "UPDATE watermarks SET digest='changed'",
        }
        if case == "corrupt":
            content = bytearray(path.read_bytes())
            content[100] = 255
            path.write_bytes(content)
        else:
            with sqlite3.connect(path) as connection:
                connection.execute(changes[case])
    both_reject(path)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_existing_auxiliary_paths_are_never_used_or_removed(path: Path, suffix: str, existing: bool, kind: str) -> None:
    if existing:
        store.put(path, binding())
    auxiliary = Path(str(path) + suffix)
    if kind == "file":
        auxiliary.write_bytes(b"unrelated auxiliary contents")
    elif kind == "symlink":
        auxiliary.symlink_to("missing-target")
    else:
        auxiliary.mkdir()
    both_reject(path)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777])
def test_parent_must_already_be_private(path: Path, mode: int) -> None:
    path.parent.chmod(mode)
    both_reject(path)


def test_missing_parent_is_not_created(path: Path) -> None:
    proposed = path.parent / "missing" / "ledger.sqlite3"
    with pytest.raises(store.StoreError):
        store.put(proposed, binding())
    assert not proposed.parent.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "readable", "writable"])
def test_database_must_be_an_owned_private_single_link_regular_file(path: Path, kind: str) -> None:
    if kind in {"symlink", "hardlink"}:
        target = path.parent / "target.sqlite3"
        store.put(target, binding())
        if kind == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        store.put(path, binding())
        path.chmod(0o640 if kind == "readable" else 0o620)
    # Snapshot helper deliberately avoids opening FIFOs.
    before = [(item.name, item.lstat().st_mode, item.lstat().st_ino) for item in path.parent.iterdir()]
    with pytest.raises(store.StoreError):
        store.get(path, digest("first"))
    with pytest.raises(store.StoreError):
        store.put(path, binding())
    assert [(item.name, item.lstat().st_mode, item.lstat().st_ino) for item in path.parent.iterdir()] == before


def test_wrong_current_user_is_rejected(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.put(path, binding())
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    both_reject(path)


def test_parent_symlink_and_nonsticky_writable_ancestor_are_rejected(path: Path) -> None:
    link = path.parent.parent / "link"
    link.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(store.StoreError):
        store.put(link / path.name, binding())
    ancestor = path.parent.parent / "shared"
    ancestor.mkdir()
    ancestor.chmod(0o777)
    private = ancestor / "operator"
    private.mkdir(mode=0o700)
    with pytest.raises(store.StoreError):
        store.put(private / path.name, binding())
    assert not path.exists()


@pytest.mark.parametrize("marker", ["directory", "pointer", "symlink"])
def test_store_is_refused_in_git_working_trees(path: Path, marker: str) -> None:
    git = path.parent.parent / ".git"
    if marker == "directory":
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
    elif marker == "pointer":
        git.write_text("gitdir: /elsewhere/git/worktrees/project\n")
    else:
        actual = path.parent.parent / "actual-git"
        actual.mkdir()
        (actual / "HEAD").write_text("ref: refs/heads/main\n")
        git.symlink_to(actual, target_is_directory=True)
    both_reject(path)


def test_empty_managed_git_guard_directory_is_not_a_working_tree(path: Path) -> None:
    (path.parent.parent / ".git").mkdir()
    assert store.put(path, binding()) == binding()


def test_sql_and_uri_metacharacters_are_values_only(path: Path) -> None:
    unusual = path.parent / "ledger?mode=memory&x=';--.sqlite3"
    proposed = binding()
    assert store.put(unusual, proposed) == proposed
    assert unusual.exists()
    assert store.get(unusual, proposed["id"])["binding"] == proposed
    with pytest.raises(store.StoreError):
        store.get(unusual, "'; DROP TABLE bindings;--")
    assert store.get(unusual, proposed["id"])["active"] is True


@pytest.mark.parametrize(
    "case",
    [
        "extra_field",
        "missing_field",
        "schema",
        "id",
        "pending_key",
        "self_supersedes",
        "negative_serial",
        "bool_serial",
        "overflow_serial",
        "snapshot_digest",
        "config_revision",
        "naive_time",
        "date",
        "nonfinite",
        "bytes",
        "key",
        "surrogate",
    ],
)
def test_invalid_registration_is_rejected_before_any_file_creation(path: Path, case: str) -> None:
    value = binding()
    if case == "extra_field":
        value["unexpected"] = True
    elif case == "missing_field":
        del value["claims"]
    elif case == "schema":
        value["schema_version"] = "9"
    elif case in {"id", "pending_key"}:
        value[case] = "sql';--"
    elif case == "self_supersedes":
        value["supersedes"] = value["id"]
    elif case in {"negative_serial", "bool_serial", "overflow_serial"}:
        value["state_snapshot"]["serial"] = {"negative_serial": -1, "bool_serial": True, "overflow_serial": 2**63}[case]
    elif case == "snapshot_digest":
        value["state_snapshot"]["digest"] = "invalid"
    elif case == "config_revision":
        value["state_snapshot"]["config_revision"] = " "
    elif case == "naive_time":
        value["registered_at"] = "2026-10-28T10:00:00"
    elif case == "date":
        value["as_of"] = "invalid"
    elif case == "nonfinite":
        value["contract"]["extra"] = float("nan")
    elif case == "bytes":
        value["contract"]["extra"] = b"data"
    elif case == "key":
        value["contract"][1] = "non-string key"
    else:
        value["contract"]["extra"] = "\ud800"
    with pytest.raises(store.StoreError):
        store.put(path, value)
    assert files(path.parent) == {}
