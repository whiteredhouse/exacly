"""Live admission requires credentials and publishes complete bundles without replacing inputs."""

import datetime as dt
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from exacly.cli import exacly_main, write_admission

TOKEN = "test-github-token-value"
TARGET = {"context": "main", "model_revision": "a" * 40, "revision": "b" * 40}
TRUST_POLICY: dict[str, Any] = {"model": {"files": ["models/exacly.yaml"]}}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from exacly import admission, github_evidence

    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake: dict[str, Any] = {
        "source": object(),
        "tokens": [],
        "calls": [],
        "result": admission.AdmissionResult(
            receipt={"status": "admitted", "checks": ["verified"], "reasons": []},
            archive=b"retained artifact archive",
            report=b"<testsuite/>",
            state={"observed": "trusted evidence"},
        ),
    }

    def client(token: str) -> Any:
        fake["tokens"].append(token)
        return fake["source"]

    def admit(*args: Any) -> Any:
        fake["calls"].append(args)
        return fake["result"]

    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    monkeypatch.setattr(admission, "admit", admit)
    monkeypatch.setattr(admission, "as_text", lambda receipt: f"Admission: {receipt['status']}")
    return fake


def invocation(tmp_path: Path) -> tuple[list[str], Path, Path, Path]:
    policy, target = tmp_path / "trust.json", tmp_path / "target.json"
    policy.write_text(json.dumps(TRUST_POLICY))
    target.write_text(json.dumps(TARGET))
    output = tmp_path / "reports" / "admission"
    args = [
        "admit", "github-actions", "--trust-policy", str(policy), "--target", str(target),
        "--run", "123", "--attempt", "2", "--date", "2026-10-28", "--out", str(output),
    ]  # fmt: skip
    return args, policy, target, output


def test_admitted_bundle_retains_reports_state_and_same_receipt_as_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, policy, target, output = invocation(tmp_path)
    before = {path: path.read_bytes() for path in (policy, target)}
    assert exacly_main([*args, "--json"]) == 0
    printed = capsys.readouterr()
    assert not printed.err
    assert output.joinpath("receipt.json").read_text() == printed.out
    assert json.loads(printed.out) == backend["result"].receipt
    assert {path.name for path in output.iterdir()} == {"receipt.json", "archive.zip", "junit.xml", "state.json"}
    assert output.joinpath("archive.zip").read_bytes() == backend["result"].archive
    assert output.joinpath("junit.xml").read_bytes() == backend["result"].report
    assert json.loads(output.joinpath("state.json").read_text()) == backend["result"].state
    assert backend["tokens"] == [TOKEN]
    source, selected_policy, selected_target, run, attempt, date = backend["calls"][0]
    assert source is backend["source"]
    assert selected_policy == TRUST_POLICY
    assert selected_target.data == TARGET
    assert (run, attempt, date) == (123, 2, dt.date(2026, 10, 28))
    assert all(path.read_bytes() == content for path, content in before.items())
    assert TOKEN not in printed.out
    assert TOKEN not in output.joinpath("receipt.json").read_text()


def test_rejection_saves_only_receipt_and_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _, output = invocation(tmp_path)
    backend["result"].receipt.update(status="rejected", reasons=["Workflow producer is not authorized"])
    assert exacly_main(args) == 1
    shown = capsys.readouterr()
    assert shown.out == "Admission: rejected\n"
    assert not shown.err
    assert {path.name for path in output.iterdir()} == {"receipt.json"}
    assert json.loads(output.joinpath("receipt.json").read_text()) == backend["result"].receipt


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o777])
@pytest.mark.parametrize("status", ["admitted", "rejected"])
def test_admission_bundle_files_are_private_from_creation_regardless_of_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: dict[str, Any],
    umask: int,
    status: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _, output = invocation(tmp_path)
    output.parent.mkdir()
    backend["result"].receipt["status"] = status
    expected = {"receipt.json", "archive.zip", "junit.xml", "state.json"} if status == "admitted" else {"receipt.json"}
    created: set[str] = set()
    original_open = os.open

    def check_creation(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        fd = original_open(path, flags, mode, **kwargs)
        if isinstance(path, Path) and path.name in expected:
            info = os.fstat(fd)
            assert flags & os.O_EXCL
            assert mode == 0o600
            assert not stat.S_IMODE(info.st_mode) & 0o077
            assert info.st_size == 0
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            created.add(path.name)
        return fd

    monkeypatch.setattr(os, "open", check_creation)
    previous_umask = os.umask(umask)
    try:
        assert exacly_main(args) == (0 if status == "admitted" else 1)
    finally:
        os.umask(previous_umask)
    assert created == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert {path.name for path in output.iterdir()} == expected
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())


@pytest.mark.parametrize("opt_out", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_live_admission_cli_retains_the_explicit_branch_protection_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opt_out: bool,
    json_output: bool,
) -> None:
    from exacly import github_evidence
    from test_admission import ATTEMPT, BASE, RUN_ID, Source
    from test_applicability import WHEN

    source = Source()
    source.pages[f"{BASE}/branches/main"]["protected"] = False
    if opt_out:
        source.policy["branch_protection"] = "not_required"
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.chdir(tmp_path)

    def client(token: str) -> Source:
        assert token == TOKEN
        return source

    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    args, policy, _, output = invocation(tmp_path)
    policy.write_text(json.dumps(source.policy))
    for field, value in (("--run", str(RUN_ID)), ("--attempt", str(ATTEMPT)), ("--date", WHEN.isoformat())):
        args[args.index(field) + 1] = value
    if json_output:
        args.append("--json")
    assert exacly_main(args) == (0 if opt_out else 1)
    printed = capsys.readouterr()
    assert not printed.err
    receipt = json.loads(output.joinpath("receipt.json").read_text())
    assert receipt["schema_version"] == "0.2.0"
    assert receipt["claims"]["branch_protection_verified"] is False
    assert receipt["status"] == ("admitted" if opt_out else "rejected")
    if json_output:
        assert json.loads(printed.out) == receipt
    else:
        assert "Branch protection: not verified" in printed.out
    if opt_out:
        assert receipt["origin"]["branch_protection"] == "not_required"
        assert receipt["origin"]["branch_protection_checked_at"] is None
        assert {path.name for path in output.iterdir()} == {"receipt.json", "archive.zip", "junit.xml", "state.json"}
    else:
        assert {path.name for path in output.iterdir()} == {"receipt.json"}
        assert source.downloads == []


@pytest.mark.parametrize("primary", [None, "", " \t"])
def test_github_token_is_used_when_primary_token_is_missing_or_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any], primary: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _, _ = invocation(tmp_path)
    if primary is None:
        monkeypatch.delenv("GH_TOKEN")
    else:
        monkeypatch.setenv("GH_TOKEN", primary)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    assert exacly_main(args) == 0
    assert backend["tokens"] == ["fallback-token"]


def test_primary_token_takes_precedence_without_being_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _, _ = invocation(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    assert exacly_main(args) == 0
    captured = capsys.readouterr()
    assert backend["tokens"] == [TOKEN]
    assert TOKEN not in captured.out + captured.err
    assert "fallback-token" not in captured.out + captured.err


def test_missing_credentials_return_two_without_network_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    args, _, _, output = invocation(tmp_path)
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "GH_TOKEN or GITHUB_TOKEN" in captured.err
    assert not captured.out
    assert not backend["calls"] and not backend["tokens"]
    assert not output.parent.exists()


def test_invalid_client_credentials_are_a_safe_input_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    from exacly import github_evidence

    args, _, _, output = invocation(tmp_path)

    def invalid_client(token: str) -> Any:
        raise github_evidence.GitHubError(f"invalid credential: {token}")

    monkeypatch.setattr(github_evidence, "GitHubClient", invalid_client)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "credentials are invalid" in captured.err
    assert TOKEN not in captured.err
    assert not captured.out and not backend["calls"]
    assert not output.parent.exists()


def test_core_input_error_is_redacted_and_cannot_create_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    from exacly import admission

    args, _, _, output = invocation(tmp_path)

    def invalid_policy(*args: Any) -> Any:
        raise ValueError(f"invalid policy contains {TOKEN}")

    monkeypatch.setattr(admission, "admit", invalid_policy)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "invalid policy" in captured.err
    assert TOKEN not in captured.err
    assert not captured.out
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("--run", "0"),
        ("--run", "-1"),
        ("--run", "x"),
        ("--attempt", "0"),
        ("--attempt", "-2"),
        ("--attempt", "x"),
        ("--date", "2026-99-99"),
    ],
)
def test_invalid_run_attempt_or_date_stops_before_source_access(
    tmp_path: Path, backend: dict[str, Any], field: str, value: str
) -> None:
    args, _, _, output = invocation(tmp_path)
    args[args.index(field) + 1] = value
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["calls"]
    assert not output.parent.exists()


@pytest.mark.parametrize("field", ["--trust-policy", "--target", "--run", "--attempt", "--date", "--out"])
def test_required_inputs_cannot_be_omitted(tmp_path: Path, backend: dict[str, Any], field: str) -> None:
    args, _, _, output = invocation(tmp_path)
    index = args.index(field)
    del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["calls"]
    assert not output.parent.exists()


@pytest.mark.parametrize("flag", ["--metadata", "--receipt", "--archive", "--token"])
def test_offline_metadata_and_cli_token_inputs_are_not_supported(
    tmp_path: Path, backend: dict[str, Any], flag: str
) -> None:
    args, _, _, output = invocation(tmp_path)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, flag, "unsupported-input"])
    assert error.value.code == 2
    assert not backend["tokens"] and not backend["calls"]
    assert not output.parent.exists()


@pytest.mark.parametrize("kind", ["file", "empty_directory", "nonempty_directory", "symlink", "dangling_symlink"])
def test_existing_output_paths_are_preserved_without_source_access(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], kind: str
) -> None:
    args, _, _, output = invocation(tmp_path)
    output.parent.mkdir()
    if kind == "file":
        output.write_bytes(b"original file")
    elif kind in {"empty_directory", "nonempty_directory"}:
        output.mkdir()
        if kind == "nonempty_directory":
            output.joinpath("original").write_bytes(b"keep me")
    else:
        target = tmp_path / "existing"
        if kind == "symlink":
            target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert exacly_main(args) == 2
    assert "new directory" in capsys.readouterr().err
    assert not backend["tokens"] and not backend["calls"]
    assert all(path.read_bytes() == content for path, content in before.items())
    if kind in {"symlink", "dangling_symlink"}:
        assert output.is_symlink()
    else:
        assert output.exists()
    assert not output.is_dir() or not output.joinpath("receipt.json").exists()


@pytest.mark.parametrize("which", ["policy", "target"])
def test_outputs_cannot_replace_local_policy_or_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], which: str
) -> None:
    args, policy, target, _ = invocation(tmp_path)
    output = policy if which == "policy" else target
    before = output.read_bytes()
    args[args.index("--out") + 1] = str(output)
    assert exacly_main(args) == 2
    assert "new directory" in capsys.readouterr().err
    assert output.read_bytes() == before


@pytest.mark.parametrize("which", ["policy_model", "symlink_parent"])
def test_output_cannot_contaminate_model_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    which: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _, _ = invocation(tmp_path)
    if which == "policy_model":
        output = tmp_path / "models" / "admission"
    else:
        model = tmp_path / "models"
        model.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(model, target_is_directory=True)
        output = alias / "admission"
    args[args.index("--out") + 1] = str(output)
    assert exacly_main(args) == 2
    assert "outside model folders" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("folder", ["spec/exacly", "models"])
def test_model_folder_symlink_loop_is_a_clean_output_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: dict[str, Any],
    folder: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    args, trust_policy, _, output = invocation(tmp_path)
    trust_policy.write_text(json.dumps({"model": {"files": [f"{folder}/model.yaml"]}}))
    loop = tmp_path / folder
    loop.parent.mkdir(parents=True, exist_ok=True)
    loop.symlink_to(loop.name, target_is_directory=True)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "cannot be resolved" in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out
    assert loop.is_symlink()
    assert not output.parent.exists()


def test_output_placement_uses_only_declared_model_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, _, _, _ = invocation(tmp_path)
    output = tmp_path / "spec" / "exacly" / "admission"
    args[args.index("--out") + 1] = str(output)
    assert exacly_main(args) == 0
    assert (output / "receipt.json").is_file()


@pytest.mark.parametrize("which", ["policy", "target"])
def test_unreadable_or_invalid_local_inputs_create_no_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: dict[str, Any], which: str
) -> None:
    args, policy, target, output = invocation(tmp_path)
    if which == "policy":
        policy.write_text("[invalid")
    else:
        target.write_text("{}")
    assert exacly_main(args) == 2
    assert "exacly admit:" in capsys.readouterr().err
    assert not backend["tokens"] and not backend["calls"]
    assert not output.parent.exists()


def test_incomplete_admitted_result_cannot_publish_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    from exacly import admission

    monkeypatch.chdir(tmp_path)
    args, _, _, output = invocation(tmp_path)
    backend["result"] = admission.AdmissionResult(
        receipt={"status": "admitted"}, archive=None, report=b"report", state={}
    )
    assert exacly_main(args) == 2
    assert "archive, report and state" in capsys.readouterr().err
    assert not output.parent.exists()


def test_partial_staging_failure_cleans_up_without_publishing_or_exposing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    args, policy, target, output = invocation(tmp_path)
    before = {path: path.read_bytes() for path in (policy, target)}
    original = os.open

    def fail_archive(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        if isinstance(path, Path) and path.name == "archive.zip":
            raise OSError(f"cannot save archive {TOKEN}")
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", fail_archive)
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert "cannot save archive" in captured.err
    assert TOKEN not in captured.err
    assert not captured.out
    assert not output.exists()
    assert not list(output.parent.iterdir())
    assert all(path.read_bytes() == content for path, content in before.items())


def test_a_directory_created_while_staging_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    _, policy, target, output = invocation(tmp_path)
    original = os.open

    def create_destination(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        result = original(path, flags, mode, **kwargs)
        if isinstance(path, Path) and path.name == "junit.xml":
            output.mkdir()
        return result

    monkeypatch.setattr(os, "open", create_destination)
    with pytest.raises(ValueError, match="new directory"):
        write_admission(output, [policy, target], TRUST_POLICY, backend["result"])
    assert output.is_dir()
    assert not list(output.iterdir())
    assert list(output.parent.iterdir()) == [output]
