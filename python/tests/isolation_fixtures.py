"""A detached, explicitly copied system-Python rootfs for real Linux isolation tests.

The fixture deliberately does not mount any host runtime directory. Only the selected interpreter,
its standard library and the shared libraries needed by the selected test extensions are copied.
It is test infrastructure, not a production rootfs builder or an approval mechanism.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from exacly import isolation

EXTENSIONS = {
    "_bisect",
    "_blake2",
    "_contextvars",
    "_csv",
    "_ctypes",
    "_datetime",
    "_decimal",
    "_hashlib",
    "_heapq",
    "_json",
    "_md5",
    "_opcode",
    "_pickle",
    "_posixsubprocess",
    "_queue",
    "_random",
    "_sha1",
    "_sha2",
    "_sha3",
    "_sha256",
    "_sha512",
    "_socket",
    "_struct",
    "_typing",
    "array",
    "binascii",
    "fcntl",
    "math",
    "mmap",
    "resource",
    "select",
    "termios",
    "unicodedata",
    "zlib",
}


def build_runtime_root(root: Path, python: Path = Path("/usr/bin/python3")) -> Path:
    """Build a small detached root from an explicitly selected trusted system interpreter."""
    assert python.is_file(), "real isolation tests require the system Python interpreter"
    paths = json.loads(
        subprocess.run(
            [
                str(python),
                "-I",
                "-S",
                "-c",
                "import json,sysconfig; print(json.dumps({"
                "'stdlib':sysconfig.get_path('stdlib'),'extensions':sysconfig.get_config_var('DESTSHARED')}))",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    root.mkdir(mode=0o700)
    copied: set[Path] = set()

    def copy_file(source: Path, destination: Path | None = None) -> None:
        target = root / (destination or source).relative_to("/")
        if target in copied:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=True)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
        copied.add(target)

    def copy_dependencies(binary: Path) -> None:
        inspected = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True).stdout
        assert "not found" not in inspected, inspected
        for line in inspected.splitlines():
            found = re.search(r"(?:=>\s*)?(/[^\s]+)", line)
            if found is not None:
                copy_file(Path(found.group(1)))

    copy_file(python)
    copy_dependencies(python)
    stdlib = Path(paths["stdlib"])
    for source in sorted(stdlib.rglob("*.py")):
        if any(part in {"dist-packages", "site-packages", "__pycache__", "test", "tests"} for part in source.parts):
            continue
        copy_file(source)
    for source in sorted(Path(paths["extensions"]).glob("*.so")):
        if source.name.split(".", 1)[0] in EXTENSIONS:
            copy_file(source)
            copy_dependencies(source)
    for name in ("proc", "dev", "tmp", "work", "inputs", "outputs", "run"):
        (root / name).mkdir(exist_ok=True)
    return root


@pytest.fixture(scope="session")
def isolation_runtime(tmp_path_factory: pytest.TempPathFactory) -> isolation.Runtime:
    """Require the real supported runtime; a missing boundary must fail, rather than skip evidence."""
    assert sys.platform == "linux", "the real isolation contract suite requires Linux"
    executable = shutil.which("bwrap")
    assert executable is not None, "install Bubblewrap >= 0.8 to run the required isolation contract suite"
    root = build_runtime_root(tmp_path_factory.mktemp("isolation-runtime") / "rootfs")
    return isolation.Runtime(
        rootfs=root,
        rootfs_digest=isolation.rootfs_digest(root),
        python="/usr/bin/python3",
        bubblewrap=Path(executable),
    )
