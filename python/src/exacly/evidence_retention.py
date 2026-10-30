"""Private retained evidence bytes, shared by native admission and composition commands."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from exacly.admission import AdmissionResult

MAX_MISSING_PARENTS = 128


def require_private_filesystem() -> None:
    """Fail before evidence retrieval when required private POSIX filesystem primitives are absent."""
    if os.name != "posix" or any(
        not hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "fchmod", "fsync")
    ):
        raise ValueError("private evidence output requires a supported local POSIX filesystem")


def json_bytes(document: Any) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n").encode()


def write_private(path: Path, data: bytes) -> None:
    """Create private content with final permissions established before writing any bytes."""
    require_private_filesystem()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    require_private_filesystem()
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_parents(path: Path) -> None:
    """Create only absent parent directories privately without changing existing permissions."""
    missing: list[Path] = []
    current = path
    while not current.is_dir():
        if current.exists():
            raise NotADirectoryError(f"output parent must be a directory: {current}")
        if len(missing) >= MAX_MISSING_PARENTS:
            raise ValueError("output path exceeds the permitted missing-parent depth")
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            directory.chmod(0o700)


def admission_bundle(out: Path, result: AdmissionResult) -> None:
    """Retain one complete native bundle; callers protect input/model path placement separately."""
    require_private_filesystem()
    if os.path.lexists(out):
        raise ValueError("--out must be a new directory, not an existing path or symlink")
    if result.receipt.get("status") not in {"admitted", "rejected"}:
        raise ValueError("admission receipt has an invalid status")
    if result.receipt["status"] == "admitted" and (
        result.archive is None or result.report is None or result.state is None
    ):
        raise ValueError("admitted evidence must include its archive, report and state")
    private_parents(out.parent)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}-", dir=out.parent) as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        write_private(folder / "receipt.json", json_bytes(result.receipt))
        if result.receipt["status"] == "admitted":
            assert result.archive is not None and result.report is not None and result.state is not None
            write_private(folder / "archive.zip", result.archive)
            write_private(folder / "junit.xml", result.report)
            write_private(folder / "state.json", json_bytes(result.state))
        sync_directory(folder)
        if os.path.lexists(out):
            raise ValueError("--out must be a new directory, not an existing path or symlink")
        os.rename(folder, out)
        sync_directory(out.parent)
