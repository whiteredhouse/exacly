"""Real local Git objects, sealed metadata and exact branch publication for handoffs."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any

import pytest
from exacly import handoff_git as git

Doc = dict[str, Any]
AUTHOR = {"name": "Reviewed operator", "email": "operator@example.test", "time": 1793376000}


def object_file(repository: Path, kind: str, data: bytes) -> str:
    encoded = kind.encode() + b" " + str(len(data)).encode() + b"\0" + data
    oid = hashlib.sha1(encoded).hexdigest()
    path = repository / "objects" / oid[:2] / oid[2:]
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(zlib.compress(encoded))
    path.chmod(0o600)
    return oid


def tree_file(repository: Path, entries: list[tuple[str, str, str]]) -> str:
    entries.sort(key=lambda item: item[1] + ("/" if item[0] == "40000" else ""))
    raw = b"".join(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid) for mode, name, oid in entries)
    return object_file(repository, "tree", raw)


def source_base(repository: Path) -> str:
    source = object_file(repository, "blob", b"original source\n")
    executable = object_file(repository, "blob", b"#!/bin/sh\nexit 0\n")
    binary = object_file(repository, "blob", b"\x00unrelated\xff")
    child = tree_file(repository, [("100644", "main.py", source), ("100755", "verify.sh", executable)])
    tree = tree_file(repository, [("40000", "src", child), ("100644", "keep.bin", binary)])
    commit = object_file(
        repository,
        "commit",
        (
            f"tree {tree}\n"
            "author Fixture <fixture@example.test> 1000000000 +0000\n"
            "committer Fixture <fixture@example.test> 1000000000 +0000\n\nbase\n"
        ).encode(),
    )
    (repository / "refs/heads/base").write_text(commit + "\n")
    return commit


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    target = private / "source.git"
    git.initialize_repository(target)
    return target, source_base(target)


def command(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["/usr/bin/git", "--git-dir=" + str(repository), *args],
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )


def prepared(repository: Path, base: str, **options: Any) -> Doc:
    return git.prepare(
        repository,
        base,
        {"src/main.py": b"reviewed replacement\n", "new.bin": b"\x00\xffbinary"},
        author=AUTHOR,
        message="Apply reviewed artifact bytes\n",
        **options,
    )


def test_initialized_repository_is_private_sealed_and_cannot_be_reinitialized(repository: tuple[Path, str]) -> None:
    path, base = repository
    identity = git.inspect_repository(path)
    assert identity["path"] == str(path) and identity["instance_id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / git.MARKER).stat().st_mode) == 0o600
    assert command(path, "rev-parse", "--is-bare-repository") == b"true\n"
    assert git.read_ref(path, "refs/heads/base") == base
    with pytest.raises(FileExistsError):
        git.initialize_repository(path)
    assert git.inspect_repository(path) == identity


def test_preview_changes_no_repository_bytes_and_preserves_unrelated_source(repository: tuple[Path, str]) -> None:
    path, base = repository
    before = {item.relative_to(path): item.read_bytes() for item in path.rglob("*") if item.is_file()}
    proposal = prepared(path, base)
    after = {item.relative_to(path): item.read_bytes() for item in path.rglob("*") if item.is_file()}
    assert before == after
    assert proposal == prepared(path, base)
    git.write_objects(path, proposal)
    git.write_objects(path, proposal)
    git.compare_and_swap(path, "refs/heads/reviewed", None, proposal["commit"])
    assert git.read_ref(path, "refs/heads/reviewed") == proposal["commit"]
    assert command(path, "show", proposal["commit"] + ":src/main.py") == b"reviewed replacement\n"
    assert command(path, "show", proposal["commit"] + ":new.bin") == b"\x00\xffbinary"
    assert command(path, "show", proposal["commit"] + ":keep.bin") == b"\x00unrelated\xff"
    assert command(path, "show", proposal["commit"] + ":src/verify.sh") == b"#!/bin/sh\nexit 0\n"
    changed = command(path, "diff-tree", "--no-commit-id", "--name-only", "-r", base, proposal["commit"])
    assert changed.splitlines() == [b"new.bin", b"src/main.py"]
    assert command(path, "rev-parse", proposal["commit"] + "^").strip().decode() == base
    git.synchronize(path, "refs/heads/reviewed")


def test_existing_executable_mode_is_preserved_and_new_files_are_not_executable(repository: tuple[Path, str]) -> None:
    path, base = repository
    proposal = git.prepare(path, base, {"src/verify.sh": b"new script\n"}, author=AUTHOR, message="Reviewed")
    assert proposal["changes"][0]["after"]["mode"] == "100755"
    git.write_objects(path, proposal)
    assert command(path, "ls-tree", proposal["tree"], "src/").startswith(b"100644 blob")
    assert command(path, "ls-tree", proposal["commit"] + ":src", "verify.sh").startswith(b"100755 blob")


def test_literal_path_and_before_directory_names_have_no_special_meaning(repository: tuple[Path, str]) -> None:
    path, base = repository
    files = {"path/before/path": b"one", "new/path": b"two", "before.txt": b"three"}
    proposal = git.prepare(path, base, files, author=AUTHOR, message="reviewed")
    git.write_objects(path, proposal)
    for name, data in files.items():
        assert command(path, "show", proposal["commit"] + ":" + name) == data


def test_overlapping_retained_paths_are_rejected_even_when_other_hashes_match(repository: tuple[Path, str]) -> None:
    path, base = repository
    proposal = prepared(path, base)
    changed = copy.deepcopy(proposal["changes"][0])
    changed["path"] += "/child"
    proposal["changes"].append(changed)
    proposal["changes"].sort(key=lambda item: item["path"])
    with pytest.raises(ValueError, match="overlap"):
        git.verify_prepared(proposal)


def test_publishing_requires_expected_ref_and_does_not_overwrite_a_newer_branch(repository: tuple[Path, str]) -> None:
    path, base = repository
    proposal = prepared(path, base)
    git.write_objects(path, proposal)
    git.compare_and_swap(path, "refs/heads/result", None, base)
    with pytest.raises(ValueError, match="ref changed"):
        git.compare_and_swap(path, "refs/heads/result", None, proposal["commit"])
    git.compare_and_swap(path, "refs/heads/result", base, proposal["commit"])
    with pytest.raises(ValueError, match="ref changed"):
        git.compare_and_swap(path, "refs/heads/result", base, base)
    assert git.read_ref(path, "refs/heads/result") == proposal["commit"]


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "a//b", "a/./b", ".git/config", ".GiT/config", "foo\\bar", "a:b", "a./b", "a\nb", ""],
)
def test_unsafe_destination_paths_are_rejected_before_writes(repository: tuple[Path, str], path: str) -> None:
    target, base = repository
    before = command(target, "count-objects", "-v")
    with pytest.raises(ValueError):
        git.prepare(target, base, {path: b"untrusted"}, author=AUTHOR, message="reviewed")
    assert command(target, "count-objects", "-v") == before


@pytest.mark.parametrize(
    "ref",
    [
        "HEAD",
        "refs/tags/release",
        "refs/heads/../escape",
        "refs/heads/a.lock",
        "refs/heads/a..b",
        "refs/heads/.hidden",
        "refs/heads/a@{b}",
    ],
)
def test_only_literal_local_branch_refs_are_permitted(repository: tuple[Path, str], ref: str) -> None:
    path, base = repository
    with pytest.raises(ValueError):
        git.compare_and_swap(path, ref, None, base)


@pytest.mark.parametrize("mode", ["120000", "160000", "40000"])
def test_mapped_symlinks_gitlinks_and_directory_replacement_are_rejected(
    repository: tuple[Path, str], mode: str
) -> None:
    path, base = repository
    oid = object_file(path, "blob", b"/operator/secrets")
    if mode == "40000":
        oid = tree_file(path, [])
    elif mode == "160000":
        oid = base
    tree = tree_file(path, [(mode, "unsafe", oid)])
    source = object_file(
        path, "commit", f"tree {tree}\nauthor A <a@b> 1 +0000\ncommitter A <a@b> 1 +0000\n\nbase\n".encode()
    )
    for destination in ("unsafe", "unsafe/child.py"):
        if mode == "40000" and "/" in destination:
            continue
        with pytest.raises(ValueError, match=r"symlink|non-directory|directory"):
            git.prepare(path, source, {destination: b"replacement"}, author=AUTHOR, message="reviewed")


@pytest.mark.parametrize(
    "damage",
    [
        "config",
        "HEAD",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "shallow",
        "commondir",
        "config.worktree",
        "hooks",
        "refs/replace",
        "objects/pack/incomplete.promisor",
    ],
)
def test_changed_or_executable_repository_metadata_fails_before_git_runs(
    repository: tuple[Path, str], damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    selected = path / damage
    selected.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if damage in {"hooks", "refs/replace"}:
        selected.mkdir()
    else:
        selected.write_bytes(b"unsafe metadata\n")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unsafe repository metadata reached Git subprocess execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError):
        prepared(path, base)


def test_inherited_git_environment_cannot_redirect_objects_config_or_hooks(
    repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    sentinel = tmp_path / "executed"
    config = tmp_path / "malicious-config"
    config.write_text(f"[core]\n\tfsmonitor = touch {sentinel}\n[include]\n\tpath = /not-readable\n")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "wrong-objects"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "alternate"))
    proposal = prepared(path, base)
    git.write_objects(path, proposal)
    git.compare_and_swap(path, "refs/heads/result", None, proposal["commit"])
    assert git.read_ref(path, "refs/heads/result") == proposal["commit"]
    assert not sentinel.exists() and not (tmp_path / "wrong-objects").exists()


def test_symbolic_destination_does_not_grant_authority_to_modify_its_target(repository: tuple[Path, str]) -> None:
    path, base = repository
    (path / "refs/heads/result").write_text("ref: refs/heads/base\n")
    proposal = prepared(path, base)
    git.write_objects(path, proposal)
    with pytest.raises(ValueError, match="symbolic"):
        git.compare_and_swap(path, "refs/heads/result", base, proposal["commit"])
    assert git.read_ref(path, "refs/heads/base") == base


@pytest.mark.parametrize("damage", ["object", "commit", "path", "mode", "unrelated"])
def test_retained_object_package_cannot_be_rewritten_or_expand_its_path_scope(
    repository: tuple[Path, str], damage: str
) -> None:
    path, base = repository
    proposal = copy.deepcopy(prepared(path, base))
    if damage == "object":
        proposal["objects"][0]["data_base64"] = base64.b64encode(b"different bytes").decode()
    elif damage == "commit":
        proposal["message"] = "different approved message"
    elif damage == "path":
        proposal["changes"][0]["path"] = "unrelated.py"
    elif damage == "mode":
        proposal["changes"][0]["after"]["mode"] = "100755"
    else:
        proposal["changes"].pop()
    with pytest.raises(ValueError):
        git.verify_prepared(proposal)


def test_missing_base_objects_block_publication_and_keep_existing_refs(repository: tuple[Path, str]) -> None:
    path, base = repository
    proposal = prepared(path, base)
    original = json.loads(json.dumps(proposal))
    tree = command(path, "rev-parse", base + "^{tree}").strip().decode()
    (path / "objects" / tree[:2] / tree[2:]).unlink()
    with pytest.raises(ValueError, match="Git operation failed"):
        git.write_objects(path, proposal)
    assert proposal == original
    assert git.read_ref(path, "refs/heads/base") == base
    assert git.read_ref(path, "refs/heads/result") is None


def test_package_larger_than_legacy_record_limit_remains_supported(repository: tuple[Path, str]) -> None:
    path, base = repository
    data = b"\x00binary-artifact" * (700_000)
    assert len(data) > 8 * 1024 * 1024
    proposal = git.prepare(path, base, {"large.bin": data}, author=AUTHOR, message="reviewed")
    assert git.verify_prepared(proposal)["commit"] == proposal["commit"]
    git.write_objects(path, proposal)
    git.compare_and_swap(path, "refs/heads/result", None, proposal["commit"])
    assert command(path, "show", proposal["commit"] + ":large.bin") == data


def test_git_process_holding_pipes_open_is_killed_at_the_finite_deadline(
    repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    pidfile = tmp_path / "git-process.pid"
    executable = tmp_path / "controlled-git"
    executable.write_text(
        f"#!/usr/bin/python3\nimport os,time\nopen({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: str(executable))
    start = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        prepared(path, base, timeout=0.2)
    assert time.monotonic() - start < 2
    assert pidfile.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_recovery_synchronizes_every_nested_reference_parent(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    ref = "refs/heads/team/feature"
    git.compare_and_swap(path, ref, None, base)
    synced: list[Path] = []
    monkeypatch.setattr(git, "_sync", synced.append)
    git.synchronize(path, ref)
    assert synced == [path / ref, path / "refs/heads/team", path / "refs/heads", path / "refs", path]


def test_packed_nested_reference_can_be_synchronized_without_a_loose_parent(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    ref = "refs/heads/team/feature"
    git.compare_and_swap(path, ref, None, base)
    command(path, "pack-refs", "--all", "--prune")
    nested = path / "refs/heads/team"
    if nested.exists():
        nested.rmdir()
    assert git.read_ref(path, ref) == base
    synced: list[Path] = []
    monkeypatch.setattr(git, "_sync", synced.append)
    git.synchronize(path, ref)
    assert synced == [path / "packed-refs", path / "refs/heads", path / "refs", path]


def test_unused_hash_valid_objects_cannot_expand_authorized_git_writes(repository: tuple[Path, str]) -> None:
    path, base = repository
    proposal = prepared(path, base)
    raw = b"not part of the approved file mapping"
    oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    proposal["objects"].append({"type": "blob", "oid": oid, "data_base64": base64.b64encode(raw).decode()})
    with pytest.raises(ValueError, match="outside its authorized change"):
        git.verify_prepared(proposal)


def test_corrupt_unrelated_base_blob_blocks_ref_publication(repository: tuple[Path, str]) -> None:
    path, base = repository
    proposal = prepared(path, base)
    identity = command(path, "rev-parse", base + ":keep.bin").strip().decode()
    selected = path / "objects" / identity[:2] / identity[2:]
    selected.write_bytes(zlib.compress(b"blob 7\0CORRUPT"))
    with pytest.raises(ValueError, match="Git operation failed"):
        git.write_objects(path, proposal)
    assert git.read_ref(path, "refs/heads/result") is None
    assert git.read_ref(path, "refs/heads/base") == base


def test_existing_packed_object_bytes_are_checked_before_a_loose_shadow_is_written(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    proposal = prepared(path, base)
    git.write_objects(path, proposal)
    git.compare_and_swap(path, "refs/heads/result", None, proposal["commit"])
    command(path, "repack", "-ad")
    target = proposal["changes"][0]["after"]["oid"]
    loose = path / "objects" / target[:2] / target[2:]
    assert not loose.exists()
    original = git._object

    def colliding_object(repository: Path, oid: str, kind: str, budget: Any) -> bytes:
        if oid == target:
            return b"controlled different packed bytes under the same alleged SHA-1"
        return original(repository, oid, kind, budget)

    monkeypatch.setattr(git, "_object", colliding_object)
    with pytest.raises(ValueError, match="object differs from preparation"):
        git.write_objects(path, proposal)
    assert not loose.exists()


def test_collision_detecting_git_hash_rejection_precedes_any_object_write(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    original = git._run
    payload = b"controlled SHA-1 collision candidate"
    before = command(path, "count-objects", "-v")

    def reject_collision(repository: Path, args: list[str], budget: Any, **options: Any) -> bytes:
        if args[0] == "hash-object" and options.get("input_data") == payload:
            assert "--stdin" in args and "--no-filters" in args and "-w" not in args
            raise ValueError("trusted Git collision-detection rejected the payload")
        return original(repository, args, budget, **options)

    monkeypatch.setattr(git, "_run", reject_collision)
    with pytest.raises(ValueError, match="collision-detection"):
        git.prepare(path, base, {"new.bin": payload}, author=AUTHOR, message="reviewed")
    assert command(path, "count-objects", "-v") == before
