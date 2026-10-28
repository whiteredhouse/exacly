"""Explicit registration is offline; bound previews preserve operator inputs and the ledger."""

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import pytest
from exacly.cli import exacly_main

TOKEN = "execution-test-token"
ACTION = "action-1"
BINDING_ID = "sha256:" + "1" * 64
OPENED_AT = "2026-10-28T09:00:00Z"
TARGET = {"context": "main", "model_revision": "a" * 40, "revision": "b" * 40}
TRUST: dict[str, Any] = {"model": {"files": ["models/approved.yaml"]}}
PLAN: dict[str, Any] = {"digest": "saved-plan", "actions": [{"id": ACTION}]}
STATE: dict[str, Any] = {"pending": [{"record": "REQ-1", "task": "issues/9", "opened_at": OPENED_AT}]}
POLICY: dict[str, Any] = {"name": "verification", "version": "1", "requirements": []}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import exacly.executions as executions
    from exacly import github_evidence, reconciliation

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake: dict[str, Any] = {
        "registrations": [],
        "reads": [],
        "tokens": [],
        "previews": [],
        "record": {"schema_version": "0.1.0", "id": BINDING_ID, "contract": {"task": "issues/9"}},
        "report": {"status": "ready", "claims": {"registered_binding_verified": True}},
        "source": object(),
    }

    def register(path: Path, request: Any) -> Any:
        fake["registrations"].append((path, request))
        return fake["record"]

    def get(path: Path, binding_id: str) -> Any:
        fake["reads"].append((path, binding_id))
        return {"binding": fake["record"], "active": True, "superseded_by": None}

    def as_text(value: dict[str, Any]) -> str:
        binding = value.get("binding", value)
        return f"Registered binding: {binding['id']}"

    def client(token: str) -> Any:
        fake["tokens"].append(token)
        return fake["source"]

    def preview(*args: Any, **kwargs: Any) -> Any:
        fake["previews"].append((args, kwargs))
        return fake["report"]

    monkeypatch.setattr(executions, "register", register)
    monkeypatch.setattr(executions, "get", get)
    monkeypatch.setattr(executions, "as_text", as_text)
    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    monkeypatch.setattr(reconciliation, "preview", preview)
    monkeypatch.setattr(reconciliation, "as_text", lambda report: f"Completion preview: {report['status']}")
    return fake


def registration(tmp_path: Path) -> tuple[list[str], dict[str, Path], Path]:
    inputs = {name: tmp_path / f"{name}.json" for name in ("plan", "state", "policy", "trust", "target")}
    for name, value in (("plan", PLAN), ("state", STATE), ("policy", POLICY), ("trust", TRUST), ("target", TARGET)):
        inputs[name].write_text(json.dumps(value))
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    ledger = operator / "executions.sqlite"
    args = [
        "executions", "register", "--ledger", str(ledger), "--plan", str(inputs["plan"]), "--action", ACTION,
        "--state", str(inputs["state"]), "--task", "issues/9", "--opened-at", OPENED_AT,
        "--policy", str(inputs["policy"]), "--trust-policy", str(inputs["trust"]),
        "--target", str(inputs["target"]), "--date", "2026-10-28",
    ]  # fmt: skip
    return args, inputs, ledger


def preview_args(tmp_path: Path) -> tuple[list[str], dict[str, Path], Path]:
    _, inputs, ledger = registration(tmp_path)
    ledger.write_bytes(b"existing private execution ledger")
    ledger.chmod(0o600)
    args = [
        "reconcile", "github-actions", "--dry-run", "--plan", str(inputs["plan"]), "--action", ACTION,
        "--policy", str(inputs["policy"]), "--trust-policy", str(inputs["trust"]), "--target", str(inputs["target"]),
        "--run", "123", "--attempt", "2", "--date", "2026-10-28",
        "--ledger", str(ledger), "--binding", BINDING_ID, "--state", str(inputs["state"]),
    ]  # fmt: skip
    return args, inputs, ledger


def replace_arg(args: list[str], flag: str, value: Path | str) -> None:
    args[args.index(flag) + 1] = str(value)


def test_register_is_explicit_offline_and_forwards_the_exact_operator_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, inputs, ledger = registration(tmp_path)
    before = {path: path.read_bytes() for path in inputs.values()}
    assert exacly_main([*args, "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["record"]
    assert not captured.err
    assert len(backend["registrations"]) == 1
    path, request = backend["registrations"][0]
    assert path == ledger
    assert request.plan == PLAN and request.state == STATE
    assert request.action_id == ACTION
    assert (request.task, request.opened_at) == ("issues/9", OPENED_AT)
    assert request.readiness_policy == POLICY and request.trust_policy == TRUST
    assert request.target.data == TARGET and request.date == dt.date(2026, 10, 28)
    assert request.adopt_verification is False and request.supersedes is None
    assert not backend["tokens"] and not backend["previews"] and not backend["reads"]
    assert all(path.read_bytes() == content for path, content in before.items())


def test_registration_forwards_explicit_legacy_adoption_and_compare_and_swap_supersession(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, _ = registration(tmp_path)
    previous = "sha256:" + "2" * 64
    assert exacly_main([*args, "--adopt-verification", "--supersedes", previous]) == 0
    request = backend["registrations"][0][1]
    assert request.adopt_verification is True and request.supersedes == previous
    assert capsys.readouterr().out == f"Registered binding: {BINDING_ID}\n"


def test_repeated_register_and_show_use_only_the_ledger_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, ledger = registration(tmp_path)
    assert exacly_main([*args, "--json"]) == 0
    first = capsys.readouterr().out
    assert exacly_main([*args, "--json"]) == 0
    assert capsys.readouterr().out == first
    assert exacly_main(["executions", "show", "--ledger", str(ledger), "--binding", BINDING_ID, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {"binding": backend["record"], "active": True, "superseded_by": None}
    assert backend["reads"] == [(ledger, BINDING_ID)]
    assert len(backend["registrations"]) == 2
    assert not backend["tokens"] and not backend["previews"]


@pytest.mark.parametrize(
    "flag",
    [
        "--ledger",
        "--plan",
        "--action",
        "--state",
        "--task",
        "--opened-at",
        "--policy",
        "--trust-policy",
        "--target",
        "--date",
    ],
)
def test_registration_requires_every_explicit_selection(tmp_path: Path, backend: dict[str, Any], flag: str) -> None:
    args, _, _ = registration(tmp_path)
    index = args.index(flag)
    del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not backend["registrations"] and not backend["reads"] and not backend["tokens"]


@pytest.mark.parametrize("flag", ["--receipt", "--binding-file", "--state-from-report", "--apply", "--token"])
def test_registration_accepts_no_imported_receipt_or_implicit_execution_options(
    tmp_path: Path, backend: dict[str, Any], flag: str
) -> None:
    args, _, _ = registration(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, flag, "untrusted.json"])
    assert error.value.code == 2
    assert not backend["registrations"]


@pytest.mark.parametrize("name", ["plan", "state", "policy", "trust", "target"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_ledger_cannot_replace_any_registration_input_or_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    name: str,
    alias: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, inputs, ledger = registration(tmp_path)
    original = inputs[name]
    if alias == "direct":
        ledger = original
    elif alias == "symlink":
        ledger.symlink_to(original)
    else:
        os.link(original, ledger)
    replace_arg(args, "--ledger", ledger)
    before = original.read_bytes()
    assert exacly_main(args) == 2
    assert "cannot overwrite inputs" in capsys.readouterr().err
    assert not backend["registrations"]
    assert original.read_bytes() == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_ledger_auxiliary_paths_cannot_collide_with_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], suffix: str
) -> None:
    args, inputs, ledger = registration(tmp_path)
    colliding = ledger.with_name(ledger.name + suffix)
    colliding.write_bytes(inputs["state"].read_bytes())
    replace_arg(args, "--state", colliding)
    before = colliding.read_bytes()
    assert exacly_main(args) == 2
    assert "cannot overwrite inputs" in capsys.readouterr().err
    assert not backend["registrations"] and colliding.read_bytes() == before


@pytest.mark.parametrize("which", ["missing", "existing", "symlink"])
def test_ledger_stays_outside_declared_model_folders_before_store_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _ = registration(tmp_path)
    model = tmp_path / "models"
    if which != "missing":
        model.mkdir()
    folder = model
    if which == "symlink":
        folder = tmp_path / "model-alias"
        folder.symlink_to(model, target_is_directory=True)
    ledger = folder / "ledger.sqlite"
    replace_arg(args, "--ledger", ledger)
    assert exacly_main(args) == 2
    assert "outside" in capsys.readouterr().err
    assert not backend["registrations"] and not ledger.exists()


@pytest.mark.parametrize("which", ["ledger", "model"])
def test_registration_symlink_loops_are_clean_input_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, ledger = registration(tmp_path)
    loop = ledger if which == "ledger" else tmp_path / "models"
    loop.symlink_to(loop.name, target_is_directory=which == "model")
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "cannot be resolved" in captured.err and "Traceback" not in captured.err
    assert not backend["registrations"] and not backend["tokens"]


def test_store_conflict_is_reported_without_touching_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
) -> None:
    import exacly.executions as executions

    args, inputs, ledger = registration(tmp_path)
    ledger.write_bytes(b"original ledger")
    before = {path: path.read_bytes() for path in [*inputs.values(), ledger]}

    def conflict(path: Path, request: Any) -> Any:
        raise ValueError("binding conflicts with the current active approval")

    monkeypatch.setattr(executions, "register", conflict)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "conflicts" in captured.err and not captured.out
    assert all(path.read_bytes() == content for path, content in before.items())


def test_show_missing_binding_is_an_error_and_never_initializes_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
) -> None:
    import exacly.executions as executions

    ledger = tmp_path / "missing.sqlite"

    def missing(path: Path, binding_id: str) -> Any:
        raise ValueError("execution ledger does not exist")

    monkeypatch.setattr(executions, "get", missing)
    assert exacly_main(["executions", "show", "--ledger", str(ledger), "--binding", BINDING_ID]) == 2
    captured = capsys.readouterr()
    assert "does not exist" in captured.err and not captured.out
    assert not ledger.exists() and not backend["registrations"] and not backend["tokens"]


def test_bound_preview_forwards_binding_inputs_without_changing_ledger_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
) -> None:
    args, inputs, ledger = preview_args(tmp_path)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    before = {path: path.read_bytes() for path in [*inputs.values(), ledger]}
    output = tmp_path / "preview.json"
    assert exacly_main([*args, "--json", "--out", str(output)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend["report"] and output.read_text() == captured.out
    positional, selected = backend["previews"][0]
    assert positional[0] is backend["source"]
    assert selected == {"ledger": ledger, "binding_id": BINDING_ID, "state_doc": STATE}
    assert not backend["registrations"] and not backend["reads"]
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    "retained",
    [
        ("--ledger",),
        ("--binding",),
        ("--state",),
        ("--ledger", "--binding"),
        ("--ledger", "--state"),
        ("--binding", "--state"),
    ],
)
def test_registered_preview_options_are_all_or_none_before_credentials_or_network(
    tmp_path: Path, backend: dict[str, Any], retained: tuple[str, ...]
) -> None:
    args, _, _ = preview_args(tmp_path)
    for flag in ("--ledger", "--binding", "--state"):
        if flag not in retained:
            index = args.index(flag)
            del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["previews"] and not backend["registrations"]


@pytest.mark.parametrize("which", ["ledger", "state", "journal", "wal", "shm", "ledger_symlink", "ledger_hardlink"])
def test_bound_preview_output_cannot_replace_registered_storage_or_current_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    args, inputs, ledger = preview_args(tmp_path)
    if which == "ledger":
        output = ledger
    elif which == "state":
        output = inputs["state"]
    elif which in {"journal", "wal", "shm"}:
        output = ledger.with_name(ledger.name + "-" + which)
        output.write_bytes(b"protected auxiliary file")
    else:
        output = tmp_path / "ledger-alias"
        if which == "ledger_symlink":
            output.symlink_to(ledger)
        else:
            os.link(ledger, output)
    before = {path: path.read_bytes() for path in [*inputs.values(), ledger, output]}
    assert exacly_main([*args, "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "overwrite" in captured.err and not captured.out
    assert not backend["registrations"]
    assert all(path.read_bytes() == content for path, content in before.items())


def test_bound_preview_still_requires_live_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, ledger = preview_args(tmp_path)
    before = ledger.read_bytes()
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "GH_TOKEN or GITHUB_TOKEN" in captured.err and not captured.out
    assert not backend["previews"] and not backend["registrations"]
    assert ledger.read_bytes() == before


def test_real_registration_show_and_bound_preview_preserve_state_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exacly import github_evidence
    from exacly.refresh import refresh
    from test_admission import ATTEMPT, READINESS, RUN_ID, Source
    from test_applicability import WHEN
    from test_executions import OPENED, TASK, run
    from test_reconciliation import make_plan

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    args, inputs, ledger = registration(tmp_path)
    source = Source()
    plan = make_plan(source)
    state = refresh({source.model_file: source.config}, {}, run(source))
    state["pending"] = [{"record": "REQ-1", "task": TASK, "opened_at": OPENED, "dimension": "verification"}]
    for name, value in (("plan", plan), ("state", state), ("policy", READINESS), ("trust", source.policy)):
        inputs[name].write_text(json.dumps(value))
    replace_arg(args, "--action", plan["actions"][0]["id"])
    replace_arg(args, "--task", TASK)
    replace_arg(args, "--opened-at", OPENED)
    replace_arg(args, "--date", WHEN.isoformat())
    before = {path: path.read_bytes() for path in inputs.values()}
    assert exacly_main([*args, "--json"]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["claims"]["operator_registered"] is True
    assert not source.calls and not source.downloads
    assert exacly_main(["executions", "show", "--ledger", str(ledger), "--binding", registered["id"], "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["binding"] == registered and shown["active"] is True
    ledger_before = ledger.read_bytes()
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setattr(github_evidence, "GitHubClient", lambda token: source)
    preview = [
        "reconcile", "github-actions", "--dry-run", "--plan", str(inputs["plan"]),
        "--action", plan["actions"][0]["id"], "--policy", str(inputs["policy"]),
        "--trust-policy", str(inputs["trust"]), "--target", str(inputs["target"]),
        "--ledger", str(ledger), "--binding", registered["id"], "--state", str(inputs["state"]),
        "--run", str(RUN_ID), "--attempt", str(ATTEMPT), "--date", WHEN.isoformat(), "--json",
    ]  # fmt: skip
    assert exacly_main(preview) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready" and report["claims"]["registered_binding_verified"] is True
    assert source.calls and source.downloads
    assert ledger.read_bytes() == ledger_before
    assert all(path.read_bytes() == value for path, value in before.items())
