"""The executor CLI requires reviewed bounded effects and keeps candidate output off the terminal."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from exacly.cli import exacly_main
from executor_fixtures import ExecutorScenario
from executor_fixtures import dispatch_scenario as dispatch_scenario
from executor_fixtures import executor_scenario as executor_scenario
from executor_fixtures import isolation_runtime as isolation_runtime

Doc = dict[str, Any]
ACTION = "sha256:" + "a" * 64
BINDING = "sha256:" + "b" * 64
EXECUTION = "sha256:" + "e" * 64
TARGET = {"context": "main", "model_revision": "1" * 40, "revision": "2" * 40}
TOKEN = "synthetic-executor-token"


def answer(status: str, *, dry_run: bool = False) -> Doc:
    return {"schema_version": "0.1.0", "operation": "executor", "status": status, "dry_run": dry_run}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Doc:
    import exacly

    calls: Doc = {
        "preview": [], "apply": [], "show": [], "cancel": [], "recover": [], "recover_journal": [],
        "initialize": [], "report": answer("succeeded"), "error": None,
    }  # fmt: skip
    module = ModuleType("exacly.executor")
    storage = ModuleType("exacly.executor_store")

    def operation(name: str) -> Any:
        def run(*args: Any, **kwargs: Any) -> Doc:
            calls[name].append((args, kwargs))
            if calls["error"] is not None:
                raise ValueError(calls["error"])
            if name == "preview":
                return answer("ready", dry_run=True)
            if name == "recover_journal":
                return answer("recovered")
            return dict(calls["report"])

        return run

    def initialize(*args: Any, **kwargs: Any) -> Doc:
        calls["initialize"].append((args, kwargs))
        return {"schema_version": "0.1.0", "status": "initialized", "journal_id": "f" * 32}

    monkeypatch.setattr(module, "Request", lambda **kwargs: SimpleNamespace(**kwargs), raising=False)
    for name in ("preview", "apply", "show", "cancel", "recover", "recover_journal"):
        monkeypatch.setattr(module, name, operation(name), raising=False)
    monkeypatch.setattr(module, "as_text", lambda report: f"Executor: {report['status']}", raising=False)
    monkeypatch.setattr(storage, "initialize", initialize, raising=False)
    monkeypatch.setitem(sys.modules, "exacly.executor", module)
    monkeypatch.setitem(sys.modules, "exacly.executor_store", storage)
    monkeypatch.setattr(exacly, "executor", module, raising=False)
    monkeypatch.setattr(exacly, "executor_store", storage, raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return calls


def invocation(tmp_path: Path, *, apply: bool = False, inputs: bool = True) -> tuple[list[str], dict[str, Path]]:
    model = tmp_path / "model" / "model.yaml"
    model.parent.mkdir()
    model.write_text("exacly: 0.2.0\nnamespace: demo\n")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    paths = {
        "model": model,
        "journal": private / "attempts.sqlite3",
        "ledger": private / "bindings.sqlite3",
        "output_root": private / "runs",
    }
    documents: Doc = {
        "plan": {"saved": "plan"},
        "policy": {"reviewed": "readiness"},
        "trust-policy": {"model": {"files": [str(model)]}},
        "target": TARGET,
        "state": {"actual": "current state"},
        "authorization": {"reviewed": "attempt", "execution_id": EXECUTION},
        "inputs": {"schema_version": "0.1.0", "files": {"source.py": "print('こんにちは')\n"}},
    }
    paths.update({key: tmp_path / f"{key}.json" for key in documents})
    documents["executor-policy"] = {
        "reviewed": "executor",
        "storage": {key: str(paths[key]) for key in ("journal", "ledger", "state", "output_root")},
    }
    paths["executor-policy"] = tmp_path / "executor-policy.json"
    for key, document in documents.items():
        paths[key].write_text(json.dumps(document))
    args = ["execute", "local", "-m", str(model), "--action", ACTION, "--binding", BINDING, "--date", "2026-10-30"]
    for key in ("plan", "policy", "trust-policy", "executor-policy", "target", "state"):
        args.extend([f"--{key}", str(paths[key])])
    if inputs:
        args.extend(["--inputs", str(paths["inputs"])])
    if apply:
        args.append("--apply")
        for key in ("journal", "ledger", "authorization"):
            args.extend([f"--{key}", str(paths[key])])
    return args, paths


def replace_arg(args: list[str], option: str, value: str) -> list[str]:
    result = args.copy()
    result[result.index(option) + 1] = value
    return result


@pytest.mark.parametrize("explicit", [False, True])
def test_preview_is_read_only_offline_and_preserves_reviewed_snapshots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, explicit: bool
) -> None:
    args, paths = invocation(tmp_path)
    before = {path: path.read_bytes() for path in paths.values() if path.is_file()}
    if explicit:
        args.append("--dry-run")
    assert exacly_main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == answer("ready", dry_run=True)
    (documents, state, request), options = backend["preview"][0]
    assert documents == {str(paths["model"]): {"exacly": "0.2.0", "namespace": "demo"}}
    assert state == paths["state"] and options == {}
    assert request.action_id == ACTION and request.binding_id == BINDING
    assert request.date == dt.date(2026, 10, 30) and request.target.data == TARGET
    assert request.inputs == {"source.py": "print('こんにちは')\n".encode()}
    assert not backend["apply"] and not backend["initialize"]
    assert not paths["journal"].exists() and not paths["ledger"].exists() and not paths["output_root"].exists()
    assert all(path.read_bytes() == content for path, content in before.items())


def test_apply_routes_one_explicit_authorization_and_lock_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, paths = invocation(tmp_path, apply=True, inputs=False)
    assert exacly_main([*args, "--lock-timeout", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == answer("succeeded")
    (journal, ledger, documents, state, request), options = backend["apply"][0]
    assert (journal, ledger, state) == (paths["journal"], paths["ledger"], paths["state"])
    assert request.executor_policy == json.loads(paths["executor-policy"].read_text())
    assert request.inputs == {} and request.trust_policy["model"]["files"] == [str(paths["model"])]
    assert documents[str(paths["model"])]["namespace"] == "demo"
    assert options == {"authorization": {"reviewed": "attempt", "execution_id": EXECUTION}, "lock_timeout": 0}
    assert not backend["preview"] and not backend["initialize"]


@pytest.mark.parametrize("missing", ["journal", "ledger", "authorization"])
def test_apply_requires_storage_and_separate_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, missing: str
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    offset = args.index(f"--{missing}")
    del args[offset : offset + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2 and "requires" in capsys.readouterr().err
    assert not backend["apply"]


def test_authorization_cannot_accidentally_turn_preview_into_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, paths = invocation(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, "--authorization", str(paths["authorization"])])
    assert error.value.code == 2 and "requires --apply" in capsys.readouterr().err
    assert not backend["preview"] and not backend["apply"]


@pytest.mark.parametrize("option", ["journal", "ledger"])
def test_preview_storage_overrides_cannot_select_another_coordinator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, option: str
) -> None:
    args, paths = invocation(tmp_path)
    supplied = {"journal": paths["journal"], "ledger": paths["ledger"]}
    supplied[option] = tmp_path / "other.sqlite3"
    assert exacly_main([*args, "--journal", str(supplied["journal"]), "--ledger", str(supplied["ledger"])]) == 2
    assert "differs from the reviewed executor storage" in capsys.readouterr().err
    assert not backend["preview"]


@pytest.mark.parametrize(
    "status",
    [
        "failed",
        "timed_out",
        "cancelled",
        "interrupted",
        "output_invalid",
        "runtime_failed",
        "output_limit",
        "attempts_exhausted",
    ],
)
def test_unsuccessful_execution_is_exit_one_without_candidate_log_rendering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, status: str
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["report"] = {**answer(status), "candidate_log": "untrusted terminal escape content"}
    assert exacly_main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == f"Executor: {status}\n" and not captured.err
    assert "untrusted" not in captured.out


@pytest.mark.parametrize("operation", ["show", "cancel", "recover"])
def test_retained_operations_need_only_journal_and_execution_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, operation: str
) -> None:
    journal = tmp_path / "attempts.sqlite3"
    assert (
        exacly_main(
            [
                "executors",
                operation,
                "--journal",
                str(journal),
                "--execution",
                EXECUTION,
                "--lock-timeout",
                "0",
                "--json",
            ]
        )
        == 0
    )
    assert backend[operation] == [((journal, EXECUTION), {"lock_timeout": 0})]
    assert json.loads(capsys.readouterr().out) == answer("succeeded")
    assert not backend["apply"] and not backend["preview"]


def test_missing_retained_execution_is_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    backend["report"] = answer("missing")
    assert exacly_main(["executors", "show", "--journal", str(tmp_path / "journal"), "--execution", EXECUTION]) == 1
    assert capsys.readouterr().out == "Executor: missing\n"


def test_explicit_init_fixes_the_coordinator_ledger_and_concurrency_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    journal, ledger = tmp_path / "attempts.sqlite3", tmp_path / "bindings.sqlite3"
    assert (
        exacly_main(
            [
                "executors",
                "init",
                "--journal",
                str(journal),
                "--ledger",
                str(ledger),
                "--max-concurrency",
                "2",
                "--json",
            ]
        )
        == 0
    )
    assert backend["initialize"] == [((journal,), {"ledger": ledger, "max_concurrency": 2})]
    assert json.loads(capsys.readouterr().out)["journal_id"] == "f" * 32


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink", "sidecar"])
def test_initialization_rejects_binding_ledger_aliases_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, alias: str
) -> None:
    ledger = tmp_path / "bindings.sqlite3"
    ledger.write_bytes(b"retained binding ledger")
    journal = tmp_path / "attempts.sqlite3"
    if alias == "same":
        journal = ledger
    elif alias == "symlink":
        journal.symlink_to(ledger)
    elif alias == "hardlink":
        os.link(ledger, journal)
    else:
        journal = Path(str(ledger) + "-journal")
    assert (
        exacly_main(
            [
                "executors",
                "init",
                "--journal",
                str(journal),
                "--ledger",
                str(ledger),
                "--max-concurrency",
                "1",
            ]
        )
        == 2
    )
    assert "cannot alias" in capsys.readouterr().err and not backend["initialize"]
    assert ledger.read_bytes() == b"retained binding ledger"


@pytest.mark.parametrize("authorized", [False, True])
def test_sqlite_recovery_routes_only_the_explicit_repair_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, authorized: bool
) -> None:
    journal = tmp_path / "attempts.sqlite3"
    args = ["executors", "recover-journal", "--journal", str(journal), "--json"]
    if authorized:
        args.append("--authorize-recovery")
    assert exacly_main(args) == 0
    assert backend["recover_journal"] == [((journal,), {"authorize": authorized})]
    assert json.loads(capsys.readouterr().out)["status"] == "recovered"
    assert not backend["recover"] and not backend["apply"]


@pytest.mark.parametrize("document", ["plan", "policy", "trust-policy", "executor-policy", "target", "authorization"])
def test_ambiguous_json_is_rejected_before_backend_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, document: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    paths[document].write_text('{"duplicate":1,"duplicate":2}')
    assert exacly_main(args) == 2
    assert "duplicate JSON key" in capsys.readouterr().err
    assert not backend["apply"] and not backend["preview"]


def test_input_manifest_never_discovers_host_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    paths["inputs"].write_text(
        json.dumps({"schema_version": "0.1.0", "files": {"secret": {"host_path": "/etc/passwd"}}})
    )
    assert exacly_main(args) == 2
    assert "UTF-8 text strings" in capsys.readouterr().err
    assert not backend["apply"]


@pytest.mark.parametrize("resource", ["state", "plan", "inputs", "journal", "ledger", "model"])
def test_output_storage_cannot_overlap_reviewed_resources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, resource: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    selected = json.loads(paths["executor-policy"].read_text())
    selected["storage"]["output_root"] = str(paths[resource])
    paths["executor-policy"].write_text(json.dumps(selected))
    assert exacly_main(args) == 2
    assert "output root" in capsys.readouterr().err and not backend["apply"]


@pytest.mark.parametrize(
    "field,value", [("schema_version", "unsupported"), ("status", "invented"), ("status", "ready"), ("dry_run", True)]
)
def test_invalid_backend_reports_do_not_claim_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, field: str, value: Any
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["report"][field] = value
    assert exacly_main(args) == 2
    assert "invalid report" in capsys.readouterr().err


def test_failures_redact_available_credentials_without_using_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, backend: Doc
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    backend["error"] = f"backend failed with {TOKEN}"
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and "[redacted]" in captured.err
    assert os.environ["GH_TOKEN"] == TOKEN


@pytest.mark.parametrize("seconds", ["-1", "NaN", "Infinity", "-Infinity", "invalid"])
def test_invalid_lock_budget_prevents_any_executor_effect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, seconds: str
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, f"--lock-timeout={seconds}"])
    assert error.value.code == 2 and "nonnegative" in capsys.readouterr().err
    assert not backend["apply"]


@pytest.mark.parametrize("limit", ["0", "17", "-1", "invalid"])
def test_invalid_coordinator_limit_prevents_initialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, limit: str
) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main(
            [
                "executors",
                "init",
                "--journal",
                str(tmp_path / "attempts.sqlite3"),
                "--ledger",
                str(tmp_path / "bindings.sqlite3"),
                f"--max-concurrency={limit}",
            ]
        )
    assert error.value.code == 2 and "max-concurrency" in capsys.readouterr().err
    assert not backend["initialize"]


def real_invocation(item: ExecutorScenario, tmp_path: Path) -> tuple[list[str], Path]:
    selected = item.request
    directory = tmp_path / "cli-inputs"
    directory.mkdir()
    model = directory / "model.json"
    model.write_text(json.dumps(next(iter(item.documents.values()))))
    documents = {
        "plan": selected.plan,
        "policy": selected.readiness_policy,
        "trust-policy": selected.trust_policy,
        "executor-policy": selected.executor_policy,
        "target": selected.target.data,
        "inputs": {
            "schema_version": "0.1.0",
            "files": {name: content.decode() for name, content in selected.inputs.items()},
        },
    }
    paths = {name: directory / f"{name}.json" for name in documents}
    for name, value in documents.items():
        paths[name].write_text(json.dumps(value))
    args = [
        "execute",
        "local",
        "-m",
        str(model),
        "--action",
        selected.action_id,
        "--binding",
        selected.binding_id,
        "--date",
        selected.date.isoformat(),
        "--state",
        str(item.state_path),
        "--json",
    ]
    for name, path in paths.items():
        args.extend([f"--{name}", str(path)])
    return args, directory / "authorization.json"


def test_real_cli_preview_retains_exact_reviewable_effect_without_running_a_worker(
    executor_scenario: ExecutorScenario,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exacly import executor_runtime

    def forbidden(*args: Any, **kwargs: Any) -> Doc:
        raise AssertionError("an execution preview must never start a worker")

    monkeypatch.setattr(executor_runtime, "run", forbidden)
    item = executor_scenario
    args, _ = real_invocation(item, tmp_path)
    before = item.authoritative_bytes(), item.journal.read_bytes()
    assert exacly_main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "ready" and preview["dry_run"] is True
    assert preview["effect"]["argv"] == item.policy["argv"]
    assert preview["effect"]["permitted_repository_effects"] == []
    assert preview["authorization_template"]["attempt"] == 1
    assert (item.authoritative_bytes(), item.journal.read_bytes()) == before
    assert list(item.output_root.iterdir()) == []


def test_real_cli_apply_publishes_identified_build_and_exact_replay_preserves_current_work(
    executor_scenario: ExecutorScenario,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exacly import executor_runtime

    item = executor_scenario
    args, authorization = real_invocation(item, tmp_path)
    assert exacly_main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    authorization.write_text(json.dumps(preview["authorization_template"]))
    apply_args = [
        *args,
        "--apply",
        "--authorization",
        str(authorization),
        "--journal",
        str(item.journal),
        "--ledger",
        str(item.ledger),
    ]
    before = item.authoritative_bytes()
    assert exacly_main(apply_args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "succeeded", applied
    receipt = applied["receipt"]
    assert receipt["build"]["id"].startswith("sha256:")
    assert receipt["task"] == item.dispatch.state["pending"][0]["task"]
    assert item.authoritative_bytes() == before
    retained = item.journal.read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Doc:
        raise AssertionError("an exact authorization replay must never start another worker")

    monkeypatch.setattr(executor_runtime, "run", forbidden)
    assert exacly_main(apply_args) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["receipt"] == receipt
    assert item.journal.read_bytes() == retained and item.authoritative_bytes() == before
    assert all(value is False for value in receipt["claims"].values())
