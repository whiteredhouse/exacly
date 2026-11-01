"""Bound the actual installed delivery implementation and its local runtime inputs.

This is a conservative Linux runtime identity, not a hostile-host or hermetic OS boundary. Trusted
administrators control the installation. No expected approval digest or candidate roster is used;
credential values never appear in the returned profile. Changed runtime inputs invalidate grants.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import stat
import sysconfig
from pathlib import Path
from typing import Any
from urllib.request import getproxies

from exacly import delivery_automation as contracts
from exacly import execution_history, planning
from exacly import execution_store as store

Doc = dict[str, Any]
MAX_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
EXCLUDED = {"site-packages", "dist-packages", "__pycache__"}


def _facts(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _file(path: Path, *, maximum: int = MAX_FILE_BYTES) -> tuple[str, int]:
    """Hash bounded actual regular bytes, detecting replacement and changes during reading."""
    selected = path.resolve(strict=True)
    descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        store.require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "unbounded delivery runtime file")
        digest, size = hashlib.sha256(), 0
        while True:
            data = os.read(descriptor, min(65536, maximum + 1 - size))
            if not data:
                break
            size += len(data)
            store.require(size <= maximum, "delivery runtime file grew beyond its bound")
            digest.update(data)
        store.require(
            _facts(before) == _facts(os.fstat(descriptor)) == _facts(selected.stat()),
            "delivery runtime input changed while fingerprinting",
        )
        return "sha256:" + digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _tree(root: Path, *, package: bool = False) -> Doc:
    root = root.resolve(strict=True)
    result: Doc = {}
    total, directories = 0, 0
    for folder, children, names in os.walk(root, followlinks=False):
        directories += 1
        store.require(directories <= MAX_FILES, "delivery runtime directory inventory exceeds its bound")
        children[:] = sorted(name for name in children if name not in EXCLUDED)
        store.require(
            all(not (Path(folder) / name).is_symlink() for name in children),
            "symlinked delivery runtime directories are unsupported",
        )
        for name in sorted(names):
            if not (name.endswith(".py") or (not package and name.endswith(".so"))):
                continue
            path = Path(folder) / name
            store.require(not package or not path.is_symlink(), "delivery source cannot be a symlink")
            store.require(len(result) < (512 if package else MAX_FILES), "delivery source roster exceeds its bound")
            digest, size = _file(path, maximum=1024 * 1024 if package else MAX_FILE_BYTES)
            total += size
            store.require(
                total <= (32 * 1024 * 1024 if package else 128 * 1024 * 1024), "delivery source exceeds its byte bound"
            )
            result[path.relative_to(root).as_posix()] = {"digest": digest, "size": size}
    store.require(bool(result), "delivery runtime source inventory is empty")
    return result


def _native() -> Doc:
    """Include the actual loaded interpreter/shared libraries, including TLS and libc inputs."""
    maps = Path("/proc/self/maps")
    store.require(maps.is_file() and maps.stat().st_size <= 1024 * 1024, "delivery requires Linux runtime identities")
    with maps.open("rb") as stream:
        content = stream.read(1024 * 1024 + 1)
    store.require(len(content) <= 1024 * 1024, "delivery native runtime map exceeds its bound")
    result: Doc = {}
    total = 0
    for line in content.decode("utf-8", errors="strict").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[5].startswith("/") or "x" not in fields[1]:
            continue
        path = Path(fields[5])
        store.require(not str(path).endswith(" (deleted)"), "loaded delivery runtime file was deleted")
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic != b"\x7fELF":
            continue
        digest, size = _file(path)
        total += size
        store.require(total <= MAX_TOTAL_BYTES and len(result) < 256, "native delivery runtime exceeds its bound")
        label = path.name
        item = {"digest": digest, "size": size}
        store.require(label not in result or result[label] == item, "ambiguous native delivery runtime identity")
        result[label] = item
    store.require(bool(result), "delivery native runtime identity is unavailable")
    return dict(sorted(result.items()))


def _transport() -> Doc:
    import ssl

    paths = ssl.get_default_verify_paths()
    roots: Doc = {}
    total = 0
    if paths.cafile is not None:
        digest, size = _file(Path(paths.cafile))
        roots["bundle"] = {"digest": digest, "size": size}
        total += size
    if paths.capath is not None:
        directory = Path(paths.capath)
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            store.require(len(roots) < 1024, "delivery trust-root inventory exceeds its bound")
            digest, size = _file(path)
            total += size
            store.require(total <= 32 * 1024 * 1024, "delivery trust roots exceed their byte bound")
            roots["directory/" + path.name] = {"digest": digest, "size": size}
    store.require(bool(roots), "delivery TLS trust roots are unavailable")
    # Proxy configuration can contain credentials. Retain only its content digest.
    return {"proxy_configuration_digest": contracts.digest(getproxies()), "tls_trust_roots": roots}


def implementation(*, required: tuple[str, ...] = ()) -> Doc:
    """Read actual installed inputs; stage-owned required names are trusted Python bootstrap."""
    # Load this fixed enforcement dependency set before inspecting mapped native libraries. A
    # dynamically linked interpreter must not gain a previously unpinned TLS/fork/key library
    # merely because the first real native call imports it after the operator's local preview.
    for name in ("ctypes", "fcntl", "select", "selectors", "signal", "socket", "ssl", "subprocess"):
        importlib.import_module(name)
    package = _tree(Path(__file__).resolve().parent, package=True)
    mandatory = {"delivery_profile.py", "delivery_automation.py", *required}
    store.require(mandatory <= set(package), "installed delivery enforcement inventory is incomplete")
    standard = _tree(Path(sysconfig.get_path("stdlib")))
    store.require(
        {"urllib/request.py", "http/client.py", "ssl.py", "socket.py", "hmac.py"} <= set(standard)
        and bool({"pathlib.py", "pathlib/__init__.py"} & set(standard)),
        "installed delivery standard-library profile is unsupported",
    )
    validation = execution_history.profile()
    store.require(
        len(validation["inputs"]) <= 2048 and sum(item["size"] for item in validation["inputs"]) <= MAX_TOTAL_BYTES,
        "installed validation runtime profile exceeds its bounds",
    )
    transport = _transport()
    native = _native()
    total = sum(
        item["size"]
        for inventory in (package, standard, native, transport["tls_trust_roots"])
        for item in inventory.values()
    ) + sum(item["size"] for item in validation["inputs"])
    store.require(total <= MAX_TOTAL_BYTES, "combined delivery runtime exceeds its byte bound")
    result = {
        "implementation": package,
        "validation_runtime": validation,
        "delivery_runtime": {"stdlib": standard, "native": native, "transport": transport},
    }
    store.require(
        len(planning.canonical(result).encode()) <= 512 * 1024, "installed delivery profile exceeds its byte bound"
    )
    return result
