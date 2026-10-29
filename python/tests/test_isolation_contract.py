"""Boundary inputs and filesystem exchanges fail closed without relying on candidate reports."""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from exacly import _isolation_seccomp, isolation


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "usr" / "bin").mkdir(parents=True)
    executable = root / "usr" / "bin" / "python3"
    executable.write_bytes(b"reviewed runtime binary")
    executable.chmod(0o755)
    return root


@pytest.fixture
def runtime(root: Path) -> isolation.Runtime:
    return isolation.Runtime(root, isolation.rootfs_digest(root), "/usr/bin/python3")


def policy(runtime: isolation.Runtime) -> dict[str, Any]:
    return {
        "schema_version": isolation.VERSION,
        "backend": isolation.BACKEND,
        "rootfs": str(runtime.rootfs),
        "rootfs_digest": runtime.rootfs_digest,
        "python": runtime.python,
        "bubblewrap": str(runtime.bubblewrap),
    }


def test_policy_is_explicit_and_does_not_accept_runtime_switches(runtime: isolation.Runtime) -> None:
    assert isolation.parse_policy(policy(runtime)) == runtime
    supplied = policy(runtime)
    supplied["arguments"] = ["--share-net", "--bind", "/", "/"]
    with pytest.raises(isolation.IsolationError, match="unsupported"):
        isolation.parse_policy(supplied)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", "99"),
        ("backend", "host-process"),
        ("rootfs", "relative"),
        ("rootfs_digest", "sha256:" + "f" * 63),
        ("bubblewrap", "bwrap"),
        ("python", "usr/bin/python3"),
        ("python", "/inputs/python3"),
        ("python", "/tmp/python3"),
        ("python", "/usr/../inputs/python3"),
        ("python", "/usr//bin/python3"),
    ],
)
def test_policy_rejects_ambiguous_runtime_identity(runtime: isolation.Runtime, key: str, value: Any) -> None:
    supplied = policy(runtime)
    supplied[key] = value
    with pytest.raises(isolation.IsolationError):
        isolation.parse_policy(supplied)


def test_rootfs_digest_covers_bytes_mode_paths_and_symlink_targets(root: Path, tmp_path: Path) -> None:
    original = isolation.rootfs_digest(root)
    binary = root / "usr" / "bin" / "python3"
    binary.write_bytes(b"different runtime binary")
    changed = isolation.rootfs_digest(root)
    assert changed != original
    binary.chmod(0o700)
    assert isolation.rootfs_digest(root) != changed
    binary.rename(binary.with_name("another-python"))
    renamed = isolation.rootfs_digest(root)
    assert renamed != changed
    (root / "link").symlink_to("/usr/bin/another-python")
    linked = isolation.rootfs_digest(root)
    (root / "link").unlink()
    (root / "link").symlink_to("/usr/bin/missing-python")
    assert isolation.rootfs_digest(root) != linked
    detached = tmp_path / "copy"
    detached.mkdir()
    before = isolation.rootfs_digest(root)
    captured, _ = isolation._tree(root, detached)
    assert captured == before == isolation.rootfs_digest(root) == isolation.rootfs_digest(detached)


@pytest.mark.parametrize("kind", ["fifo", "socket", "hardlink", "setuid", "setgid"])
def test_runtime_special_nodes_or_privileged_files_are_rejected(root: Path, kind: str) -> None:
    path = root / "unsafe"
    server: socket.socket | None = None
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
    elif kind == "hardlink":
        os.link(root / "usr" / "bin" / "python3", path)
    else:
        path.write_bytes(b"privileged executable")
        path.chmod(0o4755 if kind == "setuid" else 0o2755)
    try:
        with pytest.raises(isolation.IsolationError):
            isolation.rootfs_digest(root)
    finally:
        if server is not None:
            server.close()


def test_runtime_root_cannot_be_host_root_or_symlink(root: Path, tmp_path: Path) -> None:
    with pytest.raises(isolation.IsolationError, match="detached"):
        isolation.rootfs_digest(Path("/"))
    (tmp_path / "redirect").symlink_to(root, target_is_directory=True)
    with pytest.raises(isolation.IsolationError, match="symlinks"):
        isolation.rootfs_digest(tmp_path / "redirect")


@pytest.mark.parametrize("name", [".", "..", "../secret", "/secret", "a/../secret", "a//b", "a/./b", "a\\b", "x\0y"])
def test_snapshot_paths_fail_before_launch(
    runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(isolation, "_bubblewrap", lambda _: pytest.fail("invalid snapshot reached the launcher"))
    with pytest.raises(isolation.IsolationError):
        isolation.run(runtime, ["/usr/bin/python3"], {name: b"candidate"}, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("snapshots", [{"source": b"a", "source/child": b"b"}, {"source": "not bytes"}])
def test_snapshot_identity_and_content_types_are_explicit(
    runtime: isolation.Runtime, tmp_path: Path, snapshots: Any
) -> None:
    with pytest.raises(isolation.IsolationError):
        isolation.run(runtime, [runtime.python], snapshots, tmp_path / "out")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, 3601, 10**400])
def test_invalid_timeout_cannot_start_candidate(runtime: isolation.Runtime, tmp_path: Path, timeout: Any) -> None:
    with pytest.raises(isolation.IsolationError):
        isolation.run(runtime, [runtime.python], {}, tmp_path / "out", timeout=timeout)


def test_rootfs_mismatch_cannot_release_command(
    runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (runtime.rootfs / "changed").write_bytes(b"unreviewed input")
    monkeypatch.setattr(isolation, "_bubblewrap", lambda _: None)
    monkeypatch.setattr(isolation, "_execute", lambda *args: pytest.fail("changed rootfs released candidate"))
    with pytest.raises(isolation.IsolationError, match="reviewed digest"):
        isolation.run(runtime, [runtime.python], {}, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".exacly-isolation-*"))


def test_guest_interpreter_cannot_be_a_symlink_to_host_or_candidate(
    runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = runtime.rootfs / "usr" / "bin" / "python3"
    executable.unlink()
    executable.symlink_to("/inputs/candidate.py")
    changed = isolation.Runtime(runtime.rootfs, isolation.rootfs_digest(runtime.rootfs), runtime.python)
    monkeypatch.setattr(isolation, "_bubblewrap", lambda _: None)
    monkeypatch.setattr(isolation, "_execute", lambda *args: pytest.fail("untrusted bootstrap interpreter launched"))
    with pytest.raises(isolation.IsolationError, match="regular executable"):
        isolation.run(changed, [runtime.python], {}, tmp_path / "out")


def test_output_overwrite_and_overlap_are_refused(runtime: isolation.Runtime, tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_bytes(b"preserve")
    for destination in (output, runtime.rootfs / "outputs"):
        with pytest.raises(isolation.IsolationError, match=r"new directory|overlap"):
            isolation.run(runtime, [runtime.python], {}, destination)
    assert (output / "keep").read_bytes() == b"preserve"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "socket", "hardlink"])
def test_artifact_collection_never_follows_untrusted_objects(tmp_path: Path, kind: str) -> None:
    source, copied = tmp_path / "source", tmp_path / "copied"
    source.mkdir()
    copied.mkdir()
    outside = tmp_path / "operator-secret"
    outside.write_bytes(b"authority must remain outside the exchange")
    artifact = source / "attack"
    server: socket.socket | None = None
    if kind == "symlink":
        artifact.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(artifact)
    elif kind == "socket":
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(artifact))
    else:
        os.link(outside, artifact)
    try:
        with pytest.raises(isolation.IsolationError):
            isolation._tree(source, copied, artifacts=True)
        assert outside.read_bytes() == b"authority must remain outside the exchange"
        assert not list(copied.iterdir())
    finally:
        if server is not None:
            server.close()


def test_artifact_byte_and_entry_caps_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"ab")
    monkeypatch.setattr(isolation, "MAX_ARTIFACT_BYTES", 1)
    with pytest.raises(isolation.IsolationError, match="byte limit"):
        isolation._tree(source, artifacts=True)
    monkeypatch.setattr(isolation, "MAX_ARTIFACT_BYTES", 2)
    monkeypatch.setattr(isolation, "MAX_ARTIFACT_FILES", 0)
    with pytest.raises(isolation.IsolationError, match="entry limit"):
        isolation._tree(source, artifacts=True)


def test_rootfs_concurrent_write_is_not_captured_as_reviewed_content(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = os.read
    mutated = False
    binary = root / "usr" / "bin" / "python3"

    def write_during_read(fd: int, length: int) -> bytes:
        nonlocal mutated
        block = read(fd, length)
        if not mutated and block:
            mutated = True
            binary.write_bytes(b"concurrent runtime replacement")
        return block

    monkeypatch.setattr(os, "read", write_during_read)
    with pytest.raises(isolation.IsolationError, match=r"changed|grew"):
        isolation.rootfs_digest(root)
    assert mutated


@pytest.mark.parametrize("attack", ["shared_namespace", "capability", "privileges", "network", "extra", "integer_true"])
def test_failed_attestation_is_never_treated_as_verified(attack: str) -> None:
    host = {key: f"{key}:[1]" for key in isolation._NAMESPACES}
    facts: dict[str, Any] = {
        "verified": True,
        "namespaces": {key: f"{key}:[2]" for key in host},
        "capabilities": {key: "0000000000000000" for key in isolation._CAPABILITIES},
        "no_new_privileges": True,
        "rootfs_read_only": True,
        "inputs_read_only": True,
        "inherited_fds_closed": True,
        "network": "none",
        "pid_namespace_init": True,
        "syscalls_filtered": True,
        "keyrings_denied": True,
        "unsupported_socket_families_denied": True,
        "async_io_denied": True,
    }
    assert isolation._attestation(copy.deepcopy(facts), host) == facts
    if attack == "shared_namespace":
        facts["namespaces"]["pid"] = host["pid"]
    elif attack == "capability":
        facts["capabilities"]["CapEff"] = "0000000000000001"
    elif attack == "privileges":
        facts["no_new_privileges"] = False
    elif attack == "network":
        facts["network"] = "host"
    elif attack == "extra":
        facts["approved"] = True
    else:
        facts["verified"] = 1
    with pytest.raises(isolation.IsolationError):
        isolation._attestation(facts, host)


def test_non_linux_operations_stop_before_filesystem_or_runtime_use(
    runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(isolation.IsolationError, match="Linux"):
        isolation.rootfs_digest(runtime.rootfs)
    with pytest.raises(isolation.IsolationError, match="Linux"):
        isolation.run(runtime, [runtime.python], {}, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_missing_native_filter_dependency_starts_no_candidate(
    runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_library(name: str) -> Any:
        raise OSError("required libseccomp unavailable")

    monkeypatch.setattr(ctypes, "CDLL", missing_library)
    monkeypatch.setattr(isolation, "_bubblewrap", lambda _: None)
    monkeypatch.setattr(isolation, "_execute", lambda *args: pytest.fail("unfiltered candidate launched"))
    with pytest.raises(isolation.IsolationError, match="libseccomp unavailable"):
        isolation.run(runtime, [runtime.python], {}, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".exacly-isolation-*"))


def test_native_filter_descriptor_is_kernel_sealed_and_closed_after_use() -> None:
    with _isolation_seccomp.native_filter() as policy:
        descriptor = policy.fd
        assert os.pread(descriptor, 1024 * 1024, 0)
        assert set(policy.syscalls) == {*_isolation_seccomp.DENIED, "socket", "socketpair"}
        with pytest.raises(OSError) as failure:
            os.write(descriptor, b"replace security policy")
        assert failure.value.errno == errno.EPERM
    with pytest.raises(OSError) as failure:
        os.fstat(descriptor)
    assert failure.value.errno == errno.EBADF


def test_namespace_wait_supports_descriptors_above_select_fd_set_limit() -> None:
    process = subprocess.Popen(["/bin/true"])
    anchor = os.pidfd_open(process.pid)
    high = fcntl.fcntl(anchor, fcntl.F_DUPFD_CLOEXEC, 2048)
    try:
        process.wait(timeout=5)
        isolation._wait_namespace(high)
    finally:
        os.close(high)
        os.close(anchor)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
