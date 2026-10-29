"""Operational reuse works in the declared installed layout under an ordinary runtime.

Local layout copies keep regular tests independent of build dependencies and registries. Separate
release QA builds a real wheel and repeats the same profile and operational checks.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from test_execution_history import source_rows
from test_execution_history import v2 as v2_fixture
from test_history_distribution import SOURCE, contents
from test_history_distribution import installed as installed_fixture

Doc = dict[str, Any]
installed = installed_fixture
v2 = v2_fixture
PROGRAM = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from exacly import execution_index as index
from exacly import execution_store as store
from exacly import executions, history_maintenance, planning
data = json.load(sys.stdin)
exec(data["program"])
print(json.dumps(result))
"""


def invoke(installed: Path, program: str, **data: Any) -> Doc:
    """Use actual dependency imports without patching networking or validation functions."""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", PROGRAM, str(installed)],
        cwd=installed.parent,
        input=json.dumps({"program": program, **data}),
        text=True,
        capture_output=True,
        check=True,
    )
    return dict(json.loads(completed.stdout))


def binding_id(path: Path) -> str:
    return str(source_rows(path)["bindings"][0][0])


def state_path(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        text = connection.execute("SELECT intent_json FROM completion_intents LIMIT 1").fetchone()[0]
    return Path(json.loads(text)["request"]["state_path"])


def test_operational_source_and_installed_profiles_match_without_runtime_patches(installed: Path) -> None:
    original = contents(installed)
    program = 'result = {"profile": index.profile(), "module": index.__file__}'
    source = invoke(SOURCE, program)
    packaged = invoke(installed, program)
    assert source["profile"] == packaged["profile"]
    assert packaged["profile"]["family"] == "exacly-ledger-operational-v1"
    assert Path(packaged["module"]).is_relative_to(installed)
    assert contents(installed) == original


def test_installed_activation_reuses_terminal_proof_and_full_audit_revalidates_it(
    installed: Path, v2: Path, tmp_path: Path
) -> None:
    original = v2.read_bytes()
    state = state_path(v2)
    original_state = state.read_bytes()
    source = source_rows(v2)
    layout = contents(installed)
    destination = tmp_path / "operational-copy"
    activated = invoke(
        installed,
        "result = history_maintenance.activate(Path(data['source']), Path(data['out']))",
        source=str(v2),
        out=str(destination),
    )
    assert activated["status"] == "activated"
    operational = destination / "operational/ledger.sqlite3"
    assert (destination / "raw/ledger.sqlite3").read_bytes() == original
    assert source_rows(operational) == source
    copied = operational.read_bytes()
    ordinary = invoke(
        installed,
        "result = executions.get(Path(data['ledger']), data['binding'])",
        ledger=str(operational),
        binding=binding_id(v2),
    )
    assert ordinary["validation"]["profile_status"] == "current"
    assert ordinary["validation"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 2, "prepared": 0}
    audited = invoke(
        installed,
        "result = history_maintenance.audit(Path(data['ledger']))",
        ledger=str(operational),
    )
    assert audited["status"] == "passed"
    assert audited["validation"]["semantics"] == {"bindings": 1, "fresh": 2, "reused": 0, "prepared": 0}
    assert operational.read_bytes() == copied
    assert v2.read_bytes() == original
    assert state.read_bytes() == original_state
    assert contents(installed) == layout


def test_installed_stale_profile_requires_copy_refresh_before_ordinary_writes(
    installed: Path, v2: Path, tmp_path: Path
) -> None:
    source_bytes = v2.read_bytes()
    state = state_path(v2)
    original_state = state.read_bytes()
    destination = tmp_path / "stale-copy"
    invoke(
        installed,
        "result = history_maintenance.activate(Path(data['source']), Path(data['out']))",
        source=str(v2),
        out=str(destination),
    )
    ledger = destination / "operational/ledger.sqlite3"
    program = """
import copy, sqlite3
ledger = Path(data['ledger'])
with sqlite3.connect(ledger) as connection:
    profile = json.loads(connection.execute('SELECT profile_json FROM history_profile').fetchone()[0])
    profile.pop('digest')
    profile['inputs'][0]['digest'] = 'sha256:' + '0' * 64
    profile['digest'] = planning.digest(profile)
    connection.execute('UPDATE history_profile SET profile_json=?', (store.canonical(profile),))
    connection.execute('UPDATE history_semantics SET profile_digest=?', (profile['digest'],))
envelope = executions.get(ledger, data['binding'])
binding = copy.deepcopy(envelope['binding'])
with sqlite3.connect(ledger) as connection:
    row = connection.execute("SELECT after_state FROM completion_intents WHERE phase='committed'").fetchone()
    state = json.loads(row[0])
state['serial'] += 1
state['pending'] = [{
    'record': 'REQ-1', 'task': 'issues/installed-next',
    'opened_at': '2026-10-26T08:00:00Z', 'dimension': 'verification',
}]
pending, _ = executions.selected_pending(state, 'REQ-1', 'issues/installed-next', '2026-10-26T08:00:00Z', False)
binding['contract']['pending'] = pending
binding['pending_key'] = planning.digest({key: pending[key] for key in executions.IDENTITY_KEYS})
binding['supersedes'] = None
binding['state_snapshot'] = {
    'serial': state['serial'], 'digest': planning.digest(state), 'config_revision': state['config_revision'],
}
binding['id'] = executions.record_identity(binding)
try:
    store.put(ledger, binding)
    error = 'unexpected success'
except store.StoreError as caught:
    error = str(caught)
result = {'validation': envelope['validation'], 'binding': binding, 'write_error': error}
"""
    stale = invoke(installed, program, ledger=str(ledger), binding=binding_id(v2))
    assert stale["validation"]["profile_status"] == "known_stale"
    assert stale["validation"]["semantics"] == {"bindings": 1, "fresh": 2, "reused": 0, "prepared": 0}
    assert "activate refresh" in stale["write_error"]
    before = ledger.read_bytes()
    refreshed = tmp_path / "refreshed-copy"
    output = invoke(
        installed,
        """
report = history_maintenance.activate(Path(data['source']), Path(data['out']))
ledger = Path(data['out']) / 'operational/ledger.sqlite3'
before = executions.get(ledger, data['old_binding'])
registered = store.put(ledger, data['binding'])
after = executions.get(ledger, registered['id'])
result = {'status': report['status'], 'before': before['validation'], 'after': after}
""",
        source=str(ledger),
        out=str(refreshed),
        old_binding=binding_id(v2),
        binding=stale["binding"],
    )
    assert output["status"] == "activated"
    assert output["before"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 2, "prepared": 0}
    assert output["after"]["binding"] == stale["binding"]
    assert output["after"]["validation"]["semantics"] == {"bindings": 2, "fresh": 0, "reused": 2, "prepared": 0}
    assert ledger.read_bytes() == before
    assert (refreshed / "raw/ledger.sqlite3").read_bytes() == before
    assert v2.read_bytes() == source_bytes
    assert state.read_bytes() == original_state
