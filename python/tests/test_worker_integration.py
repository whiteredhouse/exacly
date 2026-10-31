"""Real Git snapshot preparation; these tests do not claim isolated worker execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from exacly import executor_contract, handoff_git, isolation, planning
from exacly import worker_integration as worker
from test_handoff_git import object_file, source_base, tree_file

MODEL = "sha256:" + "a" * 64


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    path = private / "source.git"
    handoff_git.initialize_repository(path)
    return path, source_base(path)


def prepare(repository: tuple[Path, str], **changes: Any) -> worker.PreparedWorker:
    path, revision = repository
    options: dict[str, Any] = {
        "repository_id": handoff_git.inspect_repository(path)["instance_id"],
        "revision": revision,
        "allowed_paths": ["src/main.py"],
        "protected_paths": ["operator/", "verify/"],
        "model_digest": MODEL,
    }
    return worker.prepare(path, **{**options, **changes})


def test_exact_snapshot_is_immutable_and_separate_from_model(repository: tuple[Path, str]) -> None:
    path, revision = repository
    before = {str(item): item.read_bytes() for item in path.rglob("*") if item.is_file()}
    result = prepare(repository)
    identity = result.identity
    assert identity["revision"] == revision and identity["model_digest"] == MODEL
    assert result.inputs["source/src/main.py"] == b"original source\n"
    assert (
        identity["files"][0]["digest"] == executor_contract.roster({"src/main.py": b"original source\n"})[0]["digest"]
    )
    assert identity["inputs"] == executor_contract.roster(result.inputs)
    assert result.argv == ("/usr/bin/python3", "-I", "-S", "/inputs/.exacly-worker.py")
    assert result.expected_outputs == ("source-digests.json",)
    assert executor_contract.snapshots(result.inputs) == dict(result.inputs)
    assert all(value is False for value in identity["claims"].values())
    identity["files"].clear()
    assert result.identity["files"]
    with pytest.raises(TypeError):
        result.inputs["source/src/main.py"] = b"changed"  # type: ignore[index]
    assert prepare(repository).identity == result.identity
    assert before == {str(item): item.read_bytes() for item in path.rglob("*") if item.is_file()}


@pytest.mark.parametrize("revision", ["base", "HEAD", "refs/heads/base", "a" * 7, "A" * 40, "a" * 40 + "^{commit}"])
def test_refs_and_abbreviations_do_not_select_source(repository: tuple[Path, str], revision: str) -> None:
    with pytest.raises(ValueError, match="full exact commit"):
        prepare(repository, revision=revision)


def test_blob_and_missing_object_cannot_masquerade_as_commit(repository: tuple[Path, str]) -> None:
    path, _ = repository
    for revision in (object_file(path, "blob", b"not a commit"), "f" * 40):
        with pytest.raises(ValueError, match=r"Git operation failed|content differs"):
            prepare(repository, revision=revision)


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["src/"],
        ["../src/main.py"],
        ["/src/main.py"],
        ["src/*.py"],
        ["src/main.py", "src/main.py"],
        ["src/z.py", "src/main.py"],
        ["src/Main.py", "src/main.py"],
        ["src/.git/config"],
    ],
)
def test_ambiguous_or_nonliteral_paths_are_rejected(repository: tuple[Path, str], paths: list[str]) -> None:
    with pytest.raises(ValueError):
        prepare(repository, allowed_paths=paths)


@pytest.mark.parametrize(
    "path",
    [
        "spec/model.yaml",
        "tests/acceptance.py",
        ".github/workflows/check.yml",
        "ci/verify.py",
        "pyproject.toml",
        "python/src/exacly/observer.py",
        "src/.exacly-task.json",
        "src/conftest.py",
        "state/main.json",
        "operator/policy.json",
        "verify/observer.py",
        "src/.gitattributes",
    ],
)
def test_reserved_and_independently_protected_paths_deny(repository: tuple[Path, str], path: str) -> None:
    with pytest.raises(ValueError, match="protected paths"):
        prepare(repository, allowed_paths=[path])


def test_protected_path_list_is_required_and_closed(repository: tuple[Path, str]) -> None:
    with pytest.raises(ValueError, match="nonempty"):
        prepare(repository, protected_paths=[])
    with pytest.raises(ValueError, match="protected paths"):
        prepare(repository, protected_paths=["src/main.py"])
    # Directory protection does not match a sibling with a merely similar name.
    assert prepare(repository, protected_paths=["src/m"]).inputs["source/src/main.py"]


@pytest.mark.parametrize("mode", ["120000", "160000", "40000"])
def test_source_never_follows_symlinks_gitlinks_or_directory_entries(repository: tuple[Path, str], mode: str) -> None:
    path, _ = repository
    oid = object_file(path, "blob", b"/operator/secret")
    if mode == "40000":
        oid = tree_file(path, [])
    root = tree_file(path, [(mode, "source", oid)])
    commit = object_file(
        path,
        "commit",
        f"tree {root}\nauthor A <a@example.test> 1 +0000\ncommitter A <a@example.test> 1 +0000\n\nsource\n".encode(),
    )
    with pytest.raises(ValueError, match=r"regular Git blobs|Git operation failed"):
        prepare((path, commit), allowed_paths=["source"])
    with pytest.raises(ValueError, match=r"non-directory|does not exist|Git operation failed"):
        prepare((path, commit), allowed_paths=["source/file.py"])


def test_repository_uuid_and_config_are_checked_before_git(repository: tuple[Path, str], tmp_path: Path) -> None:
    path, _ = repository
    with pytest.raises(ValueError, match="repository identity differs"):
        prepare(repository, repository_id="0" * 32)
    marker = tmp_path / "hook-ran"
    with (path / "config").open("ab") as stream:
        stream.write(f"[core]\n hooksPath = {marker}\n[filter evil]\n smudge = touch {marker}\n".encode())
    with pytest.raises(ValueError, match="config changed"):
        prepare(repository)
    assert not marker.exists()


def test_inherited_git_controls_and_source_code_cannot_execute(
    repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, revision = repository
    marker = tmp_path / "source-ran"
    payload = f"from pathlib import Path; Path({str(marker)!r}).touch()\n".encode()
    package = handoff_git.prepare(
        path,
        revision,
        {"src/main.py": payload},
        message="Untrusted source bytes\n",
        author={"name": "A", "email": "a@example.test", "time": 1},
    )
    handoff_git.write_objects(path, package)
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[filter evil]\n smudge = touch {marker}\n[core]\n hooksPath = {marker}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "touch " + str(marker))
    result = prepare((path, package["commit"]))
    assert result.inputs["source/src/main.py"] == payload
    assert not marker.exists()


def test_revision_and_protected_scope_are_part_of_snapshot_identity(repository: tuple[Path, str]) -> None:
    original = prepare(repository)
    changed = prepare(repository, model_digest="sha256:" + "b" * 64)
    protection = prepare(repository, protected_paths=["operator/", "verifier/", "verify/"])
    assert len({item.identity["snapshot_digest"] for item in (original, changed, protection)}) == 3
    manifest = json.loads(original.inputs[worker.MANIFEST])
    assert manifest["snapshot_digest"] == original.identity["snapshot_digest"]
    snapshot = {
        key: original.identity[key]
        for key in (
            "schema_version",
            "recipe",
            "repository",
            "revision",
            "tree",
            "model_digest",
            "allowed_paths",
            "protected_paths",
            "files",
        )
    }
    assert planning.digest(snapshot) == original.identity["snapshot_digest"]
    compile(original.inputs[worker.SCRIPT], worker.SCRIPT, "exec")


def test_snapshot_total_includes_manifest_and_fixed_recipe(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(isolation, "MAX_INPUT_BYTES", 20)
    with pytest.raises(ValueError, match="snapshot bytes exceed"):
        prepare(repository)
