"""The refresh CLI records model and implementation identities without relabeling evidence."""

import json
from pathlib import Path
from typing import Any

import pytest
from exacly import query
from exacly.applicability import Target
from exacly.cli import refresh_main
from test_refresh import assessment, config, requirement

MODEL = "a" * 40
IMPLEMENTATION = "b" * 40


@pytest.fixture(autouse=True)
def clear_github_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GITHUB_SERVER_URL",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_JOB",
    ):
        monkeypatch.delenv(name, raising=False)


def invocation(tmp_path: Path) -> tuple[list[str], Path]:
    model = tmp_path / "model.json"
    model.write_text(json.dumps(config(requirement("REQ-1", {"cases": ["unit::case"]}), app_ref="implementation")))
    (tmp_path / "implementation").mkdir()
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite><testcase file="unit" name="case"/></testsuite>')
    state = tmp_path / "state" / "main.json"
    args = [
        str(model), "--context", "main", "--state", str(state), "--junit", str(junit),
        "--root", str(tmp_path), "--run-url", "https://ci.example/runs/7",
    ]  # fmt: skip
    return args, state


def read_state(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def test_refresh_identifies_approved_model_and_observed_implementation_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, state_file = invocation(tmp_path)
    assert refresh_main([*args, "--revision", IMPLEMENTATION, "--model-revision", MODEL]) == 0
    state = read_state(state_file)
    observed = assessment(state, "REQ-1")
    assert state["config_revision"] == MODEL
    assert observed["subject"] == {"model_revision": MODEL, "revision": IMPLEMENTATION}
    assert observed["context"] == IMPLEMENTATION
    assert observed["evidence"][0]["applies_to"] == IMPLEMENTATION
    assert observed["evidence"][0]["locator"] == "https://ci.example/runs/7"
    assert observed["state"] == "passed"
    shown = capsys.readouterr().out
    assert f"implementation {IMPLEMENTATION}" in shown
    assert f"model {MODEL}" in shown
    model = query.load({"config": json.loads(Path(args[0]).read_text()), "state": state})
    selected = Target({"context": "main", "model_revision": MODEL, "revision": IMPLEMENTATION})
    assert query.assess(model, "REQ-1", selected)["status"]["verification"] == "passed"


@pytest.mark.parametrize("github_default", [False, True])
def test_legacy_invocations_still_use_one_revision_for_both_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], github_default: bool
) -> None:
    args, state_file = invocation(tmp_path)
    if github_default:
        monkeypatch.setenv("GITHUB_SHA", IMPLEMENTATION)
    else:
        args += ["--revision", IMPLEMENTATION]
    assert refresh_main(args) == 0
    state = read_state(state_file)
    observed = assessment(state, "REQ-1")
    assert state["config_revision"] == IMPLEMENTATION
    assert observed["subject"] == {"model_revision": IMPLEMENTATION, "revision": IMPLEMENTATION}
    assert observed["context"] == IMPLEMENTATION
    assert observed["evidence"][0]["applies_to"] == IMPLEMENTATION
    shown = capsys.readouterr().out
    assert f" at {IMPLEMENTATION}: " in shown
    assert "(model " not in shown


def test_github_implementation_default_can_be_combined_with_explicit_model_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, state_file = invocation(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", IMPLEMENTATION)
    assert refresh_main([*args, "--model-revision", MODEL]) == 0
    observed = assessment(read_state(state_file), "REQ-1")
    assert observed["subject"] == {"model_revision": MODEL, "revision": IMPLEMENTATION}


@pytest.mark.parametrize("flag", ["--revision", "--model-revision"])
@pytest.mark.parametrize("invalid", ["", " \t"])
@pytest.mark.parametrize("previous_exists", [False, True])
def test_blank_identity_is_a_clean_error_before_reading_or_writing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    invalid: str,
    previous_exists: bool,
) -> None:
    args, state_file = invocation(tmp_path)
    if flag != "--revision":
        args += ["--revision", IMPLEMENTATION]
    original = "previous state must stay untouched\n"
    if previous_exists:
        state_file.parent.mkdir()
        state_file.write_text(original)
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert refresh_main([*args, flag, invalid]) == 1
    result = capsys.readouterr()
    assert "exacly refresh:" in result.err
    assert "nonblank" in result.err
    assert "Traceback" not in result.err
    assert not result.out
    assert all(path.read_bytes() == content for path, content in before.items())
    if previous_exists:
        assert state_file.read_text() == original
    else:
        assert not state_file.parent.exists()


@pytest.mark.parametrize("previous_exists", [False, True])
def test_distribution_with_split_identities_preserves_previous_state_or_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], previous_exists: bool
) -> None:
    args, state_file = invocation(tmp_path)
    original = None
    if previous_exists:
        assert refresh_main([*args, "--revision", MODEL]) == 0
        original = state_file.read_bytes()
        capsys.readouterr()
    assert refresh_main([*args, "--revision", IMPLEMENTATION, "--model-revision", MODEL, "--distribution"]) == 1
    result = capsys.readouterr()
    assert "distribution" in result.err
    assert "different model and implementation" in result.err
    assert not result.out
    if original is not None:
        assert state_file.read_bytes() == original
    else:
        assert not state_file.parent.exists()


@pytest.mark.parametrize("original", [b"{malformed previous JSON\n", b"\xffunreadable previous state\n"])
def test_split_distribution_is_rejected_before_reading_invalid_previous_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], original: bytes
) -> None:
    args, state_file = invocation(tmp_path)
    state_file.parent.mkdir()
    state_file.write_bytes(original)
    assert refresh_main([*args, "--revision", IMPLEMENTATION, "--model-revision", MODEL, "--distribution"]) == 1
    result = capsys.readouterr()
    assert "distribution" in result.err
    assert "different model and implementation" in result.err
    assert "Traceback" not in result.err
    assert not result.out
    assert state_file.read_bytes() == original


@pytest.mark.parametrize("source_url", [None, "https://artifacts.example/source/custom", ""])
def test_automatic_github_source_link_uses_effective_implementation_and_preserves_explicit_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_url: str | None
) -> None:
    args, state_file = invocation(tmp_path)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/demo")
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    if source_url is not None:
        args += ["--source-url", source_url]
    assert refresh_main([*args, "--revision", IMPLEMENTATION, "--distribution"]) == 0
    released = assessment(read_state(state_file), "REQ-1", "delivery")
    expected = f"https://github.example/acme/demo/blob/{IMPLEMENTATION}" if source_url is None else source_url
    assert released["evidence"][0]["locator"] == (f"{expected}/implementation" if expected else "implementation")
    assert released["subject"]["revision"] == IMPLEMENTATION


def test_missing_revision_outside_github_retains_required_argument_error(tmp_path: Path) -> None:
    args, state_file = invocation(tmp_path)
    with pytest.raises(SystemExit) as error:
        refresh_main(args)
    assert error.value.code == 2
    assert not state_file.parent.exists()
