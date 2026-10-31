"""Actual competing processes and process death retain objective-wide accounting."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import test_standing_authorization as cases
from exacly import execution_store
from exacly import standing_authorization as authority
from test_standing_authorization import store as store

Doc = dict[str, Any]
SOURCE = str(Path(authority.__file__).parent.parent)


def arguments(store: tuple[Path, str], proposed: Doc | None = None) -> Doc:
    return {
        "store_id": store[1],
        "objective_id": cases.objective()["id"],
        "proposal": proposed or cases.proposal(),
        "approved": True,
        "reason": "trusted authorizer checked exact current scope",
    }


@pytest.mark.parametrize("duplicates", [False, True])
def test_concurrent_processes_cannot_overdraw_or_double_charge_an_objective(
    store: tuple[Path, str], duplicates: bool
) -> None:
    cases.enroll(store, cases.units(3, 150, 3, 75))
    script = """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import standing_authorization as authority
print('ready',flush=True)
sys.stdin.readline()
print(json.dumps(authority.reserve(Path(sys.argv[2]),**json.loads(sys.argv[3]))),flush=True)
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                SOURCE,
                str(store[0]),
                json.dumps(arguments(store, cases.proposal("duplicate" if duplicates else f"effect-{index}"))),
            ],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(8)
    ]
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("run\n")
            process.stdin.flush()
        results: list[Doc] = []
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            results.append(json.loads(output))
        expected = 1 if duplicates else 3
        assert sum(result["approved"] for result in results) == (8 if duplicates else 3)
        inspected = cases.inspect(store)
        assert inspected["reserved"] == cases.units(expected, 50 * expected, expected, 25 * expected)
        assert len(inspected["decisions"]) == (1 if duplicates else 8)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_process_death_after_commit_before_return_retains_the_unreturned_reservation(store: tuple[Path, str]) -> None:
    cases.enroll(store, cases.units(1, 50, 1, 25))
    script = """
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import standing_authorization as authority
authority.os.fsync=lambda descriptor: os._exit(73)
authority.reserve(Path(sys.argv[2]),**json.loads(sys.argv[3]))
raise AssertionError('reserve unexpectedly returned')
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, SOURCE, str(store[0]), json.dumps(arguments(store))],
        check=False,
        timeout=10,
    )
    assert completed.returncode == 73
    retained = cases.inspect(store)
    assert retained["reserved"] == cases.proposal()["cost"]
    assert retained["decisions"][0]["status"] == "reserved"
    assert cases.reserve(store) == retained["decisions"][0]
    assert not cases.reserve(store, cases.proposal("cannot-refill"))["approved"]


def test_process_death_after_simulated_send_does_not_free_an_unsettled_reservation(store: tuple[Path, str]) -> None:
    cases.enroll(store, cases.units(1, 50, 1, 25))
    marker = store[0].parent / "simulated-native-send.json"
    script = """
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import standing_authorization as authority
result=authority.reserve(Path(sys.argv[2]),**json.loads(sys.argv[3]))
assert result['approved']
Path(sys.argv[4]).write_text(json.dumps({'sent_proposal':result['request']['proposal']['id']}))
os._exit(73)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, SOURCE, str(store[0]), json.dumps(arguments(store)), str(marker)],
        check=False,
        timeout=10,
    )
    assert completed.returncode == 73
    sent_bytes = marker.read_bytes()
    replay = cases.reserve(store)
    assert replay["status"] == "reserved"  # A retained reservation supplies no permission to send again.
    uncertain = authority.mark_uncertain(
        store[0],
        store_id=store[1],
        objective_id=cases.objective()["id"],
        proposal_id=cases.proposal()["id"],
        reason="native response unavailable after process death",
    )
    assert uncertain["status"] == "uncertain"
    assert not cases.reserve(store, cases.proposal("replan-with-another-action"))["approved"]
    assert marker.read_bytes() == sent_bytes
    assert cases.inspect(store)["reserved"] == cases.proposal()["cost"]


def test_interrupted_sqlite_transaction_requires_explicit_recovery_and_preserves_input_bytes(
    store: tuple[Path, str],
) -> None:
    cases.enroll(store)
    cases.reserve(store)
    script = """
import os,sqlite3,sys
connection=sqlite3.connect(sys.argv[1],isolation_level=None)
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE objectives SET digest='uncommitted-change'")
os._exit(73)
"""
    completed = subprocess.run([sys.executable, "-I", "-c", script, str(store[0])], check=False, timeout=10)
    assert completed.returncode == 73
    assert Path(str(store[0]) + "-journal").exists()
    before = {item.name: item.read_bytes() for item in store[0].parent.iterdir()}
    with pytest.raises(execution_store.StoreError, match="sidecars"):
        cases.inspect(store)
    with pytest.raises(execution_store.StoreError, match="sidecars"):
        cases.reserve(store)
    assert {item.name: item.read_bytes() for item in store[0].parent.iterdir()} == before


def test_competing_lock_obeys_one_bounded_deadline_without_changing_history(store: tuple[Path, str]) -> None:
    cases.enroll(store)
    script = """
import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
sys.stdin.read(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(store[0].parent)],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        before = store[0].read_bytes()
        with pytest.raises(execution_store.LockTimeoutError, match="timed out"):
            authority.reserve(store[0], **arguments(store), lock_timeout=0)
        assert store[0].read_bytes() == before
    finally:
        process.communicate("x", timeout=5)
