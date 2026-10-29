"""The isolated invocation CLI preserves explicit policy, snapshots and candidate argv."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from exacly import isolation
from exacly.cli import exacly_main

Doc = dict[str, Any]
DIGEST = "sha256:" + "a" * 64


def report(status: str = "completed") -> Doc:
    return {
        "schema_version": "0.1.0",
        "backend": "linux-bubblewrap-v1",
        "status": status,
        "exit_code": 0 if status == "completed" else 7,
        "isolation": {"verified": True},
    }


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Doc:
    calls: Doc = {"policy": [], "run": [], "answer": report()}
    runtime = object()

    def parse(document: Any) -> object:
        calls["policy"].append(document)
        return runtime

    def run(selected: Any, argv: Any, inputs: Any, out: Any, **options: Any) -> Doc:
        assert selected is runtime
        calls["run"].append((argv, inputs, out, options))
        return dict(calls["answer"])

    monkeypatch.setattr(isolation, "parse_policy", parse)
    monkeypatch.setattr(isolation, "run", run)
    monkeypatch.setattr(isolation, "as_text", lambda answer: f"Candidate status: {answer['status']}")
    return calls


def invocation(tmp_path: Path, *, inputs: bool = True) -> tuple[list[str], Path, Path, Path]:
    policy = tmp_path / "runtime-policy.json"
    snapshot = tmp_path / "inputs.json"
    output = tmp_path / "output"
    policy.write_text(json.dumps({"approved": "explicit policy document"}))
    snapshot.write_text(json.dumps({"schema_version": "0.1.0", "files": {"source.py": "print('こんにちは')\n"}}))
    command = ["isolate", "--runtime-policy", str(policy), "--out", str(output), "--json"]
    if inputs:
        command.extend(["--inputs", str(snapshot)])
    command.extend(["--", "/usr/bin/python3", "/inputs/source.py", "--literal=$(example)"])
    return command, policy, snapshot, output


def test_invocation_forwards_only_explicit_snapshots_policy_and_literal_argv(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str]
) -> None:
    command, policy, snapshots, output = invocation(tmp_path)
    before = policy.read_bytes(), snapshots.read_bytes()
    assert exacly_main(command) == 0
    assert json.loads(capsys.readouterr().out) == report()
    assert backend["policy"] == [{"approved": "explicit policy document"}]
    assert backend["run"] == [
        (
            ["/usr/bin/python3", "/inputs/source.py", "--literal=$(example)"],
            {"source.py": "print('こんにちは')\n".encode()},
            output,
            {},
        )
    ]
    assert (policy.read_bytes(), snapshots.read_bytes()) == before
    assert not output.exists()


def test_omitted_snapshots_and_explicit_timeout_are_forwarded(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str]
) -> None:
    command, _, _, output = invocation(tmp_path, inputs=False)
    command[command.index("--") : command.index("--")] = ["--timeout", "4.5"]
    assert exacly_main(command) == 0
    assert backend["run"][0][1:] == ({}, output, {"timeout": 4.5})
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", ["candidate_failed", "timed_out", "output_limit"])
def test_unsuccessful_candidate_reports_are_retained_as_exit_one(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str], status: str
) -> None:
    backend["answer"] = report(status)
    # Output limiting may be diagnosed after a successful child exit.
    if status == "output_limit":
        backend["answer"]["exit_code"] = 0
    command, _, _, _ = invocation(tmp_path)
    command.remove("--json")
    assert exacly_main(command) == 1
    captured = capsys.readouterr()
    assert captured.out == f"Candidate status: {status}\n" and not captured.err
    assert len(backend["run"]) == 1


@pytest.mark.parametrize("timeout", ["0", "-1", "NaN", "Infinity", "-Infinity", "invalid"])
def test_invalid_timeout_prevents_backend_use(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str], timeout: str
) -> None:
    command, _, _, _ = invocation(tmp_path)
    command.insert(command.index("--"), f"--timeout={timeout}")
    with pytest.raises(SystemExit) as error:
        exacly_main(command)
    assert error.value.code == 2
    assert "finite positive seconds" in capsys.readouterr().err
    assert not backend["policy"] and not backend["run"]


def test_missing_candidate_is_an_error_before_reading_policy_or_launching(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str]
) -> None:
    command, _, _, _ = invocation(tmp_path)
    del command[command.index("--") :]
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert "candidate command after --" in captured.err and not captured.out
    assert not backend["policy"] and not backend["run"]


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"schema_version":"0.2.0","files":{}}',
        '{"schema_version":"0.1.0","files":{},"source":"/host/path"}',
        '{"schema_version":"0.1.0","files":[]}',
        '{"schema_version":"0.1.0","files":{"x":12}}',
        '{"schema_version":"0.1.0","files":{"x":null}}',
        '{"schema_version":"0.1.0","files":{"x":"first","x":"second"}}',
        '{"schema_version":"0.1.0","files":{},"files":{"x":"second"}}',
        '{"schema_version":"0.1.0","files":{"x":NaN}}',
        '{"schema_version":"0.1.0","files":{"x":"\\ud800"}}',
    ],
)
def test_ambiguous_or_invalid_input_documents_prevent_candidate_launch(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    command, _, snapshot, output = invocation(tmp_path)
    snapshot.write_text(payload)
    before = snapshot.read_bytes()
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert "exacly isolate:" in captured.err and "Traceback" not in captured.err and not captured.out
    assert not backend["run"] and not output.exists() and snapshot.read_bytes() == before


@pytest.mark.parametrize("limit", ["MAX_INPUT_BYTES", "MAX_INPUT_FILES"])
def test_snapshot_limits_prevent_candidate_launch(
    tmp_path: Path, backend: Doc, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], limit: str
) -> None:
    command, _, snapshot, output = invocation(tmp_path)
    monkeypatch.setattr(isolation, limit, 1)
    snapshot.write_text(json.dumps({"schema_version": "0.1.0", "files": {"a": "first", "b": "second"}}))
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert "permitted" in captured.err and not captured.out and not backend["run"] and not output.exists()


@pytest.mark.parametrize("target", ["policy", "inputs"])
def test_json_document_reads_are_bounded_before_decode_or_launch(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    command, policy, snapshot, output = invocation(tmp_path)
    selected, limit = (policy, 64 * 1024) if target == "policy" else (snapshot, 16 * 1024 * 1024)
    selected.write_bytes(b" " * (limit + 1))
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert "document size" in captured.err and not captured.out and not backend["run"] and not output.exists()


def test_policy_duplicate_keys_are_rejected_before_backend_parsing(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str]
) -> None:
    command, policy, _, _ = invocation(tmp_path)
    policy.write_text('{"rootfs":"approved","rootfs":"other"}')
    assert exacly_main(command) == 2
    assert "duplicate JSON key" in capsys.readouterr().err
    assert not backend["policy"] and not backend["run"]


@pytest.mark.parametrize("failure", [ValueError("invalid runtime policy"), OSError("runtime unavailable")])
def test_runtime_failures_have_a_clean_exit_and_no_printed_success(
    tmp_path: Path,
    backend: Doc,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Doc:
        raise failure

    monkeypatch.setattr(isolation, "run", fail)
    command, _, _, output = invocation(tmp_path)
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert "exacly isolate:" in captured.err and "Traceback" not in captured.err and not captured.out
    assert not output.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "0.2.0"),
        ("backend", "host"),
        ("status", "ready"),
        ("exit_code", False),
        ("exit_code", 1),
        ("isolation", {"verified": False}),
        ("isolation", {"verified": 1}),
    ],
)
def test_invalid_runtime_reports_are_never_printed_as_success(
    tmp_path: Path, backend: Doc, capsys: pytest.CaptureFixture[str], field: str, value: Any
) -> None:
    backend["answer"][field] = value
    command, _, _, _ = invocation(tmp_path)
    assert exacly_main(command) == 2
    captured = capsys.readouterr()
    assert "invalid report" in captured.err and not captured.out


@pytest.mark.parametrize("as_json", [False, True])
def test_rootfs_inspection_reports_only_content_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    root = tmp_path / "runtime"
    calls: list[Path] = []

    def digest(path: Path) -> str:
        calls.append(path)
        return DIGEST

    monkeypatch.setattr(isolation, "rootfs_digest", digest)
    command = ["isolation", "inspect", "--rootfs", str(root)]
    if as_json:
        command.append("--json")
    assert exacly_main(command) == 0
    captured = capsys.readouterr()
    assert calls == [root] and not captured.err
    if as_json:
        assert json.loads(captured.out) == {
            "schema_version": "0.1.0",
            "operation": "inspect-rootfs",
            "rootfs": str(root.resolve()),
            "digest": DIGEST,
            "scope": "content_identity",
        }
    else:
        assert DIGEST in captured.out and "operator approval is supplied separately" in captured.out
    assert not root.exists()


def test_rootfs_inspection_error_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(path: Path) -> str:
        raise isolation.IsolationError("root filesystem unavailable")

    monkeypatch.setattr(isolation, "rootfs_digest", fail)
    assert exacly_main(["isolation", "inspect", "--rootfs", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert "exacly isolation: root filesystem unavailable" in captured.err and not captured.out


@pytest.mark.parametrize("command", [["--help"], ["isolate", "--help"], ["isolation", "inspect", "--help"]])
def test_help_does_not_import_the_platform_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    monkeypatch.setitem(sys.modules, "exacly.isolation", None)
    with pytest.raises(SystemExit) as error:
        exacly_main(command)
    assert error.value.code == 0
    assert "isola" in capsys.readouterr().out
