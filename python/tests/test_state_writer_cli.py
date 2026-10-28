"""Observation commands publish conditionally and preserve another observer's committed state."""

import contextlib
import copy
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import cli, manual_qa, state_writer
from exacly.refresh import Run, refresh
from test_manual_qa import REVISION, TARGET, documents, manual_requirement, observation
from test_refresh import CI, WHEN, valid_state


@dataclass
class Scenario:
    state: Path
    initial: dict[str, Any]
    refresh_args: list[str]
    observe_args: list[str]

    def args(self, command: str) -> list[str]:
        return self.refresh_args if command == "refresh" else self.observe_args


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    (tmp_path / "qa" / "observations").mkdir(parents=True)
    for platform in ("ios", "android"):
        observation(tmp_path, platform, platform, {1: "passed", 2: "passed", 3: "passed"})
    config = documents(manual_requirement(CI))["config"]
    model = tmp_path / "model.json"
    model.write_text(json.dumps(config))
    target = tmp_path / "target.json"
    target.write_text(json.dumps(TARGET))
    initial = refresh(
        {"config": config},
        {},
        Run(REVISION, str(TARGET["context"]), WHEN, job_status="failure"),
    )
    initial["pending"] = [
        {"record": "REQ-1", "task": "issues/first", "opened_at": "2026-10-23T10:00:00Z"},
        {"record": "REQ-1", "task": "issues/other", "opened_at": "2026-10-23T11:00:00Z", "dimension": "delivery"},
    ]
    state = tmp_path / "state" / "production.json"
    state.parent.mkdir()
    state.write_text(json.dumps(initial) + "\n")
    shared = [str(model), "--state", str(state), "--revision", REVISION, "--root", str(tmp_path)]
    return Scenario(
        state,
        copy.deepcopy(initial),
        ["refresh", *shared, "--context", str(TARGET["context"]), "--job-status", "success"],
        ["observe", "manual-qa", *shared, "--target", str(target)],
    )


@pytest.mark.parametrize("outer", ["refresh", "observe"])
def test_overlapping_observers_conflict_then_retry_without_losing_work(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], outer: str
) -> None:
    """Both compute from serial 1; only the first publication wins, and a fresh retry merges it."""
    inner_args = scenario.args("observe" if outer == "refresh" else "refresh")
    original = refresh if outer == "refresh" else manual_qa.observe
    nested_output = io.StringIO()

    def interleaved(*args: Any, **kwargs: Any) -> Any:
        candidate = original(*args, **kwargs)
        with contextlib.redirect_stdout(nested_output):
            assert cli.exacly_main(inner_args) == 0
        return candidate

    with monkeypatch.context() as patch:
        patch.setattr(
            cli if outer == "refresh" else manual_qa, "refresh" if outer == "refresh" else "observe", interleaved
        )
        assert cli.exacly_main(scenario.args(outer)) == 1
    captured = capsys.readouterr()
    assert "Wrote" not in captured.out
    assert ("exacly refresh:" if outer == "refresh" else "exacly observe:") in captured.err
    assert "changed" in captured.err.lower() or "conflict" in captured.err.lower()
    committed = json.loads(scenario.state.read_text())
    assert committed["serial"] == scenario.initial["serial"] + 1
    assert committed["lineage"] == scenario.initial["lineage"]
    assert committed["pending"] == scenario.initial["pending"]
    assert nested_output.getvalue().startswith("Wrote")

    assert cli.exacly_main(scenario.args(outer)) == 0
    final = valid_state(json.loads(scenario.state.read_text()))
    assert final["serial"] == scenario.initial["serial"] + 2
    assert final["lineage"] == scenario.initial["lineage"]
    assert final["pending"] == scenario.initial["pending"]
    assessments = final["records"]["REQ-1"]["assessments"]
    assert len(assessments) == 3
    assert {a["source"] for a in assessments} == {"tests", manual_qa.SOURCE}
    assert all(a["state"] == "passed" for a in assessments)


@pytest.mark.parametrize("command", ["refresh", "observe"])
@pytest.mark.parametrize("contents", ["{broken", "null", "{}", '{"serial":true}', '{"serial": NaN}'])
def test_malformed_previous_state_is_reported_without_overwriting(
    scenario: Scenario, capsys: pytest.CaptureFixture[str], command: str, contents: str
) -> None:
    scenario.state.write_text(contents)
    assert cli.exacly_main(scenario.args(command)) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith(f"exacly {command}:")
    assert "Wrote" not in captured.out
    assert scenario.state.read_text() == contents


@pytest.mark.parametrize("command", ["refresh", "observe"])
def test_missing_state_directory_is_created_by_publication(scenario: Scenario, command: str) -> None:
    scenario.state.unlink()
    scenario.state.parent.rmdir()
    assert cli.exacly_main(scenario.args(command)) == 0
    state = valid_state(json.loads(scenario.state.read_text()))
    assert state["serial"] == 1


@pytest.mark.parametrize("command", ["refresh", "observe"])
@pytest.mark.parametrize("seconds", ["-1", "nan", "inf", "-inf", "nonsense"])
def test_state_lock_timeout_requires_finite_nonnegative_seconds(
    scenario: Scenario, capsys: pytest.CaptureFixture[str], command: str, seconds: str
) -> None:
    before = scenario.state.read_bytes()
    with pytest.raises(SystemExit) as error:
        cli.exacly_main([*scenario.args(command), f"--lock-timeout={seconds}"])
    assert error.value.code == 2
    assert "finite nonnegative seconds" in capsys.readouterr().err
    assert scenario.state.read_bytes() == before


@pytest.mark.parametrize("command", ["refresh", "observe"])
def test_requested_lock_timeout_is_forwarded_to_publication(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    received: list[float] = []
    before = scenario.state.read_bytes()

    def blocked(snapshot: state_writer.Snapshot, state: dict[str, Any], *, lock_timeout: float) -> None:
        received.append(lock_timeout)
        raise state_writer.StateLockTimeoutError("state publication lock timed out; retry with a fresh observation")

    monkeypatch.setattr(state_writer, "publish", blocked)
    assert cli.exacly_main([*scenario.args(command), "--lock-timeout", "0"]) == 1
    captured = capsys.readouterr()
    assert received == [0.0]
    assert "timed out" in captured.err
    assert "Wrote" not in captured.out
    assert scenario.state.read_bytes() == before


@pytest.mark.parametrize("command", ["refresh", "observe"])
def test_state_output_io_error_is_reported_without_success(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    before = scenario.state.read_bytes()

    def failed(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full while staging state")

    monkeypatch.setattr(state_writer, "publish", failed)
    assert cli.exacly_main(scenario.args(command)) == 1
    captured = capsys.readouterr()
    assert "disk full while staging state" in captured.err
    assert "Wrote" not in captured.out
    assert scenario.state.read_bytes() == before


@pytest.mark.parametrize("command", ["refresh", "observe"])
def test_post_publication_durability_error_does_not_claim_success_or_rollback(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    def published_but_unsynced(snapshot: state_writer.Snapshot, state: dict[str, Any], **kwargs: Any) -> None:
        scenario.state.write_text(json.dumps(state))
        raise state_writer.PublicationDurabilityError(
            "state was published but directory fsync failed; inspect before retrying"
        )

    monkeypatch.setattr(state_writer, "publish", published_but_unsynced)
    assert cli.exacly_main(scenario.args(command)) == 1
    captured = capsys.readouterr()
    assert "was published" in captured.err
    assert "inspect before retrying" in captured.err
    assert "Wrote" not in captured.out
    assert json.loads(scenario.state.read_text())["serial"] == scenario.initial["serial"] + 1


def test_invalid_junit_is_reported_without_publishing(scenario: Scenario, capsys: pytest.CaptureFixture[str]) -> None:
    junit = scenario.state.parent / "broken.xml"
    junit.write_text("<testsuite><testcase")
    before = scenario.state.read_bytes()
    assert cli.exacly_main([*scenario.refresh_args, "--junit", str(junit)]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("exacly refresh:")
    assert "Wrote" not in captured.out
    assert scenario.state.read_bytes() == before
