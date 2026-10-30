"""Real CLI composition validates every input offline and retains complete or blocked diagnostics."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from exacly import composition_io, evidence_retention, github_evidence
from exacly.cli import exacly_main
from test_admission import ATTEMPT, BASE, MODEL_FILE, READINESS, RUN_ID, Source
from test_applicability import WHEN, A, B

TOKEN = "composition-fixture-token"
IDENTIFIERS = ["darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64"]


class SequenceSource:
    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        self.index = -1
        self.calls = 0

    def get_json(self, path: str) -> dict[str, Any]:
        self.calls += 1
        if path.strip("/") == BASE:
            self.index += 1
        return self.sources[self.index].get_json(path)

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        return self.sources[self.index].download_artifact(repository, artifact_id)


def scenario(tmp_path: Path, sources: list[Source] | None = None) -> tuple[list[str], Path, Path, SequenceSource]:
    sources = [Source() for _ in IDENTIFIERS] if sources is None else sources
    config = tmp_path / "controlled"
    config.mkdir()
    model_file = tmp_path / "checkout" / MODEL_FILE
    model_file.parent.mkdir(parents=True)
    model_file.write_text(json.dumps(sources[0].config))
    members = []
    for identifier, source in zip(IDENTIFIERS, sources, strict=True):
        source.policy["name"] = f"approved-{identifier}"
        (config / f"{identifier}-trust.json").write_text(json.dumps(source.policy))
        (config / f"{identifier}-ready.json").write_text(json.dumps(READINESS))
        members.append(
            {
                "id": identifier,
                "trust_policy": f"{identifier}-trust.json",
                "readiness_policy": f"{identifier}-ready.json",
                "model_root": "../checkout",
            }
        )
    definition = config / "definition.json"
    definition.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "name": "native-release",
                "version": "1",
                "members": members,
                "constraints": [{"kind": "same_revision", "members": IDENTIFIERS}],
            }
        )
    )
    loaded = composition_io.load_definition(definition, WHEN)
    selection = config / "selection.json"
    selection.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "definition_digest": loaded.prepared.digest,
                "members": [
                    {
                        "id": identifier,
                        "run": RUN_ID,
                        "attempt": ATTEMPT,
                        "target": {"context": "native-ci", "model_revision": A, "revision": B},
                    }
                    for identifier in IDENTIFIERS
                ],
            }
        )
    )
    out = tmp_path / "reports" / "composition"
    args = [
        "compose", "github-actions", "--definition", str(definition), "--selection", str(selection),
        "--date", WHEN.isoformat(), "--out", str(out), "--json",
    ]  # fmt: skip
    return args, definition, out, SequenceSource(sources)


def backend(monkeypatch: pytest.MonkeyPatch, source: SequenceSource) -> list[str]:
    tokens: list[str] = []

    def client(token: str) -> SequenceSource:
        tokens.append(token)
        return source

    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    return tokens


def test_definition_validation_is_offline_and_resolves_paths_relative_to_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, definition, output, source = scenario(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path.parent)

    def forbidden(token: str) -> Any:
        pytest.fail("offline definition validation created a GitHub client")

    monkeypatch.setattr(github_evidence, "GitHubClient", forbidden)
    assert (
        exacly_main(["composition", "validate", "--definition", str(definition), "--date", WHEN.isoformat(), "--json"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["definition_digest"].startswith("sha256:")
    assert {member["id"] for member in report["members"]} == set(IDENTIFIERS)
    assert source.calls == 0 and not output.exists()


def test_all_members_use_separate_admitted_states_and_retain_exact_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, output, source = scenario(tmp_path)
    tokens = backend(monkeypatch, source)
    assert exacly_main(args) == 0
    printed = capsys.readouterr()
    report = json.loads(printed.out)
    assert report == json.loads((output / "report.json").read_text())
    assert report["complete"] is True and report["result"] == "satisfied"
    assert len(report["members"]) == 4 and source.index == 3 and tokens == [TOKEN]
    for member, native in zip(report["members"], source.sources, strict=True):
        folder = output / "members" / member["id"]
        assert {p.name for p in folder.iterdir()} == {
            "receipt.json",
            "archive.zip",
            "junit.xml",
            "state.json",
            "gate.json",
        }
        assert (folder / "archive.zip").read_bytes() == native.archive
        assert (folder / "junit.xml").read_bytes() == native.report
        assert json.loads((folder / "receipt.json").read_text()) == member["admission"]
        assert json.loads((folder / "state.json").read_text())["namespace"] == native.config["model"]["namespace"]
        assert member["gate"]["result"] == "satisfied"
    assert TOKEN not in printed.out and not printed.err


@pytest.mark.parametrize("outcome", ["failure", "skipped", "rejected"])
def test_one_blocked_member_keeps_other_successes_without_aggregate_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], outcome: str
) -> None:
    second = Source("failure") if outcome == "failure" else Source()
    if outcome == "skipped":
        second = Source(
            report=(
                b'<testsuite><testcase file="python/tests/test_requirement.py" '
                b'name="test_requirement"><skipped/></testcase></testsuite>'
            )
        )
    if outcome == "rejected":
        second.run["head_sha"] = "f" * 40
    args, _, output, source = scenario(tmp_path, [Source(), second, Source(), Source()])
    backend(monkeypatch, source)
    assert exacly_main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is True and report["result"] == "blocked"
    assert [m["status"] for m in report["members"]] == ["satisfied", "blocked", "satisfied", "satisfied"]
    rejected_folder = output / "members" / IDENTIFIERS[1]
    if outcome == "rejected":
        assert {p.name for p in rejected_folder.iterdir()} == {"receipt.json"}
    else:
        assert report["members"][1]["admission"]["status"] == "admitted"
        assert report["members"][1]["gate"]["result"] == ("not_satisfied" if outcome == "failure" else "unknown")


@pytest.mark.parametrize(
    "mutation",
    ["omitted", "duplicate", "unknown", "wrong-producer", "wrong-model", "wrong-gate", "revision", "attempt", "scope"],
)
def test_invalid_whole_selection_fails_before_credentials_network_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mutation: str
) -> None:
    args, definition, output, source = scenario(tmp_path)
    selected_file = definition.parent / "selection.json"
    selected = json.loads(selected_file.read_text())
    if mutation == "omitted":
        selected["members"].pop()
    elif mutation == "duplicate":
        selected["members"][-1] = copy.deepcopy(selected["members"][0])
    elif mutation == "unknown":
        selected["members"][-1]["id"] = "unapproved"
    elif mutation == "wrong-producer":
        trust = definition.parent / f"{IDENTIFIERS[0]}-trust.json"
        trust.write_bytes((definition.parent / f"{IDENTIFIERS[1]}-trust.json").read_bytes())
    elif mutation == "wrong-model":
        model = tmp_path / "checkout" / MODEL_FILE
        changed = json.loads(model.read_text())
        changed["records"][0]["name"] = "Altered approved model"
        model.write_text(json.dumps(changed))
    elif mutation == "wrong-gate":
        readiness = definition.parent / f"{IDENTIFIERS[0]}-ready.json"
        changed = json.loads(readiness.read_text())
        changed["requirements"][0]["record"] = "REQ-unknown"
        readiness.write_text(json.dumps(changed))
    elif mutation == "revision":
        selected["members"][-1]["target"]["revision"] = "f" * 40
    elif mutation == "attempt":
        selected["members"][-1]["attempt"] = True
    else:
        selected["members"][-1]["target"]["platform"] = "linux/arm64"
    selected_file.write_text(json.dumps(selected))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def forbidden(token: str) -> Any:
        pytest.fail("invalid composition created a source client")

    monkeypatch.setattr(github_evidence, "GitHubClient", forbidden)
    assert exacly_main(args) == 2
    assert "GH_TOKEN" not in capsys.readouterr().err
    assert source.calls == 0 and not output.exists()


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077, 0o777])
def test_all_created_bundle_directories_and_files_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mask: int
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    previous = os.umask(mask)
    try:
        assert exacly_main(args) == 0
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    for path in [output, *output.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize("location", ["model", "existing", "symlink"])
def test_output_aliases_are_rejected_before_live_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    if location == "model":
        output = tmp_path / "checkout" / MODEL_FILE
        output = output.parent / "new-evidence"
    elif location == "existing":
        output.mkdir(parents=True)
    else:
        output.parent.mkdir()
        output.symlink_to(tmp_path / "checkout", target_is_directory=True)
    args[args.index("--out") + 1] = str(output)
    assert exacly_main(args) == 2
    assert source.calls == 0


def test_member_output_failure_retains_prior_success_but_incomplete_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = evidence_retention.write_private
    completed = [0]

    def fail_second_gate(path: Path, data: bytes) -> None:
        if path.name == "gate.json":
            completed[0] += 1
            if completed[0] == 2:
                raise OSError("simulated retained gate failure")
        original(path, data)

    monkeypatch.setattr(evidence_retention, "write_private", fail_second_gate)
    assert exacly_main(args) == 2
    aggregate = json.loads((output / "report.json").read_text())
    assert aggregate["complete"] is False and aggregate["result"] == "blocked"
    assert (output / "members" / IDENTIFIERS[0] / "gate.json").is_file()
    assert source.index == 1


def test_final_aggregate_publish_failure_cannot_leave_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = composition_io._replace_report

    def refuse_final(out: Path, report: dict[str, Any]) -> None:
        if report["complete"]:
            raise OSError("simulated final report publication failure")
        original(out, report)

    monkeypatch.setattr(composition_io, "_replace_report", refuse_final)
    assert exacly_main(args) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["complete"] is False and report["result"] == "blocked"
    assert len(report["members"]) == 4
    assert source.index == 3


def test_interrupted_later_member_keeps_first_evidence_and_incomplete_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = source.get_json

    def interrupted(path: str) -> dict[str, Any]:
        if path.strip("/") == BASE and source.index == 0:
            raise KeyboardInterrupt
        return original(path)

    monkeypatch.setattr(source, "get_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        exacly_main(args)
    report = json.loads((output / "report.json").read_text())
    assert report["complete"] is False and report["result"] == "blocked"
    assert [member["id"] for member in report["members"]] == [IDENTIFIERS[0]]
    assert (output / "members" / IDENTIFIERS[0] / "state.json").is_file()


def test_transport_error_tokens_are_redacted_in_retained_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    original = source.get_json

    def unavailable(path: str) -> dict[str, Any]:
        if path.strip("/") == BASE:
            source.index += 1
            raise github_evidence.GitHubError(f"unavailable: {TOKEN}")
        return original(path)

    monkeypatch.setattr(source, "get_json", unavailable)
    assert exacly_main(args) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    for path in output.rglob("*.json"):
        assert TOKEN not in path.read_text()
    assert "[redacted]" in (output / "report.json").read_text()


@pytest.mark.parametrize("operation", ["compose", "admit"])
def test_missing_private_filesystem_primitive_is_rejected_before_credentials_or_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    args, definition, output, source = scenario(tmp_path)
    if operation == "admit":
        args = [
            "admit", "github-actions", "--trust-policy", str(definition.parent / f"{IDENTIFIERS[0]}-trust.json"),
            "--target", str(definition.parent / "target.json"), "--run", str(RUN_ID), "--attempt", "1",
            "--date", WHEN.isoformat(), "--out", str(output),
        ]  # fmt: skip
    monkeypatch.delattr(os, "O_NOFOLLOW")
    assert exacly_main(args) == 2
    assert "supported local POSIX" in capsys.readouterr().err
    assert source.calls == 0 and not output.exists()


def test_repeat_requires_new_destination_and_freshly_observes_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, output, source = scenario(tmp_path)
    backend(monkeypatch, source)
    assert exacly_main(args) == 0
    capsys.readouterr()
    before = (output / "report.json").read_bytes()
    assert exacly_main(args) == 2
    capsys.readouterr()
    assert source.index == 3
    fresh_sources = [Source("failure"), Source(), Source(), Source()]
    fresh = SequenceSource(fresh_sources)
    backend(monkeypatch, fresh)
    args[args.index("--out") + 1] = str(output.parent / "retry")
    assert exacly_main(args) == 1
    assert fresh.index == 3 and all(s.calls and s.downloads for s in fresh.sources)
    assert (output / "report.json").read_bytes() == before


@pytest.mark.parametrize(
    "text",
    ['{"schema_version":"0.1.0","schema_version":"0.1.0"}', "root: &a [*a]", "root: .nan", "[" * 120 + "]" * 120],
)
def test_strict_documents_fail_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
    definition = tmp_path / "bad.yaml"
    definition.write_text(text)
    assert exacly_main(["composition", "validate", "--definition", str(definition), "--date", WHEN.isoformat()]) == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("role", ["definition", "selection", "trust"])
def test_fifo_inputs_fail_without_blocking_or_fetching(tmp_path: Path, role: str) -> None:
    args, definition, output, _ = scenario(tmp_path)
    selected = {
        "definition": definition,
        "selection": definition.parent / "selection.json",
        "trust": definition.parent / f"{IDENTIFIERS[0]}-trust.json",
    }[role]
    selected.unlink()
    os.mkfifo(selected)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from exacly.cli import exacly_main; sys.exit(exacly_main(sys.argv[1:]))",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert done.returncode == 2 and "regular files" in done.stderr
    assert not output.exists()


def test_excessively_deep_output_is_rejected_before_source_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, _, source = scenario(tmp_path)
    deep = tmp_path.joinpath(*(["d"] * 1100), "out")
    args[args.index("--out") + 1] = str(deep)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert exacly_main(args) == 2
    shown = capsys.readouterr()
    assert "missing-parent depth" in shown.err and "Traceback" not in shown.err
    assert source.calls == 0 and not (tmp_path / "d").exists()


def test_total_input_limit_uses_actual_bytes_even_when_stat_reports_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _, output, source = scenario(tmp_path)
    original = Path.stat

    def empty_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        values = list(original(path, follow_symlinks=follow_symlinks))
        values[6] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", empty_stat)
    monkeypatch.setattr(composition_io, "MAX_INPUT_BYTES", 16)
    assert exacly_main(args) == 2
    assert "total input size limit" in capsys.readouterr().err
    assert source.calls == 0 and not output.exists()
