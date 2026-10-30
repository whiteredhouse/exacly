"""A sealed, local-only Git boundary for reviewed executor output handoffs.

There is no checkout, hook, filter, credential or network operation. Object construction is pure
Python; the trusted installed Git binary only reads existing objects and performs a no-dereference
compare-and-swap of one local branch. Repository preparation and base import are operator actions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import uuid
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

from exacly import execution_store as store

Doc = dict[str, Any]
VERSION = "0.1.0"
CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
HEAD = b"ref: refs/heads/main\n"
MARKER = "exacly-handoff.json"
OID = re.compile(r"[0-9a-f]{40}\Z")
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_RAW_PACKAGE_BYTES = 80 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_FILES = 4096
MODES = {b"40000", b"100644", b"100755", b"120000", b"160000"}


def relative_path(value: Any) -> str:
    """The first local contract uses normalized portable literal Git paths."""
    store.require(isinstance(value, str) and 0 < len(value) <= 1024, "invalid handoff Git path")
    assert isinstance(value, str)
    parts = value.split("/")
    store.require(
        len(parts) <= 32
        and all(
            re.fullmatch(r"[A-Za-z0-9._-]+", part)
            and part not in (".", "..")
            and part.lower() != ".git"
            and not part.endswith(".")
            for part in parts
        ),
        "handoff paths must be normalized portable paths without .git aliases",
    )
    return value


def reference(value: Any) -> str:
    store.require(isinstance(value, str) and value.startswith("refs/heads/"), "handoff requires one local branch ref")
    assert isinstance(value, str)
    parts = value.removeprefix("refs/heads/").split("/")
    store.require(
        len(value) <= 1024
        and all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) and ".." not in part and not part.endswith((".", ".lock"))
            for part in parts
        ),
        "invalid handoff local branch ref",
    )
    return value


def _oid(value: Any) -> str:
    store.require(isinstance(value, str) and OID.fullmatch(value), "invalid handoff Git object identity")
    return str(value)


def _hash(kind: str, data: bytes) -> str:
    return hashlib.sha1(kind.encode() + b" " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _read_file(path: Path, limit: int, *, private: bool = False) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        store.require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.geteuid()
            and before.st_size <= limit
            and (not private or stat.S_IMODE(before.st_mode) == 0o600),
            "invalid protected handoff Git metadata",
        )
        data = bytearray()
        while block := os.read(fd, min(65536, limit + 1 - len(data))):
            data.extend(block)
            store.require(len(data) <= limit, "handoff Git metadata exceeded its limit")
        after = os.fstat(fd)
        current = path.lstat()

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

        store.require(identity(before) == identity(after) == identity(current), "handoff Git metadata changed")
        return bytes(data)
    finally:
        os.close(fd)


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if path.is_dir() else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def initialize_repository(path: Path, *, timeout: float = 30) -> Doc:
    """Create a new private SHA-1 bare repository, without importing or publishing source."""
    budget = store.Deadline(timeout)
    absolute = store.location(path)
    store.require(absolute == path.absolute().resolve(), "handoff repository path is not canonical")
    absolute.mkdir(mode=0o700)
    for name in ("objects", "objects/info", "objects/pack", "refs", "refs/heads", "refs/tags"):
        (absolute / name).mkdir(mode=0o700)
    _new_file(absolute / "config", CONFIG)
    _new_file(absolute / "HEAD", HEAD)
    _new_file(
        absolute / MARKER,
        (
            json.dumps(
                {"schema_version": VERSION, "instance_id": uuid.uuid4().hex, "object_format": "sha1"}, sort_keys=True
            )
            + "\n"
        ).encode(),
    )
    for folder in (
        absolute / "objects/info",
        absolute / "objects/pack",
        absolute / "objects",
        absolute / "refs/heads",
        absolute / "refs/tags",
        absolute / "refs",
        absolute,
        absolute.parent,
    ):
        budget.check()
        _sync(folder)
    return inspect_repository(absolute, timeout=budget.remaining())


def inspect_repository(path: Path, *, timeout: float = 30) -> Doc:
    """Validate the actual tool-owned destination before any Git command can read its config."""
    budget = store.Deadline(timeout)
    absolute = store.location(path)
    store.require(absolute == absolute.resolve(), "handoff repository traverses a symbolic link")
    info = absolute.lstat()
    store.require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        "handoff repository must be an owner-private directory",
    )
    store.require(_read_file(absolute / "config", 4096, private=True) == CONFIG, "handoff repository config changed")
    store.require(_read_file(absolute / "HEAD", 4096, private=True) == HEAD, "handoff repository HEAD changed")
    raw = _read_file(absolute / MARKER, 4096, private=True)
    try:
        marker = json.loads(raw, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        instance = uuid.UUID(hex=marker["instance_id"])
        store.require(
            set(marker) == {"schema_version", "instance_id", "object_format"}
            and marker["schema_version"] == VERSION
            and marker["object_format"] == "sha1"
            and instance.hex == marker["instance_id"]
            and instance.version == 4,
            "invalid handoff repository identity",
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid handoff repository identity") from error
    for name in (
        ".git",
        "commondir",
        "gitdir",
        "worktrees",
        "config.worktree",
        "shallow",
        "hooks",
        "info/grafts",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "refs/replace",
    ):
        store.require(not os.path.lexists(absolute / name), "unsupported handoff repository metadata: " + name)
    count = 0
    for root in (absolute / "objects", absolute / "refs"):
        store.require(root.is_dir() and not root.is_symlink(), "invalid handoff repository directory")
        stack = [root]
        while stack:
            directory = stack.pop()
            for entry in directory.iterdir():
                budget.check()
                count += 1
                store.require(count <= MAX_ENTRIES, "handoff repository metadata exceeds its entry limit")
                found = entry.lstat()
                store.require(
                    found.st_uid == os.geteuid()
                    and not stat.S_ISLNK(found.st_mode)
                    and (stat.S_ISDIR(found.st_mode) or (stat.S_ISREG(found.st_mode) and found.st_nlink == 1))
                    and entry.suffix != ".promisor",
                    "unsafe handoff Git object or reference metadata",
                )
                if stat.S_ISDIR(found.st_mode):
                    stack.append(entry)
    if os.path.lexists(absolute / "packed-refs"):
        _read_file(absolute / "packed-refs", 8 * 1024 * 1024)
    return {
        "schema_version": VERSION,
        "path": str(absolute),
        "instance_id": instance.hex,
        "device": info.st_dev,
        "inode": info.st_ino,
        "config_digest": "sha256:" + hashlib.sha256(CONFIG).hexdigest(),
    }


def _run(
    path: Path,
    args: list[str],
    budget: store.Deadline,
    *,
    limit: int = 1024 * 1024,
    input_data: bytes | None = None,
) -> bytes:
    budget.check()
    if input_data is not None:
        # Anonymous bounded input avoids pipe-write deadlocks and never touches the destination.
        with tempfile.TemporaryFile(dir="/tmp") as incoming:
            incoming.write(input_data)
            incoming.seek(0)
            budget.check()
            return _execute(path, args, budget, limit=limit, incoming=incoming)
    return _execute(path, args, budget, limit=limit)


def _execute(
    path: Path, args: list[str], budget: store.Deadline, *, limit: int, incoming: BinaryIO | None = None
) -> bytes:
    budget.check()
    executable = shutil.which("git", path="/usr/bin:/bin")
    store.require(executable is not None, "the trusted system Git executable is unavailable")
    assert executable is not None
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    command = [
        executable,
        "--no-optional-locks",
        "--git-dir=" + str(path),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.logAllRefUpdates=false",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        *args,
    ]
    output = bytearray()
    errors = bytearray()
    process = subprocess.Popen(
        command,
        stdin=incoming if incoming is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        with selectors.DefaultSelector() as selected:
            for stream, destination in ((process.stdout, output), (process.stderr, errors)):
                os.set_blocking(stream.fileno(), False)
                selected.register(stream, selectors.EVENT_READ, destination)
            while selected.get_map():
                budget.check()
                for key, _ in selected.select(min(budget.remaining(), 0.1)):
                    budget.check()
                    block = os.read(key.fd, 65536)
                    if not block:
                        selected.unregister(key.fileobj)
                        continue
                    target = key.data
                    target.extend(block)
                    store.require(
                        len(target) <= (limit if target is output else 65536), "Git output exceeded its bound"
                    )
            code = process.wait(timeout=budget.remaining())
        budget.check()
        store.require(code == 0, "trusted Git operation failed: " + errors.decode("utf-8", "replace")[:4096])
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise ValueError("trusted Git operation timed out") from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _object(path: Path, identity: str, kind: str, budget: store.Deadline) -> bytes:
    _oid(identity)
    size = _run(path, ["cat-file", "-s", identity], budget, limit=128).strip()
    store.require(size.isdigit() and int(size) <= MAX_OBJECT_BYTES, "Git object exceeds its size bound")
    data = _run(path, ["cat-file", kind, identity], budget, limit=int(size) + 1)
    store.require(
        len(data) == int(size) and _hash(kind, data) == identity, "Git object content differs from its identity"
    )
    return data


def _checked_hash(path: Path, kind: str, raw: bytes, budget: store.Deadline) -> str:
    """Retain the installed Git binary's SHA-1 collision detection without writing an object."""
    expected = _hash(kind, raw)
    actual = _run(
        path, ["hash-object", "--stdin", "--no-filters", "-t", kind], budget, limit=128, input_data=raw
    ).strip()
    store.require(actual == expected.encode(), "Git collision-detecting object identity differs from preparation")
    return expected


def _existing_object(path: Path, identity: str, budget: store.Deadline) -> tuple[str, bytes] | None:
    answer = _run(path, ["cat-file", "--batch-check"], budget, limit=256, input_data=(identity + "\n").encode()).split()
    if answer == [identity.encode(), b"missing"]:
        return None
    store.require(
        len(answer) == 3 and answer[0] == identity.encode() and answer[1] in {b"blob", b"tree", b"commit", b"tag"},
        "invalid existing Git object metadata",
    )
    kind = answer[1].decode()
    return kind, _object(path, identity, kind, budget)


def _verify_closure(path: Path, commit: str, budget: store.Deadline) -> None:
    _run(
        path, ["rev-list", "--objects", "--no-object-names", "--missing=error", commit], budget, limit=MAX_ENTRIES * 41
    )
    _run(path, ["fsck", "--no-reflogs", "--no-dangling", "--no-progress", commit], budget)


def _tree(data: bytes) -> dict[bytes, tuple[bytes, str]]:
    result: dict[bytes, tuple[bytes, str]] = {}
    cursor = 0
    while cursor < len(data):
        space = data.find(b" ", cursor)
        end = data.find(b"\0", space + 1)
        store.require(cursor < space < end and len(data) >= end + 21, "invalid Git tree object")
        mode, name, oid = data[cursor:space], data[space + 1 : end], data[end + 1 : end + 21].hex()
        store.require(
            mode in MODES
            and name not in result
            and name not in (b"", b".", b"..")
            and b"/" not in name
            and name.lower() != b".git",
            "invalid or unsafe Git tree entry",
        )
        result[name] = mode, oid
        store.require(len(result) <= MAX_ENTRIES, "Git tree exceeds its entry bound")
        cursor = end + 21
    return result


def _tree_bytes(entries: dict[bytes, tuple[bytes, str]]) -> bytes:
    ordered = sorted(entries, key=lambda name: name + (b"/" if entries[name][0] == b"40000" else b""))
    return b"".join(entries[name][0] + b" " + name + b"\0" + bytes.fromhex(entries[name][1]) for name in ordered)


def _author(value: Any) -> Doc:
    store.require(isinstance(value, dict) and set(value) == {"name", "email", "time"}, "invalid handoff commit author")
    store.require(
        isinstance(value["name"], str)
        and 0 < len(value["name"]) <= 200
        and value["name"].strip() == value["name"]
        and not any(character in value["name"] for character in "\n\r\0<>")
        and isinstance(value["email"], str)
        and re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@-]{1,254}", value["email"])
        and "@" in value["email"]
        and type(value["time"]) is int
        and 0 <= value["time"] <= 253402300799,
        "invalid handoff commit author fields",
    )
    return dict(value)


def _commit(tree: str, base: str, author: Doc, message: str) -> bytes:
    store.require(
        isinstance(message, str) and 0 < len(message.encode()) <= 65536 and "\0" not in message,
        "invalid handoff commit message",
    )
    who = f"{author['name']} <{author['email']}> {author['time']} +0000"
    return f"tree {tree}\nparent {base}\nauthor {who}\ncommitter {who}\n\n{message}".encode()


def read_ref(path: Path, ref: str, *, timeout: float = 30) -> str | None:
    budget = store.Deadline(timeout)
    inspect_repository(path, timeout=budget.remaining())
    ref = reference(ref)
    selected = path / ref
    if os.path.lexists(selected):
        raw = _read_file(selected, 4096)
        store.require(not raw.startswith(b"ref:"), "handoff destination must not be a symbolic reference")
        store.require(re.fullmatch(rb"[0-9a-f]{40}\n", raw), "invalid handoff destination ref")
        return raw[:-1].decode()
    packed = path / "packed-refs"
    found: str | None = None
    if packed.exists():
        for line in _read_file(packed, 8 * 1024 * 1024).splitlines():
            budget.check()
            if not line or line.startswith((b"#", b"^")):
                continue
            fields = line.split(b" ")
            store.require(
                len(fields) == 2 and OID.fullmatch(fields[0].decode("ascii")), "invalid packed Git references"
            )
            if fields[1] == ref.encode():
                store.require(found is None, "duplicate packed handoff reference")
                found = fields[0].decode()
    return found


def prepare(
    path: Path, base: str, files: Mapping[str, bytes], *, message: str, author: Doc, timeout: float = 30
) -> Doc:
    """Compute an exact add/replace commit package without writing an object or reference."""
    budget = store.Deadline(timeout)
    repository = inspect_repository(path, timeout=budget.remaining())
    base = _oid(base)
    author = _author(author)
    store.require(isinstance(files, Mapping) and 0 < len(files) <= MAX_FILES, "invalid handoff file mapping")
    selected = {relative_path(name): data for name, data in files.items()}
    store.require(
        all(isinstance(data, bytes) for data in selected.values())
        and sum(map(len, selected.values())) <= MAX_OBJECT_BYTES,
        "handoff files exceed their byte bound",
    )
    objects: dict[str, tuple[str, bytes]] = {}
    raw_bytes = 0
    changes: list[Doc] = []

    def retain(kind: str, data: bytes) -> str:
        nonlocal raw_bytes
        identity = _hash(kind, data)
        if identity in objects:
            store.require(objects[identity] == (kind, data), "Git object collision in prepared handoff")
            return identity
        store.require(
            raw_bytes + len(data) <= MAX_RAW_PACKAGE_BYTES and len(objects) < MAX_ENTRIES,
            "prepared Git object package exceeds its raw byte or entry bound",
        )
        _checked_hash(path, kind, data, budget)
        raw_bytes += len(data)
        objects[identity] = kind, data
        return identity

    parent = _object(path, base, "commit", budget)
    retain("commit", parent)
    first = parent.split(b"\n", 1)[0]
    store.require(re.fullmatch(rb"tree [0-9a-f]{40}", first), "base commit has no exact Git tree")
    base_tree = first[5:].decode()
    updates: dict[str, Any] = {}
    for name, data in selected.items():
        cursor = updates
        parts = name.split("/")
        for part in parts[:-1]:
            found = cursor.setdefault(part, {})
            store.require(isinstance(found, dict), "handoff destination paths overlap")
            cursor = found
        store.require(parts[-1] not in cursor, "handoff destination paths overlap")
        cursor[parts[-1]] = data

    def rewrite(old: str | None, replacements: dict[str, Any], prefix: str) -> str:
        budget.check()
        raw = _object(path, old, "tree", budget) if old is not None else b""
        if old is not None:
            retain("tree", raw)
        entries = _tree(raw)
        for name, replacement in sorted(replacements.items()):
            encoded = name.encode()
            store.require(
                not any(key.lower() == encoded.lower() and key != encoded for key in entries),
                "handoff path aliases an existing Git entry",
            )
            existing = entries.get(encoded)
            full = prefix + name
            if isinstance(replacement, dict):
                store.require(
                    existing is None or existing[0] == b"40000", "handoff path traverses a non-directory Git entry"
                )
                result = rewrite(existing[1] if existing else None, replacement, full + "/")
                entries[encoded] = b"40000", result
            else:
                store.require(
                    existing is None or existing[0] in {b"100644", b"100755"},
                    "handoff cannot replace a directory, symlink or gitlink",
                )
                mode = existing[0] if existing else b"100644"
                oid = retain("blob", replacement)
                before = None
                if existing:
                    size = _run(path, ["cat-file", "-s", existing[1]], budget, limit=128).strip()
                    store.require(size.isdigit(), "invalid existing Git blob size")
                    before = {"mode": mode.decode(), "oid": existing[1], "size": int(size)}
                after = {"mode": mode.decode(), "oid": oid, "size": len(replacement)}
                changes.append({"path": full, "before": before, "after": after})
                entries[encoded] = mode, oid
        return retain("tree", _tree_bytes(entries))

    tree = rewrite(base_tree, updates, "")
    commit = retain("commit", _commit(tree, base, author, message))
    package = {
        "schema_version": VERSION,
        "repository": repository,
        "base": base,
        "base_tree": base_tree,
        "tree": tree,
        "commit": commit,
        "author": author,
        "message": message,
        "changes": sorted(changes, key=lambda item: item["path"]),
        "objects": [
            {"type": kind, "oid": identity, "data_base64": base64.b64encode(data).decode()}
            for identity, (kind, data) in sorted(objects.items())
        ],
    }
    verify_prepared(package)
    store.require(
        inspect_repository(path, timeout=budget.remaining()) == repository, "handoff repository identity changed"
    )
    return package


def verify_prepared(value: Any) -> Doc:
    """Verify retained package hashes and exact before/after tree semantics without filesystem IO."""
    try:
        store.require(
            isinstance(value, dict)
            and set(value)
            == {
                "schema_version",
                "repository",
                "base",
                "base_tree",
                "tree",
                "commit",
                "author",
                "message",
                "changes",
                "objects",
            }
            and value["schema_version"] == VERSION,
            "invalid prepared Git handoff package",
        )
        store.require(
            len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
            <= MAX_PACKAGE_BYTES,
            "prepared Git package exceeds its byte bound",
        )
        repository = value["repository"]
        store.require(
            isinstance(repository, dict)
            and set(repository) == {"schema_version", "path", "instance_id", "device", "inode", "config_digest"}
            and repository["schema_version"] == VERSION
            and isinstance(repository["path"], str)
            and Path(repository["path"]).is_absolute()
            and ".." not in Path(repository["path"]).parts
            and str(Path(repository["path"])) == repository["path"]
            and uuid.UUID(hex=repository["instance_id"]).hex == repository["instance_id"]
            and uuid.UUID(hex=repository["instance_id"]).version == 4
            and type(repository["device"]) is int
            and repository["device"] >= 0
            and type(repository["inode"]) is int
            and repository["inode"] > 0
            and repository["config_digest"] == "sha256:" + hashlib.sha256(CONFIG).hexdigest(),
            "invalid prepared Git repository identity",
        )
        store.require(
            isinstance(value["objects"], list) and len(value["objects"]) <= MAX_ENTRIES,
            "prepared Git object count exceeds its bound",
        )
        objects: dict[str, tuple[str, bytes]] = {}
        for item in value["objects"]:
            store.require(
                set(item) == {"type", "oid", "data_base64"} and item["type"] in {"commit", "tree", "blob"},
                "invalid prepared Git object",
            )
            raw = base64.b64decode(item["data_base64"], validate=True)
            identity = _oid(item["oid"])
            store.require(
                len(raw) <= MAX_OBJECT_BYTES and _hash(item["type"], raw) == identity and identity not in objects,
                "prepared Git object identity changed",
            )
            objects[identity] = item["type"], raw

        used: set[str] = set()

        def object_data(identity: str, kind: str) -> bytes:
            selected = objects[_oid(identity)]
            store.require(selected[0] == kind, "prepared Git object type changed")
            used.add(identity)
            return selected[1]

        author = _author(value["author"])
        store.require(
            object_data(value["base"], "commit").split(b"\n", 1)[0] == b"tree " + _oid(value["base_tree"]).encode(),
            "prepared Git base tree changed",
        )
        store.require(
            object_data(value["commit"], "commit")
            == _commit(_oid(value["tree"]), _oid(value["base"]), author, value["message"]),
            "prepared Git commit changed",
        )
        changes = value["changes"]
        store.require(isinstance(changes, list) and 0 < len(changes) <= MAX_FILES, "invalid prepared Git changes")
        paths = [relative_path(item["path"]) for item in changes]
        store.require(paths == sorted(set(paths)), "prepared Git paths are ambiguous")
        grouped: dict[str, Any] = {}
        for change in changes:
            store.require(set(change) == {"path", "before", "after"}, "invalid prepared Git change")
            cursor = grouped
            parts = change["path"].split("/")
            for part in parts[:-1]:
                child = cursor.setdefault(part, {})
                store.require(isinstance(child, dict), "prepared Git paths overlap")
                cursor = child
            store.require(parts[-1] not in cursor, "prepared Git paths overlap")
            cursor[parts[-1]] = (change,)

        def check_tree(before: str | None, after: str, children: dict[str, Any]) -> None:
            previous = _tree(object_data(before, "tree")) if before is not None else {}
            actual = _tree(object_data(after, "tree"))
            expected = dict(previous)
            for name, child in children.items():
                encoded = name.encode()
                store.require(
                    not any(key.lower() == encoded.lower() and key != encoded for key in expected),
                    "prepared Git path aliases an existing entry",
                )
                old = previous.get(encoded)
                store.require(encoded in actual, "prepared Git target disappeared")
                new = actual[encoded]
                if isinstance(child, dict):
                    store.require(
                        (old is None or old[0] == b"40000") and new[0] == b"40000",
                        "prepared Git directory scope changed",
                    )
                    check_tree(old[1] if old else None, new[1], child)
                else:
                    change = child[0]
                    old_doc = change["before"]
                    new_doc = change["after"]
                    store.require(old is None or old[0] in {b"100644", b"100755"}, "unsafe prepared Git base entry")
                    store.require((old_doc is None) == (old is None), "prepared Git original file changed")
                    if old is not None:
                        store.require(
                            set(old_doc) == {"mode", "oid", "size"}
                            and (old_doc["mode"].encode(), old_doc["oid"]) == old
                            and type(old_doc["size"]) is int
                            and old_doc["size"] >= 0,
                            "prepared Git original file identity changed",
                        )
                    store.require(
                        set(new_doc) == {"mode", "oid", "size"}
                        and new_doc["mode"] == (old[0].decode() if old else "100644")
                        and new == (new_doc["mode"].encode(), new_doc["oid"]),
                        "prepared Git produced file identity changed",
                    )
                    blob = object_data(new_doc["oid"], "blob")
                    store.require(
                        type(new_doc["size"]) is int and len(blob) == new_doc["size"],
                        "prepared Git produced file size changed",
                    )
                expected[encoded] = new
            store.require(actual == expected, "prepared Git tree modifies unrelated base content")

        check_tree(value["base_tree"], value["tree"], grouped)
        store.require(set(objects) == used, "prepared Git package contains objects outside its authorized change")
        return dict(value)
    except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as error:
        raise ValueError("invalid prepared Git handoff package") from error


def write_objects(path: Path, prepared: Doc, *, timeout: float = 30) -> None:
    """Durably retain exact prepared objects; this grants no ref-publication authority."""
    budget = store.Deadline(timeout)
    verify_prepared(prepared)
    store.require(
        inspect_repository(path, timeout=budget.remaining()) == prepared["repository"],
        "handoff repository identity differs from preparation",
    )
    # A retained package does not erase the duty to preserve the original repository's base closure.
    _run(
        path,
        ["rev-list", "--objects", "--no-object-names", "--missing=error", prepared["base"]],
        budget,
        limit=MAX_ENTRIES * 41,
    )
    for item in prepared["objects"]:
        budget.check()
        identity = item["oid"]
        destination = path / "objects" / identity[:2] / identity[2:]
        if not destination.parent.exists():
            destination.parent.mkdir(mode=0o700)
            _sync(destination.parent.parent)
        raw = base64.b64decode(item["data_base64"], validate=True)
        _checked_hash(path, item["type"], raw, budget)
        existing = _existing_object(path, identity, budget)
        if existing is not None:
            store.require(existing == (item["type"], raw), "retained Git object differs from preparation")
            if destination.exists():
                _sync(destination)
        else:
            encoded = zlib.compress(item["type"].encode() + b" " + str(len(raw)).encode() + b"\0" + raw)
            budget.check()
            temporary = destination.parent / (".handoff-" + uuid.uuid4().hex)
            try:
                _new_file(temporary, encoded)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            _sync(destination.parent)
    _sync(path / "objects")
    _verify_closure(path, prepared["commit"], budget)
    store.require(
        inspect_repository(path, timeout=budget.remaining()) == prepared["repository"],
        "handoff repository identity changed during object publication",
    )


def compare_and_swap(path: Path, ref: str, expected: str | None, commit: str, *, timeout: float = 30) -> None:
    """Publish one local branch through Git's atomic no-dereference ref transaction."""
    budget = store.Deadline(timeout)
    repository = inspect_repository(path, timeout=budget.remaining())
    ref = reference(ref)
    if expected is not None:
        _oid(expected)
    _oid(commit)
    store.require(read_ref(path, ref, timeout=budget.remaining()) == expected, "handoff destination ref changed")
    _object(path, commit, "commit", budget)
    _verify_closure(path, commit, budget)
    _run(path, ["update-ref", "--no-deref", ref, commit, expected or "0" * 40], budget)
    _sync(path / ref)
    for directory in (path / ref).parents:
        _sync(directory)
        if directory == path:
            break
    store.require(
        inspect_repository(path, timeout=budget.remaining()) == repository,
        "handoff repository identity changed during ref publication",
    )


def synchronize(path: Path, ref: str, *, timeout: float = 30) -> None:
    """Re-synchronize an already-published exact ref before recovery finalizes its receipt."""
    budget = store.Deadline(timeout)
    inspect_repository(path, timeout=budget.remaining())
    selected = path / reference(ref)
    if selected.exists():
        _sync(selected)
        for directory in selected.parents:
            budget.check()
            _sync(directory)
            if directory == path:
                break
    elif (path / "packed-refs").exists():
        _sync(path / "packed-refs")
        for directory in (path / "refs/heads", path / "refs", path):
            budget.check()
            _sync(directory)
    else:
        raise ValueError("published handoff reference is missing")
