"""Merge marker authentication has separate protected key storage and purpose from verification."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from exacly import delivery_key as keys
from exacly import execution_store as store
from exacly import publication_key

Doc = dict[str, Any]


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, str]:
    directory = tmp_path / "private-delivery-key"
    directory.mkdir(mode=0o700)
    path = directory / "merge.key"
    return path, keys.initialize(path)


def marker(key_id: str) -> Doc:
    return {"stage": "merge", "key_id": key_id, "operation_id": "sha256:" + "a" * 64, "repository_id": 123}


def test_distinct_initialized_format_private_mode_and_exact_canonical_signing(key: tuple[Path, str]) -> None:
    path, identity = key
    before = path.read_bytes()
    assert before.startswith(keys.MAGIC) and len(before) == keys.FILE_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert identity == "sha256:" + hashlib.sha256(before).hexdigest()
    keys.validate_key(path, identity)
    payload = marker(identity)
    proof = keys.sign(payload, path, identity)
    assert len(proof) == 64
    assert proof == keys.sign(dict(reversed(list(payload.items()))), path, identity)
    assert keys.verify(payload, proof, path, identity) is True
    assert keys.verify({**payload, "repository_id": 124}, proof, path, identity) is False
    assert path.read_bytes() == before


def test_domain_is_distinct_from_plain_and_verification_publication_signatures(key: tuple[Path, str]) -> None:
    path, identity = key
    payload = marker(identity)
    secret = path.read_bytes()[len(keys.MAGIC) :]
    encoded = store.canonical(payload).encode()
    proof = keys.sign(payload, path, identity)
    assert proof != hmac.new(secret, encoded, hashlib.sha256).hexdigest()
    assert proof != hmac.new(secret, publication_key.DOMAIN + encoded, hashlib.sha256).hexdigest()


def test_verification_pilot_key_file_cannot_be_reused_as_delivery_key(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "verification.key"
    identity = publication_key.initialize(path)
    with pytest.raises(ValueError, match="format or purpose"):
        keys.sign(marker(identity), path, identity)
    with pytest.raises(ValueError, match="format or purpose"):
        keys.validate_key(path, identity)


def test_delivery_key_cannot_be_reinterpreted_as_a_verification_pilot_key(key: tuple[Path, str]) -> None:
    path, identity = key
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        publication_key.sign(marker(identity), path, identity)


@pytest.mark.parametrize("proof", ["", "0" * 64, "F" * 64, "not-a-proof", "é" * 64, None])
def test_invalid_proofs_never_authenticate(key: tuple[Path, str], proof: Any) -> None:
    path, identity = key
    assert keys.verify(marker(identity), proof, path, identity) is False


@pytest.mark.parametrize("change", ["missing_stage", "other_stage", "missing_key", "other_key"])
def test_marker_cannot_select_another_purpose_or_unsigned_key(key: tuple[Path, str], change: str) -> None:
    path, identity = key
    payload = marker(identity)
    if change.startswith("missing"):
        payload.pop("stage" if change == "missing_stage" else "key_id")
    else:
        payload["stage" if change == "other_stage" else "key_id"] = "publish" if change == "other_stage" else "other"
    with pytest.raises(ValueError, match="pinned key and merge purpose"):
        keys.sign(payload, path, identity)


def test_wrong_pinned_instance_and_excessive_payload_cannot_sign(key: tuple[Path, str]) -> None:
    path, identity = key
    wrong = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="pinned identity"):
        keys.sign(marker(wrong), path, wrong)
    with pytest.raises(ValueError, match="size limit"):
        keys.sign({**marker(identity), "data": "x" * keys.MAX_PAYLOAD_BYTES}, path, identity)


def test_reinitialization_is_exclusive_and_does_not_replace_existing_key(key: tuple[Path, str]) -> None:
    path, identity = key
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        keys.initialize(path)
    assert path.read_bytes() == before
    keys.validate_key(path, identity)


@pytest.mark.parametrize("damage", ["replacement", "missing", "short", "long", "header", "symlink", "hardlink", "mode"])
def test_changed_missing_or_unsafe_key_blocks_signing_and_verification(
    key: tuple[Path, str], tmp_path: Path, damage: str
) -> None:
    path, identity = key
    payload = marker(identity)
    proof = keys.sign(payload, path, identity)
    if damage == "replacement":
        replacement = path.parent / "replacement.key"
        keys.initialize(replacement)
        replacement.replace(path)
    elif damage == "missing":
        path.unlink()
    elif damage in {"short", "long"}:
        path.write_bytes(b"x" * (keys.FILE_BYTES - 1 if damage == "short" else keys.FILE_BYTES + 1))
    elif damage == "header":
        path.write_bytes(b"x" * len(keys.MAGIC) + path.read_bytes()[len(keys.MAGIC) :])
    elif damage == "mode":
        path.chmod(0o640)
    else:
        other = tmp_path / "other.key"
        path.rename(other)
        if damage == "symlink":
            path.symlink_to(other)
        else:
            path.hardlink_to(other)
    for operation in (lambda: keys.sign(payload, path, identity), lambda: keys.verify(payload, proof, path, identity)):
        with pytest.raises((OSError, ValueError)):
            operation()


@pytest.mark.parametrize("boundary", ["git", "public", "symlink_parent", "relative"])
def test_storage_requires_real_private_operator_directory(tmp_path: Path, boundary: str) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "merge.key"
    if boundary == "git":
        (parent / ".git").mkdir()
        (parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    elif boundary == "public":
        parent.chmod(0o755)
    elif boundary == "symlink_parent":
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        path = alias / "merge.key"
    else:
        path = Path("relative-merge.key")
    with pytest.raises((OSError, ValueError)):
        keys.initialize(path)
    assert not (parent / "merge.key").exists()


def test_private_file_mode_survives_restrictive_umask(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    previous = os.umask(0o777)
    try:
        path = parent / "merge.key"
        identity = keys.initialize(path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    keys.validate_key(path, identity)


@pytest.mark.parametrize("barrier", ["file", "parent"])
def test_failed_initialization_durability_never_returns_identity_and_keeps_exact_recovery_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, barrier: str
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "merge.key"
    original = os.fsync

    def fail_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode) == (barrier == "file"):
            raise OSError("injected key durability failure")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(OSError, match="durability failure"):
            keys.initialize(path)
    assert path.exists() and path.stat().st_size == keys.FILE_BYTES
    with pytest.raises(FileExistsError):
        keys.initialize(path)
    identity = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    keys.validate_key(path, identity)


def test_signing_requires_complete_key_and_parent_durability(
    key: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, identity = key
    payload = marker(identity)
    proof = keys.sign(payload, path, identity)

    def fail_sync(descriptor: int) -> None:
        raise OSError("injected retained-key durability failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="durability failure"):
        keys.sign(payload, path, identity)
    with pytest.raises(OSError, match="durability failure"):
        keys.verify(payload, proof, path, identity)


@pytest.mark.parametrize("operation", ["sign", "verify"])
def test_key_changed_during_read_cannot_release_a_stale_signature(
    key: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path, identity = key
    payload = marker(identity)
    proof = keys.sign(payload, path, identity)
    before = path.read_bytes()
    timestamp = path.stat().st_mtime_ns
    original = os.read
    changed = False

    def change_after_read(descriptor: int, limit: int) -> bytes:
        nonlocal changed
        result = original(descriptor, limit)
        if not changed:
            changed = True
            path.write_bytes(before[:-1] + bytes([before[-1] ^ 1]))
            os.utime(path, ns=(timestamp, timestamp + 1_000_000_000))
        return result

    monkeypatch.setattr(os, "read", change_after_read)
    with pytest.raises(ValueError, match="changed"):
        if operation == "sign":
            keys.sign(payload, path, identity)
        else:
            keys.verify(payload, proof, path, identity)


def test_contended_parent_lock_obeys_finite_wait_budget(key: tuple[Path, str]) -> None:
    import fcntl

    path, identity = key
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(store.LockTimeoutError):
            keys.sign(marker(identity), path, identity, lock_timeout=0.01)
    finally:
        os.close(descriptor)
    assert keys.verify(marker(identity), keys.sign(marker(identity), path, identity), path, identity)
