"""Dispatch defaults to an offline preview; apply requires a separate reviewed authorization."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from dispatch_fixtures import Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly.cli import exacly_main

Doc = dict[str, Any]
ACTION = "sha256:" + "a" * 64
DISPATCH = "sha256:" + "d" * 64
TARGET = {"context": "main", "model_revision": "1" * 40, "revision": "2" * 40}
TOKEN = "synthetic-dispatch-token"


def answer(status: str, *, dry_run: bool = False) -> Doc:
    return {"schema_version": "0.1.0", "operation": "dispatch", "status": status, "dry_run": dry_run}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Doc:
    import exacly
    from exacly import github_dispatch

    calls: Doc = {
        "preview": [], "apply": [], "recover": [], "show": [], "supersede": [], "initialize": [],
        "tokens": [], "remote": [], "live": False, "report": answer("registered"),
    }  # fmt: skip
    module = ModuleType("exacly.dispatch")
    storage = ModuleType("exacly.dispatch_store")

    def request(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def preview(*args: Any, **kwargs: Any) -> Doc:
        calls["preview"].append((args, kwargs))
        return answer("ready", dry_run=True)

    def retained(name: str) -> Any:
        def operation(*args: Any, **kwargs: Any) -> Doc:
            calls[name].append((args, kwargs))
            if name in {"apply", "recover"} and calls["live"]:
                assert kwargs["client"].repository({"full_name": "acme/demo", "id": 123})["id"] == 123
            return dict(calls["report"])

        return operation

    def initialize(*args: Any, **kwargs: Any) -> Doc:
        calls["initialize"].append((args, kwargs))
        return {
            "schema_version": "0.1.0",
            "operation": "initialize-dispatch-journal",
            "status": "initialized",
            "journal_id": "f" * 32,
        }

    class Client:
        def __init__(self, token: str) -> None:
            calls["tokens"].append(token)

        def repository(self, selected: Doc) -> Doc:
            calls["remote"].append(selected)
            return selected

        def discover(self, repository: Doc, marker: str, effect: Doc) -> list[Doc]:
            raise AssertionError("unexpected remote discovery")

        def create(self, repository: Doc, effect: Doc) -> Doc:
            raise AssertionError("unexpected remote create")

    monkeypatch.setattr(module, "Request", request, raising=False)
    monkeypatch.setattr(module, "preview", preview, raising=False)
    for name in ("apply", "recover", "show", "supersede"):
        monkeypatch.setattr(module, name, retained(name), raising=False)
    monkeypatch.setattr(module, "as_text", lambda result: f"Dispatch: {result['status']}", raising=False)
    monkeypatch.setattr(storage, "initialize", initialize, raising=False)
    monkeypatch.setitem(sys.modules, "exacly.dispatch", module)
    monkeypatch.setitem(sys.modules, "exacly.dispatch_store", storage)
    monkeypatch.setattr(exacly, "dispatch", module, raising=False)
    monkeypatch.setattr(exacly, "dispatch_store", storage, raising=False)
    monkeypatch.setattr(github_dispatch, "GitHubIssueAdapter", Client)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return calls


def invocation(tmp_path: Path, *, apply: bool = False) -> tuple[list[str], dict[str, Path]]:
    model = tmp_path / "model" / "model.yaml"
    model.parent.mkdir()
    model.write_text("exacly: 0.2.0\nnamespace: demo\n")
    documents: Doc = {
        "plan": {"saved": "plan"},
        "policy": {"reviewed": "readiness"},
        "trust-policy": {"model": {"files": [str(model)]}},
        "dispatch-policy": {"repository": {"full_name": "acme/demo", "id": 123}},
        "target": TARGET,
        "authorization": {"reviewed": "authorization", "dispatch_id": DISPATCH},
        "state": {"actual": "current state"},
    }
    paths = {key: tmp_path / f"{key}.json" for key in documents}
    for key, document in documents.items():
        paths[key].write_text(json.dumps(document))
    paths["model"] = model
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    paths["journal"] = private / "dispatch.sqlite3"
    paths["ledger"] = private / "executions.sqlite3"
    args = ["dispatch", "github-issues", "-m", str(model), "--action", ACTION, "--date", "2026-10-30"]
    for key in ("plan", "policy", "trust-policy", "dispatch-policy", "target", "state"):
        args.extend([f"--{key}", str(paths[key])])
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
def test_default_and_explicit_preview_are_offline_and_preserve_every_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, explicit: bool
) -> None:
    args, paths = invocation(tmp_path)
    before = {path: path.read_bytes() for path in paths.values() if path.is_file()}
    if explicit:
        args.append("--dry-run")
    assert exacly_main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == answer("ready", dry_run=True)
    (documents, state, request), options = backend["preview"][0]
    assert state == paths["state"] and not options
    assert documents == {str(paths["model"]): {"exacly": "0.2.0", "namespace": "demo"}}
    assert request.action_id == ACTION and request.date == dt.date(2026, 10, 30)
    assert request.target.data == TARGET and request.plan == {"saved": "plan"}
    assert not backend["tokens"] and not backend["remote"] and not backend["apply"]
    assert not backend["initialize"] and not paths["journal"].exists() and not paths["ledger"].exists()
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("missing", ["journal", "ledger", "authorization"])
def test_apply_requires_both_stores_and_reviewed_authorization(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    offset = args.index(f"--{missing}")
    del args[offset : offset + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert "--apply requires" in capsys.readouterr().err
    assert not backend["apply"] and not backend["tokens"]


@pytest.mark.parametrize("operation", ["apply", "recover"])
def test_apply_and_explicit_recovery_route_the_same_reviewed_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, operation: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    if operation == "recover":
        args[:2] = ["dispatches", "recover"]
        args.remove("--apply")
    assert exacly_main([*args, "--lock-timeout", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == backend["report"]
    (journal, ledger, documents, state, request), options = backend[operation][0]
    assert (journal, ledger, state) == (paths["journal"], paths["ledger"], paths["state"])
    assert documents[str(paths["model"])]["namespace"] == "demo"
    assert request.readiness_policy == {"reviewed": "readiness"}
    assert request.dispatch_policy == {"repository": {"full_name": "acme/demo", "id": 123}}
    assert options["authorization"] == {"reviewed": "authorization", "dispatch_id": DISPATCH}
    assert options["lock_timeout"] == 0 and options["client"] is not None
    assert not backend["tokens"] and not backend["initialize"]


@pytest.mark.parametrize(
    "status,code",
    [
        ("registered", 0),
        ("superseded", 0),
        ("ambiguous", 1),
        ("conflict", 1),
        ("attempts_exhausted", 1),
        ("blocked", 1),
    ],
)
def test_apply_exit_status_preserves_success_or_required_operator_resolution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, status: str, code: int
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["report"] = answer(status)
    assert exacly_main(args) == code
    assert capsys.readouterr().out == f"Dispatch: {status}\n"


def test_init_and_show_are_local_operations_without_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    journal = tmp_path / "dispatch.sqlite3"
    assert exacly_main(["dispatches", "init", "--journal", str(journal), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"
    assert backend["initialize"] == [((journal,), {})]
    assert exacly_main(["dispatches", "show", "--journal", str(journal), "--dispatch", DISPATCH]) == 0
    assert capsys.readouterr().out == "Dispatch: registered\n"
    assert backend["show"] == [((journal, DISPATCH), {})]
    assert not backend["tokens"] and not backend["remote"]


@pytest.mark.parametrize("status", ["prepared", "creating", "remote_confirmed", "registration_prepared", "ambiguous"])
def test_show_can_inspect_unfinished_history_successfully(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, status: str
) -> None:
    backend["report"] = answer(status)
    assert exacly_main(["dispatches", "show", "--journal", str(tmp_path / "dispatch.db"), "--dispatch", DISPATCH]) == 0
    assert capsys.readouterr().out == f"Dispatch: {status}\n" and not backend["tokens"]


def test_supersession_requires_explicit_authority_and_retains_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    journal = tmp_path / "dispatch.sqlite3"
    args = ["dispatches", "supersede", "--journal", str(journal), "--dispatch", DISPATCH, "--reason", "replanned"]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2 and not backend["supersede"]
    capsys.readouterr()
    backend["report"] = answer("superseded")
    assert exacly_main([*args, "--authorize-supersession"]) == 0
    assert backend["supersede"] == [((journal, DISPATCH), {"reason": "replanned", "authorize": True})]
    assert not backend["tokens"]


@pytest.mark.parametrize("primary", [None, "", " \t", TOKEN])
def test_live_operations_construct_one_client_from_the_first_nonblank_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Doc, primary: str | None
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["live"] = True
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    if primary is not None:
        monkeypatch.setenv("GH_TOKEN", primary)
    assert exacly_main(args) == 0
    assert backend["tokens"] == [TOKEN if primary == TOKEN else "fallback-token"]
    assert backend["remote"] == [{"full_name": "acme/demo", "id": 123}]


def test_missing_live_credentials_are_a_clean_operational_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["live"] = True
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "GH_TOKEN or GITHUB_TOKEN" in captured.err and not captured.out
    assert not backend["tokens"] and not backend["remote"]


@pytest.mark.parametrize("content", ['{"repository":{},"repository":{}}', '{"invalid":NaN}', "[" * 2000, "x" * 65537])
def test_ambiguous_or_unbounded_dispatch_json_is_rejected_before_remote_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, content: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    paths["dispatch-policy"].write_text(content)
    assert exacly_main(args) == 2
    assert capsys.readouterr().err
    assert not backend["apply"] and not backend["tokens"]


@pytest.mark.parametrize("destination", ["journal", "ledger", "state"])
def test_store_or_state_aliases_of_reviewed_inputs_are_rejected_before_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, destination: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    args = replace_arg(args, f"--{destination}", str(paths["plan"]))
    before = paths["plan"].read_bytes()
    assert exacly_main(args) == 2
    assert capsys.readouterr().err
    assert paths["plan"].read_bytes() == before and not backend["apply"] and not backend["tokens"]


def test_credential_values_are_redacted_from_input_and_parser_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    paths["dispatch-policy"].write_text('{"' + TOKEN + '":0,"' + TOKEN + '":1}')
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and "[redacted]" in captured.err
    with pytest.raises(SystemExit):
        exacly_main([*args, "--lock-timeout", TOKEN])
    assert TOKEN not in capsys.readouterr().err
    assert not backend["apply"] and not backend["tokens"]


def test_preview_cannot_be_confused_with_supplied_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, paths = invocation(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, "--authorization", str(paths["authorization"])])
    assert error.value.code == 2 and "--authorization requires --apply" in capsys.readouterr().err
    assert not backend["preview"] and not backend["apply"]


def test_invalid_backend_report_is_an_operational_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["report"] = answer("completed")
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "invalid report" in captured.err and not captured.out


def test_real_cli_preview_reviewed_apply_and_local_replay(
    dispatch_scenario: Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from exacly import github_dispatch

    scenario = dispatch_scenario
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    paths: dict[str, Path] = {}
    artifacts = {
        "model": scenario.source.config,
        "plan": scenario.request.plan,
        "policy": scenario.request.readiness_policy,
        "trust-policy": scenario.request.trust_policy,
        "dispatch-policy": scenario.request.dispatch_policy,
        "target": scenario.request.target.data,
    }
    for name, value in artifacts.items():
        path = reviewed / f"{name}.json"
        path.write_text(json.dumps(value))
        paths[name] = path
    paths["state"] = scenario.state_path
    args = ["dispatch", "github-issues", "-m", str(paths["model"]), "--action", scenario.request.action_id]
    args.extend(["--date", scenario.request.date.isoformat()])
    for name in ("plan", "policy", "trust-policy", "dispatch-policy", "target", "state"):
        args.extend([f"--{name}", str(paths[name])])
    before_state, before_journal = scenario.state_path.read_bytes(), scenario.journal.read_bytes()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert exacly_main([*args, "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "ready" and preview["dry_run"] is True
    assert not scenario.client.calls and not scenario.ledger.exists()
    assert scenario.state_path.read_bytes() == before_state and scenario.journal.read_bytes() == before_journal
    authorization = reviewed / "authorization.json"
    authorization.write_text(json.dumps(preview["authorization_template"]))
    monkeypatch.setattr(github_dispatch, "GitHubIssueAdapter", lambda token: scenario.client)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    apply_args = [
        *args,
        "--apply",
        "--journal", str(scenario.journal),
        "--ledger", str(scenario.ledger),
        "--authorization", str(authorization),
        "--json",
    ]  # fmt: skip
    assert exacly_main(apply_args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "registered" and scenario.client.calls.count("create") == 1
    assert len(scenario.state["pending"]) == 1 and scenario.ledger.is_file()
    remote_calls = list(scenario.client.calls)
    monkeypatch.delenv("GH_TOKEN")
    assert exacly_main(apply_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "registered"
    assert scenario.client.calls == remote_calls
