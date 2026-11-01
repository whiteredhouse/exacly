"""Owner-private per-instance publication signing keys outside candidate and Git storage.

The operator pins both the absolute path and returned key ID in approved configuration. A key proves
possession of that protected instance's authority; it cannot approve its own scope or evidence. Raw
32-byte keys never appear in return values, journals, process arguments, or diagnostics.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Any

from exacly import execution_store as store

Doc = dict[str, Any]
KEY_BYTES = 32
DOMAIN = b"exacly.remote-publication.signature.v1\0"


def _path(path: Path) -> Path:
    store.require(
        isinstance(path, Path) and path.is_absolute() and path == path.absolute() and ".." not in path.parts,
        "publication key requires a pinned canonical absolute path",
    )
    return store.location(path)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
    )


def initialize(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Exclusively create and durably retain one random key; never replace an existing path."""
    absolute = _path(path)
    with store.locked(absolute, write=True, lock_timeout=lock_timeout) as (selected, parent_fd):
        descriptor = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            value = os.urandom(KEY_BYTES)
            written = 0
            while written < len(value):
                count = os.write(descriptor, value[written:])
                store.require(count > 0, "publication key creation did not write its complete content")
                written += count
            info = store.file_info(descriptor)
            store.require(
                info.st_size == KEY_BYTES and _identity(selected.lstat()) == _identity(info),
                "publication key path changed during initialization",
            )
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return "sha256:" + hashlib.sha256(value).hexdigest()
        finally:
            os.close(descriptor)


def _key(path: Path, key_id: str, lock_timeout: float) -> bytes:
    absolute = _path(path)
    store.require(isinstance(key_id, str) and store.DIGEST.fullmatch(key_id), "invalid pinned publication key identity")
    with store.locked(absolute, write=False, lock_timeout=lock_timeout) as (selected, parent_fd):
        store.check_file_info(selected.lstat())
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = store.file_info(descriptor)
            store.require(before.st_size == KEY_BYTES, "publication key must contain exactly 32 bytes")
            value = bytearray()
            while block := os.read(descriptor, KEY_BYTES + 1 - len(value)):
                value.extend(block)
                store.require(len(value) <= KEY_BYTES, "publication key changed while being read")
            store.require(
                len(value) == KEY_BYTES
                and _identity(os.fstat(descriptor)) == _identity(before)
                and _identity(selected.lstat()) == _identity(before),
                "publication key changed while being read",
            )
            actual = "sha256:" + hashlib.sha256(value).hexdigest()
            store.require(hmac.compare_digest(actual, key_id), "publication key does not match its pinned identity")
            # Initialization may have committed bytes before a directory sync failed. Re-establish
            # both barriers before releasing any authority from a retained key after process loss.
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return bytes(value)
        finally:
            os.close(descriptor)


def validate_key(path: Path, key_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> None:
    """Validate the pinned private instance and its durability without exposing its bytes."""
    _key(path, key_id, lock_timeout)


def sign(payload: Doc, path: Path, key_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Sign bounded canonical JSON explicitly identifying the protected key instance."""
    store.require(
        isinstance(payload, dict) and payload.get("key_id") == key_id,
        "publication signature payload must bind the pinned key identity",
    )
    content = store.canonical(payload).encode("utf-8")
    value = _key(path, key_id, lock_timeout)
    return hmac.new(value, DOMAIN + content, hashlib.sha256).hexdigest()


def verify(
    payload: Doc, proof: str, path: Path, key_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> bool:
    """Check exact signed content in constant time; invalid protected storage always raises."""
    expected = sign(payload, path, key_id, lock_timeout=lock_timeout)
    if not isinstance(proof, str) or re.fullmatch(r"[0-9a-f]{64}", proof) is None:
        return False
    return hmac.compare_digest(expected, proof)
