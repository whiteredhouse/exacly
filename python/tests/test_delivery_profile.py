"""Actual delivery runtime identities and bounded byte inventories, without provider effects."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from exacly import delivery_profile as profile
from exacly import planning


def test_actual_profile_binds_transport_supervision_and_native_runtime_without_proxy_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-proxy-credential-for-profile-check"
    monkeypatch.setattr(profile, "getproxies", lambda: {"https": "http://user:" + secret + "@proxy.invalid:8080"})
    actual = profile.implementation(required=("automatic_merge.py", "delivery_engine.py", "delivery_runtime.py"))
    runtime = actual["delivery_runtime"]
    assert {"urllib/request.py", "http/client.py", "ssl.py", "socket.py", "hmac.py", "subprocess.py"} <= set(
        runtime["stdlib"]
    )
    assert any(name.startswith("ctypes/") for name in runtime["stdlib"])
    ssl_extension = importlib.import_module("_ssl")
    if getattr(ssl_extension, "__file__", None) is None:
        assert "_ssl" in sys.builtin_module_names
    else:
        assert any("_ssl" in name for name in runtime["stdlib"])
    assert runtime["native"] and runtime["transport"]["tls_trust_roots"]
    assert secret not in planning.canonical(actual)
    assert len(planning.canonical(actual).encode()) <= 512 * 1024


def test_missing_installed_enforcement_module_is_not_an_expected_digest_echo() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        profile.implementation(required=("missing-delivery-enforcement-module.py",))


def test_actual_source_changes_change_runtime_identity(tmp_path: Path) -> None:
    source = tmp_path / "transport.py"
    source.write_text("first reviewed transport\n")
    before = profile._tree(tmp_path)
    source.write_text("substituted transport with different semantics\n")
    after = profile._tree(tmp_path)
    assert before != after
    assert set(before) == set(after) == {"transport.py"}


def test_source_symlinks_cannot_hide_an_enforcement_input(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "worker.py").write_text("source\n")
    root = tmp_path / "package"
    root.mkdir()
    (root / "worker.py").symlink_to(external / "worker.py")
    with pytest.raises(ValueError, match="symlink"):
        profile._tree(root, package=True)
    (root / "worker.py").unlink()
    (root / "nested").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        profile._tree(root, package=True)


def test_runtime_files_and_inventory_are_bounded_before_unlimited_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "one.py"
    source.write_bytes(b"content")
    with pytest.raises(ValueError, match="unbounded"):
        profile._file(source, maximum=3)
    (tmp_path / "two.py").write_bytes(b"second source")
    monkeypatch.setattr(profile, "MAX_FILES", 1)
    with pytest.raises(ValueError, match="roster"):
        profile._tree(tmp_path)
