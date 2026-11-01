"""Private per-instance HMAC signing authority never comes from candidate-controlled bytes."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from exacly import publication_key as keys

Doc = dict[str, Any]


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, str]:
    directory = tmp_path / "protected-publication-key"
    directory.mkdir(mode=0o700)
    path = directory / "instance.key"
    return path, keys.initialize(path)


def test_explicit_initialization_private_shape_and_canonical_exact_signing(key: tuple[Path, str]) -> None:
    path, identity = key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_size == 32
    assert identity == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    keys.validate_key(path, identity)
    payload = {"key_id": identity, "operation_id": "sha256:" + "a" * 64, "repository_id": "123"}
    first = keys.sign(payload, path, identity)
    assert len(first) == 64
    assert first == keys.sign(dict(reversed(list(payload.items()))), path, identity)
    assert keys.verify(payload, first, path, identity) is True
    assert keys.verify({**payload, "repository_id": "124"}, first, path, identity) is False
    assert keys.verify(payload, "0" * 64, path, identity) is False
    assert keys.verify(payload, "not-a-signature", path, identity) is False
    assert keys.verify(payload, "é" * 64, path, identity) is False


def test_signature_is_domain_separated_and_never_changes_secret_content(key: tuple[Path, str]) -> None:
    path, identity = key
    payload = {"key_id": identity, "value": [1, 2, 3]}
    before = path.read_bytes()
    from exacly import execution_store

    ordinary_hmac = hmac.new(before, execution_store.canonical(payload).encode(), hashlib.sha256).hexdigest()
    assert keys.sign(payload, path, identity) != ordinary_hmac
    assert path.read_bytes() == before


def test_reinitialization_is_exclusive_and_never_replaces_key(key: tuple[Path, str]) -> None:
    path, identity = key
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        keys.initialize(path)
    assert path.read_bytes() == before
    keys.validate_key(path, identity)


@pytest.mark.parametrize("damage", ["replacement", "missing", "short", "long", "symlink", "hardlink", "mode"])
def test_changed_missing_or_unsafe_key_cannot_sign_or_verify(
    key: tuple[Path, str], tmp_path: Path, damage: str
) -> None:
    path, identity = key
    payload = {"key_id": identity, "operation_id": "one"}
    proof = keys.sign(payload, path, identity)
    if damage == "replacement":
        replacement = path.parent / "replacement.key"
        replacement.write_bytes(os.urandom(32))
        replacement.chmod(0o600)
        replacement.replace(path)
    elif damage == "missing":
        path.unlink()
    elif damage in {"short", "long"}:
        path.write_bytes(b"a" * (31 if damage == "short" else 33))
    elif damage == "mode":
        path.chmod(0o640)
    else:
        other = tmp_path / "copy.key"
        path.rename(other)
        if damage == "symlink":
            path.symlink_to(other)
        else:
            path.hardlink_to(other)
    for operation in (lambda: keys.sign(payload, path, identity), lambda: keys.verify(payload, proof, path, identity)):
        with pytest.raises((OSError, ValueError)):
            operation()


def test_wrong_identity_or_unsigned_key_selection_is_rejected(key: tuple[Path, str]) -> None:
    path, identity = key
    with pytest.raises(ValueError, match="pinned identity"):
        keys.sign({"key_id": "sha256:" + "0" * 64}, path, "sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="bind the pinned"):
        keys.sign({"operation_id": "one"}, path, identity)
    with pytest.raises(ValueError, match="bind the pinned"):
        keys.sign({"key_id": identity + "other"}, path, identity)


@pytest.mark.parametrize("boundary", ["git", "public", "symlink_parent", "relative"])
def test_key_storage_requires_real_private_operator_storage(tmp_path: Path, boundary: str) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "instance.key"
    if boundary == "git":
        marker = parent / ".git"
        marker.mkdir()
        (marker / "HEAD").write_text("ref: refs/heads/main\n")
    elif boundary == "public":
        parent.chmod(0o755)
    elif boundary == "symlink_parent":
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        path = alias / "instance.key"
    else:
        path = Path("relative-instance.key")
    with pytest.raises((OSError, ValueError)):
        keys.initialize(path)
    assert not (parent / "instance.key").exists()


def test_private_mode_is_independent_of_restrictive_umask(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    previous = os.umask(0o777)
    try:
        path = parent / "instance.key"
        identity = keys.initialize(path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    keys.validate_key(path, identity)


def test_initialization_syncs_complete_file_then_parent_before_releasing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "instance.key"
    original = os.fsync
    synchronized: list[str] = []

    def synchronize(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            assert info.st_size == 32
            synchronized.append("file")
        elif stat.S_ISDIR(info.st_mode):
            assert synchronized == ["file"]
            synchronized.append("parent")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", synchronize)
    identity = keys.initialize(path)
    assert identity.startswith("sha256:") and synchronized == ["file", "parent"]


@pytest.mark.parametrize("barrier", ["file", "parent"])
def test_failed_initialization_sync_never_returns_authority_and_preserves_key_for_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, barrier: str
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "instance.key"
    original = os.fsync

    def failing_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode) == (barrier == "file"):
            raise OSError("injected publication key sync failure")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failing_sync)
        with pytest.raises(OSError, match="key sync failure"):
            keys.initialize(path)
    assert path.exists() and path.stat().st_size == 32
    with pytest.raises(FileExistsError):
        keys.initialize(path)
    identity = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    keys.validate_key(path, identity)


def test_sign_and_verify_require_successful_key_durability_barriers(
    key: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, identity = key
    payload = {"key_id": identity, "operation_id": "one"}
    proof = keys.sign(payload, path, identity)

    def failing_sync(descriptor: int) -> None:
        raise OSError("injected retained key sync failure")

    monkeypatch.setattr(os, "fsync", failing_sync)
    with pytest.raises(OSError, match="key sync failure"):
        keys.sign(payload, path, identity)
    with pytest.raises(OSError, match="key sync failure"):
        keys.verify(payload, proof, path, identity)
