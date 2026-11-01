"""Process faults at the standing/native boundary; all native effects here are controlled files."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from exacly import automation_authorization as standing
from exacly import automation_revocations as revocations
from exacly import execution_store
from test_standing_authorization_scope import Scenario, units
from test_standing_authorization_scope import scenario as scenario

Doc = dict[str, Any]
SOURCE = str(Path(standing.__file__).parent.parent)
TESTS = str(Path(__file__).parent)
BOOTSTRAP = """
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,sys.argv[2])
from exacly import automation_authorization as standing
from test_standing_authorization_scope import InstalledEffect,NOW
config_path=Path(sys.argv[3])
config=json.loads(config_path.read_text())
facts=json.loads(sys.argv[4])
effect=InstalledEffect(facts,Path(facts['native']['store']['path']))
authorizer=standing.Authorizer(config_path,approved_digest=standing.digest(config),
 issuer='independent-maintainer',approval_id='reviewed-packet-001',effects={'execute':effect},clock=lambda:NOW)
request={'task':'fixed fixture inventory'}
effect_id=sys.argv[5]
"""


def command(scenario: Scenario, body: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-c",
        BOOTSTRAP + body,
        SOURCE,
        TESTS,
        str(scenario.config_path),
        json.dumps(scenario.effect.facts),
        standing.digest("effect"),
    ]


def test_duplicate_process_schedules_release_exactly_one_native_invocation(scenario: Scenario) -> None:
    scenario.authorizer()
    body = """
print('ready',flush=True)
sys.stdin.readline()
print(json.dumps(authorizer.execute('execute',request,effect_id=effect_id)),flush=True)
"""
    processes = [
        subprocess.Popen(
            command(scenario, body), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(6)
    ]
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("run\n")
            process.stdin.flush()
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            assert json.loads(output)["status"] == "completed"
        assert scenario.history()["reserved"] == units(1, 1000)
        assert len(scenario.history()["decisions"]) == 1
        assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.parametrize("point", ["before_intent", "prepared", "after_send", "after_terminal"])
def test_process_death_preserves_reservation_and_reconciles_before_any_native_retry(
    scenario: Scenario, point: str
) -> None:
    capabilities = sorted(["finish-prepared-v1", *scenario.config["effects"]["execute"]["capabilities"]])
    scenario.config["effects"]["execute"]["capabilities"] = capabilities
    scenario.effect.facts["capabilities"] = capabilities
    scenario.authorizer()
    bodies = {
        "before_intent": "effect.before_invoke=lambda:os._exit(73)\n",
        "prepared": """
def prepared_crash():
 effect._write({'request':request,'authorization':effect.facts['authorization'],'status':'prepared','send_count':0})
 os._exit(73)
effect.before_invoke=prepared_crash
""",
        "after_send": "effect.after_send=lambda:os._exit(73)\n",
        "after_terminal": """
original_settle=standing.accounting.settle
def die_after_native_terminal(*args,**kwargs):
 if 'native_operation' in kwargs['outcome']['receipt']:
  os._exit(73)
 return original_settle(*args,**kwargs)
standing.accounting.settle=die_after_native_terminal
""",
    }
    body = (
        bodies[point] + "authorizer.execute('execute',request,effect_id=effect_id)\nraise AssertionError('returned')\n"
    )
    result = subprocess.run(command(scenario, body), check=False, timeout=10)
    assert result.returncode == 73
    assert scenario.history()["reserved"] == units(1, 1000)
    assert scenario.history()["decisions"][0]["status"] == "uncertain"
    authorizer = scenario.authorizer()
    discovered = authorizer.reconcile(
        "execute", {"task": "fixed fixture inventory"}, effect_id=standing.digest("effect")
    )
    expected = {
        "before_intent": "not_started",
        "prepared": "recoverable",
        "after_send": "uncertain",
        "after_terminal": "completed",
    }
    assert discovered["status"] == expected[point]
    assert scenario.effect.invocations == 0 and scenario.effect.resumes == 0
    resumed = scenario.execute(authorizer)
    assert resumed["status"] == ("uncertain" if point == "after_send" else "completed")
    assert scenario.history()["reserved"] == units(1, 2000 if point in ("before_intent", "prepared") else 1000)
    assert len(scenario.history()["decisions"]) == 1
    assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1


def test_concurrent_operator_revocation_is_observed_before_native_invocation(scenario: Scenario) -> None:
    scenario.authorizer()
    body = """
def pause_after_reservation(count):
 if count==2:
  print('reserved',flush=True)
  sys.stdin.readline()
effect.before_inspect=pause_after_reservation
print(json.dumps(authorizer.execute('execute',request,effect_id=effect_id)),flush=True)
"""
    process = subprocess.Popen(
        command(scenario, body), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "reserved"
        scenario.revoke()
        output, error = process.communicate("continue\n", timeout=10)
        assert process.returncode == 0, error
        report = json.loads(output)
        assert report["status"] == "paused" and "revoked" in report["reason"]
        assert not scenario.effect.native_path.exists()
        assert scenario.history()["reserved"] == units(1, 1000)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_revocation_commit_survives_process_death_before_return(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scope = scenario.config["revocations"]
    script = """
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import automation_revocations as revocations
revocations.os.fsync=lambda descriptor:os._exit(73)
revocations.revoke(Path(sys.argv[2]),store_id=sys.argv[3],delegation_id=sys.argv[4],reason='operator stop')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, SOURCE, scope["path"], scope["store_id"], scenario.config["id"]],
        check=False,
        timeout=10,
    )
    assert result.returncode == 73
    assert not authorizer.check_available()["available"]
    assert scenario.execute(authorizer)["status"] == "denied"
    assert scenario.effect.invocations == 0
    assert scenario.history()["reserved"] == units(0, 0)


@pytest.mark.parametrize("damage", ["schema", "version", "identity", "changed_record", "sidecar", "replacement"])
def test_unavailable_or_untrusted_revocation_history_never_becomes_active_authority(
    scenario: Scenario, damage: str
) -> None:
    authorizer = scenario.authorizer()
    scenario.revoke()
    path = Path(scenario.config["revocations"]["path"])
    if damage == "sidecar":
        Path(str(path) + "-journal").write_bytes(b"unresolved rollback history")
    elif damage == "replacement":
        replacement = path.with_name("replacement.sqlite3")
        revocations.initialize(replacement)
        os.replace(replacement, path)
    else:
        with sqlite3.connect(path) as connection:
            statement = {
                "schema": "CREATE TABLE unexpected (value TEXT)",
                "version": "PRAGMA user_version=999",
                "identity": "UPDATE revocation_store SET id='invalid-uuid'",
                "changed_record": "UPDATE revocations SET digest='changed'",
            }[damage]
            connection.execute(statement)
    before = {item.name: item.read_bytes() for item in path.parent.iterdir()}
    assert not authorizer.check_available()["available"]
    assert scenario.execute(authorizer)["status"] == "paused"
    assert scenario.effect.invocations == 0
    assert {item.name: item.read_bytes() for item in path.parent.iterdir()} == before


def test_revocation_is_monotonic_and_exact_replay_never_rewrites_history(scenario: Scenario) -> None:
    scenario.authorizer()
    record = scenario.revoke()
    path = Path(scenario.config["revocations"]["path"])
    before = path.read_bytes()
    assert scenario.revoke() == record
    assert path.read_bytes() == before
    with pytest.raises(execution_store.StoreError, match="conflicts"):
        revocations.revoke(
            path,
            store_id=scenario.config["revocations"]["store_id"],
            delegation_id=scenario.config["id"],
            reason="rewrite revocation",
        )
    assert path.read_bytes() == before


def test_owner_permissions_symlinks_and_duplicate_config_keys_cannot_select_authority(scenario: Scenario) -> None:
    scenario.write()
    selection: Doc = {
        "approved_digest": standing.digest(scenario.config),
        "issuer": "independent-maintainer",
        "approval_id": "reviewed-packet-001",
        "effects": {"execute": scenario.effect},
    }
    scenario.config_path.chmod(0o644)
    with pytest.raises(execution_store.StoreError, match="0600"):
        standing.Authorizer(scenario.config_path, **selection)
    scenario.config_path.chmod(0o600)
    alias = scenario.config_path.with_name("symlink.json")
    alias.symlink_to(scenario.config_path)
    with pytest.raises(execution_store.StoreError, match="symbolic links"):
        standing.Authorizer(alias, **selection)
    content = scenario.config_path.read_text()
    scenario.config_path.write_text('{"id":"candidate",' + content[1:])
    with pytest.raises(execution_store.StoreError, match="duplicate keys"):
        standing.Authorizer(scenario.config_path, **selection)
    assert scenario.effect.invocations == 0


def test_unknown_native_success_labels_cannot_settle_reserved_capacity(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    scenario.effect.invocation_override = {"status": "passed", "receipt": {"candidate_claim": "success"}}
    result = scenario.execute(authorizer)
    assert result["status"] == "uncertain"
    assert scenario.history()["decisions"][0]["status"] == "uncertain"
    assert scenario.history()["reserved"] == units(1, 1000)
    assert scenario.execute(authorizer)["status"] == "uncertain"
    assert scenario.effect.invocations == 1


METER_BOOTSTRAP = """
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,sys.argv[2])
from exacly import automation_authorization as standing
from test_standing_authorization_scope import ControlledReader,InstalledEffect,NOW
config_path=Path(sys.argv[3])
config=json.loads(config_path.read_text())
facts=json.loads(sys.argv[4])
scope=config['reads']['producer-observe']
reader=ControlledReader(config['repository'],scope['contract_digest'],scope['max_wall_time_ms'],Path(sys.argv[7]))
effect=InstalledEffect(facts,Path(facts['native']['store']['path']))
authorizer=standing.Authorizer(config_path,approved_digest=sys.argv[5],issuer='independent-maintainer',
 approval_id=sys.argv[6],effects={'execute':effect},readers={'producer-observe':reader},clock=lambda:NOW)
"""


def test_concurrent_approved_policy_versions_cannot_reset_or_overdraw_cumulative_read_time(scenario: Scenario) -> None:
    import copy

    from test_standing_authorization_scope import metered_reader, raw_history

    reader = metered_reader(scenario, budget=300, bound=100)
    scenario.authorizer(readers={"producer-observe": reader})
    other_config = copy.deepcopy(scenario.config)
    other_path = scenario.config_path.with_name("independently-approved-v2.json")
    other_config.update(config_path=str(other_path), id="independently-approved-v2", approval_id="reviewed-packet-002")
    other_path.write_text(json.dumps(other_config))
    other_path.chmod(0o600)
    read_log = scenario.config_path.with_name("metered-read.log")
    body = """
print('ready',flush=True)
sys.stdin.readline()
print(json.dumps(authorizer.call_metered('producer-observe',{},operation_id=standing.digest('same-correlated-phase'))),flush=True)
"""
    processes = []
    for index in range(6):
        config = scenario.config if index % 2 else other_config
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                METER_BOOTSTRAP + body,
                SOURCE,
                TESTS,
                config["config_path"],
                json.dumps(scenario.effect.facts),
                standing.digest(config),
                config["approval_id"],
                str(read_log),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("run\n")
            process.stdin.flush()
        results = []
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            results.append(json.loads(output))
        assert sum(item["status"] == "completed" for item in results) == 3
        assert sum(item["status"] == "denied" for item in results) == 3
        assert read_log.read_bytes().splitlines() == [b"read"] * 3
        history = raw_history(scenario)
        assert history["reserved"] == units(0, 300)
        assert len({item["request"]["proposal"]["id"] for item in history["decisions"]}) == 6
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_read_time_survives_process_death_and_new_controller_request_ids(scenario: Scenario) -> None:
    from test_standing_authorization_scope import metered_reader, raw_history

    reader = metered_reader(scenario, budget=100, bound=100)
    scenario.authorizer(readers={"producer-observe": reader})
    log = scenario.config_path.with_name("metered-read.log")
    body = """
reader.after_read=lambda:os._exit(73)
authorizer.call_metered('producer-observe',{},operation_id=standing.digest('durable-original-request'))
raise AssertionError('returned')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            METER_BOOTSTRAP + body,
            SOURCE,
            TESTS,
            str(scenario.config_path),
            json.dumps(scenario.effect.facts),
            standing.digest(scenario.config),
            scenario.config["approval_id"],
            str(log),
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 73
    history = raw_history(scenario)
    assert history["reserved"] == units(0, 100)
    assert history["decisions"][0]["status"] == "uncertain"
    resumed = scenario.authorizer(readers={"producer-observe": reader})
    denied = resumed.call_metered("producer-observe", {}, operation_id=standing.digest("new-controller-alias"))
    assert denied["status"] == "denied" and reader.calls == 0
    assert log.read_bytes() == b"read\n"
    assert raw_history(scenario)["reserved"] == units(0, 100)


def test_simultaneous_different_policy_grants_have_one_objective_writer_even_before_any_native_history(
    scenario: Scenario,
) -> None:
    from test_standing_authorization_scope import alternate_policy, raw_history

    scenario.authorizer()
    replacement = alternate_policy(scenario)
    replacement.authorizer()
    body = """
effect.after_send=lambda:(_ for _ in ()).throw(OSError('native response lost'))
print('ready',flush=True)
sys.stdin.readline()
print(json.dumps(authorizer.execute('execute',request,effect_id=effect_id)),flush=True)
"""
    processes = []
    for index, current in enumerate((scenario, replacement)):
        args = command(current, body)
        args[-1] = standing.digest("separate-phase-" + str(index))
        processes.append(
            subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        )
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("run\n")
            process.stdin.flush()
        statuses = []
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            statuses.append(json.loads(output)["status"])
        assert sorted(statuses) == ["paused", "uncertain"]
        native_files = [item.effect.native_path for item in (scenario, replacement) if item.effect.native_path.exists()]
        assert len(native_files) == 1 and json.loads(native_files[0].read_text())["send_count"] == 1
        assert raw_history(scenario)["reserved"] == units(1, 1000)
        assert len(scenario.history()["decisions"]) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_objective_mutex_wait_is_bounded_without_holding_the_accounting_directory_lock(scenario: Scenario) -> None:
    scenario.authorizer()
    path = next(scenario.config_path.parent.glob(".exacly-objective-*.lock"))
    script = """
import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDONLY)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
sys.stdin.read(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        # Constructor can still inspect/enroll accounting while the independent file mutex is held.
        authorizer = scenario.authorizer(lock_timeout=0)
        blocked = scenario.execute(authorizer)
        assert blocked["status"] == "paused" and "timed out" in blocked["reason"]
        assert scenario.effect.invocations == 0
        assert scenario.history()["reserved"] == units(0, 0)
    finally:
        process.communicate("release", timeout=5)


def test_active_cancellation_does_not_wait_for_the_objective_launch_owner(scenario: Scenario) -> None:
    import time

    scope = scenario.config["effects"]["execute"]
    scope["cancel_inflight"] = True
    capabilities = sorted(["cancel-inflight-v1", *scope["capabilities"]])
    scope["capabilities"] = scenario.effect.facts["capabilities"] = capabilities
    authorizer = scenario.authorizer(lock_timeout=0.15)
    body = """
def supervise_until_cancel(request,authorization):
 native=json.loads(effect.native_path.read_text())
 native.update(status='sent',send_count=1)
 effect._write(native)
 print('running',flush=True)
 sys.stdin.readline()
 native=json.loads(effect.native_path.read_text())
 assert native['status']=='terminal',native
 return native['outcome']
effect.resume=supervise_until_cancel
print(json.dumps(authorizer.execute('execute',request,effect_id=effect_id)),flush=True)
"""
    process = subprocess.Popen(
        command(scenario, body), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "running"
        started = time.monotonic()
        report = authorizer.request_cancel(
            "execute", {"task": "fixed fixture inventory"}, effect_id=standing.digest("effect")
        )
        elapsed = time.monotonic() - started
        assert report["status"] == "cancelled", report
        assert elapsed < 1.0
        output, error = process.communicate("observe-cancellation\n", timeout=5)
        assert process.returncode == 0, error
        assert json.loads(output)["status"] == "cancelled"
        assert json.loads(scenario.effect.native_path.read_text())["send_count"] == 1
        assert scenario.history()["reserved"] == units(1, 2000)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
