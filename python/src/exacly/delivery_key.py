"""Owner-private, merge-purpose signing keys with no verification-publication authority.

The file header and signature domain differ from the verification pilot's key contract. An
operator initializes and pins a distinct key instance; possessing it authenticates selected marker
content, not independent approval or the fact that any remote operation happened. Secret bytes
never appear in return values, journals or diagnostics.
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
MAGIC = b"EXACLY DELIVERY MERGE KEY v1\n"
KEY_BYTES = 32
FILE_BYTES = len(MAGIC) + KEY_BYTES
DOMAIN = b"exacly.delivery.merge.marker.v1\0"
MAX_PAYLOAD_BYTES = 256 * 1024


def _path(path: Path) -> Path:
    store.require(
        isinstance(path, Path) and path.is_absolute() and path == path.absolute() and ".." not in path.parts,
        "delivery key requires a pinned canonical absolute path",
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
    """Exclusively retain one new merge key, syncing it before returning its pinned identity."""
    absolute = _path(path)
    with store.locked(absolute, write=True, lock_timeout=lock_timeout) as (selected, parent_fd):
        descriptor = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            encoded = MAGIC + os.urandom(KEY_BYTES)
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                store.require(count > 0, "delivery key initialization did not write its complete content")
                written += count
            info = store.file_info(descriptor)
            store.require(
                info.st_size == FILE_BYTES and _identity(selected.lstat()) == _identity(info),
                "delivery key changed during initialization",
            )
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return "sha256:" + hashlib.sha256(encoded).hexdigest()
        finally:
            os.close(descriptor)


def _key(path: Path, key_id: str, lock_timeout: float) -> bytes:
    absolute = _path(path)
    store.require(isinstance(key_id, str) and store.DIGEST.fullmatch(key_id), "invalid pinned delivery key identity")
    with store.locked(absolute, write=False, lock_timeout=lock_timeout) as (selected, parent_fd):
        store.check_file_info(selected.lstat())
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = store.file_info(descriptor)
            store.require(before.st_size == FILE_BYTES, "delivery key has an unsupported format or purpose")
            encoded = bytearray()
            while block := os.read(descriptor, FILE_BYTES + 1 - len(encoded)):
                encoded.extend(block)
                store.require(len(encoded) <= FILE_BYTES, "delivery key changed while being read")
            store.require(
                len(encoded) == FILE_BYTES
                and encoded.startswith(MAGIC)
                and _identity(os.fstat(descriptor)) == _identity(before)
                and _identity(selected.lstat()) == _identity(before),
                "delivery key changed or has an unsupported format or purpose",
            )
            actual = "sha256:" + hashlib.sha256(encoded).hexdigest()
            store.require(hmac.compare_digest(actual, key_id), "delivery key does not match its pinned identity")
            # A previous initialization may have stopped after writing bytes but before a successful
            # durability barrier. Re-establish both barriers before any signing authority leaves.
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return bytes(encoded[len(MAGIC) :])
        finally:
            os.close(descriptor)


def validate_key(path: Path, key_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> None:
    """Check the exact initialized private merge key and its durability, without returning bytes."""
    _key(path, key_id, lock_timeout)


def sign(payload: Doc, path: Path, key_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Sign bounded merge-marker content; matching a protected key cannot approve that content."""
    store.require(
        isinstance(payload, dict) and payload.get("key_id") == key_id and payload.get("stage") == "merge",
        "delivery marker must bind the pinned key and merge purpose",
    )
    content = store.canonical(payload).encode("utf-8")
    store.require(len(content) <= MAX_PAYLOAD_BYTES, "delivery marker payload exceeds its size limit")
    key = _key(path, key_id, lock_timeout)
    return hmac.new(key, DOMAIN + content, hashlib.sha256).hexdigest()


def verify(
    payload: Doc, proof: str, path: Path, key_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> bool:
    """Compare exact content in constant time; invalid protected storage always remains an error."""
    expected = sign(payload, path, key_id, lock_timeout=lock_timeout)
    if not isinstance(proof, str) or re.fullmatch(r"[0-9a-f]{64}", proof) is None:
        return False
    return hmac.compare_digest(expected, proof)
