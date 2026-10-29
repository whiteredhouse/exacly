"""History profiles follow installed validation inputs without network or checkout fallback.

These tests reproduce the declared wheel layout using local files, so the normal test suite needs
neither build dependencies nor registry access. The release workflow separately builds real wheels.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "python/src"
Doc = dict[str, Any]
PROFILE = """
import json, socket, sys
sys.path.insert(0, sys.argv[1])
def denied(*args, **kwargs):
    raise AssertionError("profile generation attempted network access")
socket.create_connection = denied
socket.socket.connect = denied
try:
    exec(sys.argv[2])
    from exacly import execution_history
    result = {"profile": execution_history.profile(), "module": execution_history.__file__}
except Exception as error:
    result = {"error_type": type(error).__name__, "message": str(error)}
print(json.dumps(result))
"""


def run_profile(path: Path, setup: str = "") -> Doc:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", PROFILE, str(path), setup],
        cwd=path.parent,
        text=True,
        capture_output=True,
        check=True,
    )
    return dict(json.loads(result.stdout))


def contents(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    site = tmp_path / "venv/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    shutil.copytree(SOURCE / "exacly", site / "exacly", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "spec/schema", site / "exacly/schemas")
    return site


def test_source_and_wheel_layout_have_the_same_profile_without_source_writes(installed: Path) -> None:
    original = contents(installed)
    source = run_profile(SOURCE)
    packaged = run_profile(installed)
    assert "profile" in source, source
    assert "profile" in packaged, packaged
    assert Path(packaged["module"]).is_relative_to(installed)
    assert source["profile"] == packaged["profile"]
    assert contents(installed) == original


@pytest.mark.parametrize("relative", ["completion_state.py", "state-schema"])
def test_changed_installed_semantic_inputs_change_the_profile(installed: Path, relative: str) -> None:
    before = run_profile(installed)
    assert "profile" in before, before
    target = (
        sorted((installed / "exacly/schemas/0.2/state").glob("*.json"))[-1]
        if relative == "state-schema"
        else installed / "exacly" / relative
    )
    with target.open("a") as stream:
        stream.write("\n# changed installed validation input\n" if target.suffix == ".py" else "\n ")
    after = run_profile(installed)
    assert "profile" in after, after
    assert after["profile"]["digest"] != before["profile"]["digest"]


def test_a_broken_installation_does_not_borrow_schemas_from_a_checkout(installed: Path) -> None:
    shutil.rmtree(installed / "exacly/schemas/0.2/state")
    # This is the fallback considered by the ordinary source-checkout schema resolver. A broken
    # installed wheel must not obtain an accepted profile from unrelated files at this location.
    fallback = (installed / "exacly/core.py").resolve().parents[3] / "spec/schema"
    shutil.copytree(ROOT / "spec/schema", fallback)
    result = run_profile(installed)
    assert result.get("error_type") == "HistoryError", result


def test_missing_required_installed_validation_code_fails_closed(installed: Path) -> None:
    (installed / "exacly/manual_qa.py").unlink()
    result = run_profile(installed)
    assert result.get("error_type") in {"HistoryError", "ImportError", "ModuleNotFoundError"}, result
    assert "manual_qa" in result["message"]
    assert "profile" not in result


def test_imported_evidence_schema_cannot_hide_missing_installed_source(installed: Path) -> None:
    schema = installed / "exacly/_evidence_format.py"
    result = run_profile(
        installed,
        f"from pathlib import Path\nfrom exacly import execution_history\nPath({str(schema)!r}).unlink()\n",
    )
    assert result.get("error_type") == "HistoryError", result
    assert "_evidence_format.py" in result["message"]
    assert "profile" not in result


def test_imported_dependency_code_cannot_hide_behind_another_distributions_metadata(installed: Path) -> None:
    before = run_profile(installed)
    assert "profile" in before, before
    shutil.copytree(
        Path(jsonschema.__file__).parent,
        installed / "jsonschema",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    with (installed / "jsonschema/validators.py").open("a") as stream:
        stream.write("\n# changed imported validator bytes; original distribution metadata is still present\n")
    after = run_profile(installed)
    assert after.get("error_type") == "HistoryError" or (
        "profile" in after and after["profile"]["digest"] != before["profile"]["digest"]
    ), after


def test_changed_registered_format_checker_cannot_reuse_the_original_profile(installed: Path) -> None:
    before = run_profile(installed)
    assert "profile" in before, before
    after = run_profile(
        installed,
        "from jsonschema import FormatChecker\n"
        "@FormatChecker.cls_checks('controlled-extra-format')\n"
        "def checker(value):\n"
        "    return isinstance(value, str) and value.startswith('approved:')\n",
    )
    assert after.get("error_type") == "HistoryError" or (
        "profile" in after and after["profile"]["digest"] != before["profile"]["digest"]
    ), after


def test_installed_operational_apis_refuse_a_valid_audit_only_copy(installed: Path, tmp_path: Path) -> None:
    from exacly import history_maintenance
    from test_completion import prepare_scenario

    case = prepare_scenario(tmp_path)
    original_state = case.state
    completed = case.complete()
    destination = tmp_path / "history-copy"
    assert history_maintenance.convert(case.ledger, destination)["status"] == "indexed"
    indexed = destination / "indexed/ledger.sqlite3"
    sources = {path: path.read_bytes() for path in (case.ledger, indexed, case.state_path)}
    payload = {
        "plan": case.plan,
        "readiness": case.readiness,
        "trust": case.source.policy,
        "binding": case.binding,
        "state": original_state,
        "completion_id": completed["completion_id"],
    }
    program = """
import datetime as dt, json, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def denied(*args, **kwargs):
    raise AssertionError("audit-only ledger operation attempted network access")
socket.create_connection = denied
socket.socket.connect = denied
from exacly import completion, executions, execution_store as store
from exacly.applicability import Target
class NoNetwork:
    get_json = denied
    download_artifact = denied
data=json.load(sys.stdin)
ledger, state = Path(sys.argv[2]), Path(sys.argv[3])
binding=data["binding"]
pending=binding["contract"]["pending"]
target=Target(binding["contract"]["target"])
date=dt.date.fromisoformat(binding["as_of"])
action=binding["contract"]["plan"]["action_id"]
request=executions.Registration(data["plan"],action,data["state"],pending["task"],pending["opened_at"],data["readiness"],data["trust"],target,date)
results={}
for name, invoke in {
    "get":lambda:executions.get(ledger,binding["id"]),
    "put":lambda:store.put(ledger,binding),
    "register":lambda:executions.register(ledger,request),
    "show":lambda:completion.show(ledger,data["completion_id"]),
    "recover":lambda:completion.recover(ledger,data["completion_id"],state_path=state),
    "complete":lambda:completion.complete(NoNetwork(),data["plan"],action,data["readiness"],data["trust"],target,777,1,date,ledger=ledger,binding_id=binding["id"],state_path=state),
}.items():
    try:
        invoke()
        results[name]="unexpected success"
    except store.StoreError as error:
        results[name]=str(error)
print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(installed), str(indexed), str(case.state_path)],
        cwd=installed.parent,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    rejected = json.loads(result.stdout)
    assert set(rejected) == {"get", "put", "register", "show", "recover", "complete"}
    assert all("audit-only" in reason for reason in rejected.values()), rejected
    assert {path: path.read_bytes() for path in sources} == sources
