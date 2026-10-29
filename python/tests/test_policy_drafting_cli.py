"""Policy drafting fingerprints explicit GitHub inputs and publishes only a review candidate."""

import copy
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from exacly import admission, github_evidence
from exacly.cli import exacly_main
from test_admission import REPOSITORY, Source
from test_applicability import B

TOKEN = "test-policy-drafting-primary-token"
FALLBACK_TOKEN = "test-policy-drafting-fallback-token"


@dataclass
class Draft:
    source: Source
    args: list[str]
    proposal: Path
    out: Path
    tokens: list[str]

    def option(self, flag: str, value: str) -> None:
        self.args[self.args.index(flag) + 1] = value


def invocation(tmp_path: Path, source: Source, suffix: str = ".yaml") -> Draft:
    proposal, out = tmp_path / f"producer{suffix}", tmp_path / "review" / "candidate.json"
    configuration = {"producer": source.producer, "observer_paths": source.observer_paths}
    proposal.write_text(json.dumps(configuration) if suffix == ".json" else yaml.safe_dump(configuration))
    args = [
        "evidence", "propose-policy", "github-actions",
        "--repository", REPOSITORY, "--revision", B, "--proposal", str(proposal),
        "--model-file", source.model_file, "--name", "release-contract", "--version", "3",
        "--out", str(out),
    ]  # fmt: skip
    return Draft(source, args, proposal, out, [])


@pytest.fixture
def draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Draft:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", FALLBACK_TOKEN)
    result = invocation(tmp_path, Source(layout="custom"))

    def client(token: str) -> Source:
        result.tokens.append(token)
        return result.source

    monkeypatch.setattr(github_evidence, "GitHubClient", client)
    return result


@pytest.mark.parametrize("layout,suffix", [("standard", ".yaml"), ("custom", ".json")])
@pytest.mark.parametrize("protection", [None, "required", "not_required"])
def test_json_candidate_matches_existing_api_without_installing_or_approving_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    draft: Draft,
    layout: str,
    suffix: str,
    protection: str | None,
) -> None:
    source = Source(layout=layout)
    draft = invocation(tmp_path, source, suffix)
    monkeypatch.setattr(github_evidence, "GitHubClient", lambda token: source)
    expected = admission.propose_policy(
        source,
        REPOSITORY,
        B,
        [source.model_file],
        producer=source.producer,
        observer_paths=source.observer_paths,
        name="release-contract",
        version="3",
        branch_protection=protection,
    )
    source.calls.clear()
    before = copy.deepcopy(source.pages)
    proposal_bytes = draft.proposal.read_bytes()
    state, ledger = tmp_path / "state.json", tmp_path / "executions.sqlite"
    state.write_bytes(b"unchanged model state")
    ledger.write_bytes(b"unchanged execution ledger")
    if protection is not None:
        draft.args.extend(["--branch-protection", protection])
    assert exacly_main([*draft.args, "--json"]) == 0
    shown = capsys.readouterr()
    assert not shown.err
    assert shown.out == draft.out.read_text() == json.dumps(expected, indent=2, sort_keys=True) + "\n"
    assert json.loads(shown.out) == expected
    assert ("branch_protection" in expected) is (protection is not None)
    assert "approved" not in expected and "candidate" not in expected
    assert stat.S_IMODE(draft.out.stat().st_mode) == 0o600
    assert list(draft.out.parent.iterdir()) == [draft.out]
    assert source.pages == before and source.downloads == []
    assert draft.proposal.read_bytes() == proposal_bytes
    assert state.read_bytes() == b"unchanged model state"
    assert ledger.read_bytes() == b"unchanged execution ledger"


def test_human_output_requires_independent_review_and_does_not_print_policy(
    capsys: pytest.CaptureFixture[str], draft: Draft
) -> None:
    assert exacly_main(draft.args) == 0
    shown = capsys.readouterr()
    assert not shown.err
    assert str(draft.out) in shown.out
    assert "candidate" in shown.out and "independent review" in shown.out
    assert "branch_protection" not in json.loads(draft.out.read_text())
    assert draft.tokens == [TOKEN]


def test_repeatable_model_files_keep_explicit_selection_and_authored_order(draft: Draft) -> None:
    extra = "contracts/additional.json"
    draft.source.tree.append(draft.source.add_blob(extra, b'{"spec_version":"0.2.0","records":[]}'))
    draft.args.extend(["--model-file", extra])
    assert exacly_main(draft.args) == 0
    candidate = json.loads(draft.out.read_text())
    assert candidate["model"]["files"] == [draft.source.model_file, extra]
    assert candidate == admission.propose_policy(
        draft.source,
        REPOSITORY,
        B,
        [draft.source.model_file, extra],
        producer=draft.source.producer,
        observer_paths=draft.source.observer_paths,
        name="release-contract",
        version="3",
    )


@pytest.mark.parametrize(
    "flag", ["--repository", "--revision", "--proposal", "--model-file", "--name", "--version", "--out"]
)
def test_every_main_input_is_required_without_source_access(
    capsys: pytest.CaptureFixture[str], draft: Draft, flag: str
) -> None:
    at = draft.args.index(flag)
    del draft.args[at : at + 2]
    with pytest.raises(SystemExit) as error:
        exacly_main(draft.args)
    assert error.value.code == 2
    assert flag in capsys.readouterr().err
    assert draft.tokens == [] and draft.source.calls == []
    assert not draft.out.parent.exists()


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--repository", "https://github.com/acme/demo"),
        ("--repository", "../demo"),
        ("--repository", "acme/demo/extra"),
        ("--revision", "main"),
        ("--revision", "B" * 40),
        ("--model-file", "../contracts.yaml"),
        ("--model-file", "/contracts.yaml"),
        ("--model-file", "contracts\\model.yaml"),
        ("--model-file", "contracts/./model.yaml"),
        ("--model-file", "contracts//model.yaml"),
        ("--model-file", "contracts/*.yaml"),
        ("--model-file", "contracts/model?.yaml"),
        ("--model-file", "contracts/[ab].yaml"),
        ("--model-file", "contracts/model.txt"),
        ("--name", "  "),
        ("--version", "\t"),
    ],
)
def test_invalid_explicit_parameters_fail_before_credentials_or_network(
    capsys: pytest.CaptureFixture[str], draft: Draft, flag: str, value: str
) -> None:
    draft.option(flag, value)
    assert exacly_main(draft.args) == 2
    shown = capsys.readouterr()
    assert not shown.out and "exacly evidence propose-policy:" in shown.err
    assert "Traceback" not in shown.err
    assert draft.tokens == [] and draft.source.calls == []
    assert not draft.out.parent.exists()


def test_duplicate_model_files_fail_before_source_access(draft: Draft) -> None:
    draft.args.extend(["--model-file", draft.source.model_file])
    assert exacly_main(draft.args) == 2
    assert draft.tokens == [] and draft.source.calls == []
    assert not draft.out.parent.exists()


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        [],
        {},
        {"producer": {}},
        {"observer_paths": []},
        {"producer": {}, "observer_paths": [], "extra": True},
        {"producer": {}, "observer_paths": []},
    ],
)
def test_proposal_is_an_exact_explicit_producer_and_observer_paths_document(draft: Draft, configuration: Any) -> None:
    draft.proposal.write_text(json.dumps(configuration))
    assert exacly_main(draft.args) == 2
    assert draft.tokens == [] and draft.source.calls == []
    assert not draft.out.parent.exists()


@pytest.mark.parametrize("which", ["missing", "malformed", "directory", "non_utf8", "invalid_selectors"])
def test_unreadable_or_invalid_local_proposal_has_no_side_effects(draft: Draft, which: str) -> None:
    if which == "missing":
        draft.proposal.unlink()
    elif which == "malformed":
        draft.proposal.write_text("producer: [unclosed")
    elif which == "directory":
        draft.proposal.unlink()
        draft.proposal.mkdir()
    elif which == "non_utf8":
        draft.proposal.write_bytes(b"\xff")
    else:
        draft.proposal.write_text(json.dumps({"producer": draft.source.producer, "observer_paths": ["checks/*"]}))
    assert exacly_main(draft.args) == 2
    assert draft.tokens == [] and draft.source.calls == []
    assert not draft.out.parent.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling", "proposal", "input_hardlink"])
def test_preexisting_outputs_and_input_aliases_are_preserved_before_source_access(
    tmp_path: Path, draft: Draft, kind: str
) -> None:
    draft.out.parent.mkdir()
    if kind == "file":
        draft.out.write_bytes(b"preserve this candidate")
    elif kind == "directory":
        draft.out.mkdir()
    elif kind == "symlink":
        draft.out.symlink_to(draft.proposal)
    elif kind == "dangling":
        draft.out.symlink_to(tmp_path / "missing-input")
    elif kind == "proposal":
        draft.out = draft.proposal
        draft.option("--out", str(draft.out))
    else:
        os.link(draft.proposal, draft.out)
    proposal_bytes = draft.proposal.read_bytes()
    assert exacly_main(draft.args) == 2
    assert os.path.lexists(draft.out)
    assert draft.proposal.read_bytes() == proposal_bytes
    assert draft.tokens == [] and draft.source.calls == []
    if kind == "file":
        assert draft.out.read_bytes() == b"preserve this candidate"
    if kind in {"symlink", "dangling"}:
        assert draft.out.is_symlink()


@pytest.mark.parametrize("kind", ["absent_model", "model_symlink", "output_symlink"])
def test_declared_model_folders_and_their_aliases_are_protected_without_default_layouts(
    tmp_path: Path, draft: Draft, kind: str
) -> None:
    model = tmp_path / ".quality"
    if kind == "model_symlink":
        elsewhere = tmp_path / "real-contracts"
        elsewhere.mkdir()
        model.symlink_to(elsewhere, target_is_directory=True)
        out = elsewhere / "candidate.json"
    elif kind == "output_symlink":
        model.mkdir()
        alias = tmp_path / "model-alias"
        alias.symlink_to(model, target_is_directory=True)
        out = alias / "candidate.json"
    else:
        out = model / "candidate.json"
    draft.option("--out", str(out))
    assert exacly_main(draft.args) == 2
    assert not out.exists()
    assert draft.tokens == [] and draft.source.calls == []


def test_undeclared_conventional_model_folder_is_not_assumed(tmp_path: Path, draft: Draft) -> None:
    out = tmp_path / "spec" / "exacly" / "candidate.json"
    draft.option("--out", str(out))
    assert exacly_main(draft.args) == 0
    assert out.is_file()


def test_root_model_file_requires_output_outside_the_working_directory(tmp_path: Path, draft: Draft) -> None:
    model_file = "model.yaml"
    draft.source.tree.append(draft.source.add_blob(model_file, yaml.safe_dump(draft.source.config).encode()))
    draft.option("--model-file", model_file)
    assert exacly_main(draft.args) == 2
    assert draft.tokens == [] and draft.source.calls == []
    external = tmp_path.parent / f"{tmp_path.name}-candidate.json"
    draft.option("--out", str(external))
    try:
        assert exacly_main(draft.args) == 0
        assert json.loads(external.read_text())["model"]["files"] == [model_file]
    finally:
        external.unlink(missing_ok=True)


@pytest.mark.parametrize("which", ["out", "model"])
def test_symlink_loops_are_clean_local_path_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], draft: Draft, which: str
) -> None:
    loop = tmp_path / ("loop" if which == "out" else ".quality")
    loop.symlink_to(loop.name, target_is_directory=True)
    if which == "out":
        draft.option("--out", str(loop / "candidate.json"))
    assert exacly_main(draft.args) == 2
    shown = capsys.readouterr()
    assert not shown.out and "cannot be resolved" in shown.err
    assert draft.tokens == [] and draft.source.calls == []
    assert loop.is_symlink()


def test_output_parent_file_is_rejected_before_source_access(draft: Draft) -> None:
    draft.out.parent.write_bytes(b"not a directory")
    assert exacly_main(draft.args) == 2
    assert draft.out.parent.read_bytes() == b"not a directory"
    assert draft.tokens == [] and draft.source.calls == []


@pytest.mark.parametrize("primary", [None, "", "   ", TOKEN])
def test_token_precedence_uses_fallback_only_when_primary_is_absent_or_blank(
    monkeypatch: pytest.MonkeyPatch, draft: Draft, primary: str | None
) -> None:
    if primary is None:
        monkeypatch.delenv("GH_TOKEN")
    else:
        monkeypatch.setenv("GH_TOKEN", primary)
    assert exacly_main(draft.args) == 0
    assert draft.tokens == [TOKEN if primary == TOKEN else FALLBACK_TOKEN]


def test_missing_credentials_is_a_clean_error_without_network_or_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], draft: Draft
) -> None:
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN")
    assert exacly_main(draft.args) == 2
    shown = capsys.readouterr()
    assert not shown.out and "GH_TOKEN or GITHUB_TOKEN" in shown.err
    assert draft.tokens == [] and draft.source.calls == []
    assert not draft.out.parent.exists()


@pytest.mark.parametrize("stage", ["parse", "client", "api", "local", "output"])
def test_both_tokens_are_redacted_from_every_stage_of_error_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], draft: Draft, stage: str
) -> None:
    message = f"failed with {TOKEN} and {FALLBACK_TOKEN}"
    if stage == "parse":
        draft.args.extend(["--branch-protection", message])
        with pytest.raises(SystemExit) as error:
            exacly_main(draft.args)
        assert error.value.code == 2
    else:
        if stage == "client":

            def invalid_client(token: str) -> Source:
                raise github_evidence.GitHubError(message)

            monkeypatch.setattr(github_evidence, "GitHubClient", invalid_client)
        elif stage == "api":

            def unavailable(path: str) -> dict[str, Any]:
                raise github_evidence.GitHubError(message)

            monkeypatch.setattr(draft.source, "get_json", unavailable)
        elif stage == "local":
            draft.proposal.write_text(f"producer: [{message}")
        else:

            def cannot_link(source: Path, target: Path) -> None:
                raise OSError(message)

            monkeypatch.setattr(os, "link", cannot_link)
        assert exacly_main(draft.args) == 2
    shown = capsys.readouterr()
    assert not shown.out and shown.err
    assert TOKEN not in shown.err and FALLBACK_TOKEN not in shown.err
    assert "Traceback" not in shown.err
    assert not draft.out.exists()
    assert not draft.out.parent.exists() or not list(draft.out.parent.iterdir())


@pytest.mark.parametrize("failure", ["commit", "tree", "model_missing", "workflow", "model_yaml", "model_value"])
def test_remote_metadata_and_content_failures_return_two_without_a_candidate(
    capsys: pytest.CaptureFixture[str], draft: Draft, failure: str
) -> None:
    if failure == "commit":
        page = next(page for key, page in draft.source.pages.items() if key.endswith(f"git/commits/{B}"))
        page["sha"] = "f" * 40
    elif failure == "tree":
        page = next(
            page
            for key, page in draft.source.pages.items()
            if "git/trees/" in key and page["tree"] is draft.source.tree
        )
        page["truncated"] = True
    elif failure == "model_missing":
        draft.source.tree[:] = [entry for entry in draft.source.tree if entry["path"] != draft.source.model_file]
    else:
        path = draft.source.workflow_path if failure == "workflow" else draft.source.model_file
        content = {
            "workflow": b"jobs: {}",
            "model_yaml": b"records: [unclosed",
            "model_value": b"value: !!set {a: null}",
        }[failure]
        draft.source.tree[:] = [entry for entry in draft.source.tree if entry["path"] != path]
        draft.source.tree.append(draft.source.add_blob(path, content))
    assert exacly_main(draft.args) == 2
    shown = capsys.readouterr()
    assert not shown.out and "exacly evidence propose-policy:" in shown.err
    assert "Traceback" not in shown.err
    assert draft.source.calls
    assert not draft.out.parent.exists()


@pytest.mark.parametrize("concurrent", ["file", "directory", "dangling_symlink"])
def test_atomic_publication_preserves_any_output_created_after_validation(
    monkeypatch: pytest.MonkeyPatch, draft: Draft, concurrent: str
) -> None:
    original_link = os.link

    def racing_link(source: Path, target: Path) -> None:
        if concurrent == "file":
            target.write_bytes(b"concurrent candidate")
        elif concurrent == "directory":
            target.mkdir()
        else:
            target.symlink_to("still-absent")
        original_link(source, target)

    monkeypatch.setattr(os, "link", racing_link)
    assert exacly_main(draft.args) == 2
    assert list(draft.out.parent.iterdir()) == [draft.out]
    if concurrent == "file":
        assert draft.out.read_bytes() == b"concurrent candidate"
    elif concurrent == "directory":
        assert draft.out.is_dir() and not list(draft.out.iterdir())
    else:
        assert draft.out.is_symlink()


@pytest.mark.parametrize("stage", ["write", "link"])
def test_temporary_files_are_cleaned_after_partial_write_or_publication_failure(
    monkeypatch: pytest.MonkeyPatch, draft: Draft, stage: str
) -> None:
    if stage == "write":
        original = tempfile.NamedTemporaryFile

        class BrokenFile:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.file = original(*args, **kwargs)
                self.name = self.file.name

            def __enter__(self) -> "BrokenFile":
                return self

            def __exit__(self, *args: Any) -> None:
                self.file.close()

            def write(self, text: str) -> None:
                self.file.write(text[:10])
                raise OSError("disk full during write")

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", BrokenFile)
    else:

        def failure(source: Path, target: Path) -> None:
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
            raise OSError("publication unavailable")

        monkeypatch.setattr(os, "link", failure)
    assert exacly_main(draft.args) == 2
    assert not draft.out.exists()
    assert not list(draft.out.parent.iterdir())
