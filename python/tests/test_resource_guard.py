"""Controlled kernel-fact validation and trusted unit construction, not runtime containment proof."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import executor, isolation, planning
from exacly import resource_guard as resources
from executor_fixtures import make_executor_scenario


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), 0.5, "1", 2**63])
def test_bounds_reject_unsupported_or_unbounded_units(value: Any) -> None:
    with pytest.raises(ValueError):
        resources.Bounds(value, 100000, 1024**3, 0, 128)


@pytest.mark.parametrize("field", ["cpu_quota_us", "cpu_period_us", "memory_bytes", "pids"])
def test_cpu_memory_and_process_bounds_cannot_be_zero(field: str) -> None:
    values = asdict(resources.PILOT_BOUNDS)
    values[field] = 0
    with pytest.raises(ValueError):
        resources.Bounds(**values)


def caps() -> dict[str, str]:
    return {"cpu.max": "100000 100000", "memory.max": str(1024**3), "memory.swap.max": "0", "pids.max": "128"}


@pytest.mark.parametrize("field", ["cpu.max", "memory.max", "memory.swap.max", "pids.max"])
def test_unlimited_actual_kernel_caps_deny(field: str) -> None:
    values = caps()
    values[field] = "max 100000" if field == "cpu.max" else "max"
    with pytest.raises(ValueError, match="finite"):
        resources._caps(values, resources.PILOT_BOUNDS)


@pytest.mark.parametrize(
    "field,value",
    [("cpu.max", "100001 100000"), ("memory.max", str(1024**3 + 1)), ("memory.swap.max", "1"), ("pids.max", "129")],
)
def test_weaker_actual_caps_deny(field: str, value: str) -> None:
    values = {**caps(), field: value}
    with pytest.raises(ValueError, match="exceed"):
        resources._caps(values, resources.PILOT_BOUNDS)


def test_cpu_ceiling_compares_exact_ratios_and_finite_stricter_caps_are_valid() -> None:
    actual = resources._caps({**caps(), "cpu.max": "50000 100000", "pids.max": "64"}, resources.PILOT_BOUNDS)
    assert actual["cpu_quota_us"] == 50000 and actual["pids"] == 64


@pytest.mark.parametrize("membership", ["1:cpu:/unit", "0::relative", "0::/a/../b", "0::/a//b", "0::/one\n0::/two"])
def test_nonunified_or_ambiguous_kernel_membership_denies(membership: str) -> None:
    with pytest.raises(ValueError):
        resources._membership(membership)


def test_retained_guard_rechecks_boot_caps_and_physical_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    value: dict[str, Any] = {
        "cgroup": "/system.slice/exacly-test.service",
        "boot_id": "boot-one",
        "inode": 123,
        "caps": caps(),
    }
    monkeypatch.setattr(resources, "_facts", lambda *_: value)
    guard = resources.capture(resources.PILOT_BOUNDS, expected_cgroup=value["cgroup"])
    assert guard.check() == value
    guard.identity["caps"].clear()
    assert guard.check() == value
    for key, changed in (("boot_id", "boot-two"), ("inode", 124), ("caps", {**caps(), "pids.max": "256"})):
        replacement = {**value, key: changed}
        monkeypatch.setattr(resources, "_facts", lambda *_, value=replacement: value)
        with pytest.raises(ValueError, match="identity or caps changed"):
            guard.check()


def test_unit_command_bounds_current_user_and_forwards_only_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PILOT_TOKEN", "never-place-this-secret-in-command-arguments")
    command = resources.unit_command(
        ["/usr/bin/python3", "-m", "trusted_coordinator"],
        cwd=tmp_path,
        bounds=resources.PILOT_BOUNDS,
        timeout=120,
        unit="exacly-reviewed-test",
        forward_env=["PILOT_TOKEN"],
    )
    assert "--preserve-env=PILOT_TOKEN" in command
    assert "--setenv=PILOT_TOKEN" in command
    assert "--property=CPUQuota=100.00%" in command
    assert "--property=MemoryMax=1073741824" in command
    assert "--property=MemorySwapMax=0" in command
    assert "--property=TasksMax=128" in command
    assert "--property=RuntimeMaxSec=120" in command
    assert "--property=KillMode=control-group" in command
    assert "never-place-this-secret" not in json.dumps(command)
    assert "--setenv=EXACLY_RESOURCE_CGROUP=/system.slice/exacly-reviewed-test.service" in command


@pytest.mark.parametrize(
    "name",
    ["PATH", "HOME", "LD_PRELOAD", "PYTHONPATH", "GIT_CONFIG_COUNT", "EXACLY_RESOURCE_CGROUP", "BAD=VALUE", "--quiet"],
)
def test_environment_cannot_expand_runtime_authority(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if "=" not in name:
        monkeypatch.setenv(name, "candidate-controlled")
    with pytest.raises(ValueError, match="unsafe"):
        resources.unit_command(
            ["/usr/bin/python3"],
            cwd=tmp_path,
            bounds=resources.PILOT_BOUNDS,
            timeout=60,
            unit="exacly-test",
            forward_env=[name],
        )


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, 601])
def test_unit_runtime_deadline_is_finite(timeout: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deadline"):
        resources.unit_command(
            ["/usr/bin/python3"], cwd=tmp_path, bounds=resources.PILOT_BOUNDS, timeout=timeout, unit="exacly-test"
        )


def descriptor() -> dict[str, Any]:
    identity = {
        "schema_version": resources.VERSION,
        "backend": resources.BACKEND,
        "boot_id": "673072d4-1f12-43f6-a126-e947a82513ce",
        "cgroup": "/system.slice/exacly-test.service",
        "mount_id": "10",
        "mount_device": "0:20",
        "device": 20,
        "inode": 30,
        "caps": asdict(resources.PILOT_BOUNDS),
        "bounds": asdict(resources.PILOT_BOUNDS),
    }
    return resources.Guard(resources.PILOT_BOUNDS, json.dumps(identity).encode()).descriptor


@pytest.mark.parametrize("change", ["extra", "digest", "version", "infinite", "boolean", "broader-cap", "root"])
def test_native_resource_descriptor_is_closed_and_binds_finite_approved_identity(change: str) -> None:
    value = descriptor()
    assert resources.from_descriptor(value).descriptor == value
    if change == "extra":
        value["disable_guard"] = True
    elif change == "digest":
        value["digest"] = "sha256:" + "f" * 64
    elif change == "version":
        value["schema_version"] = "999"
    else:
        identity = value["identity"]
        if change == "root":
            identity["cgroup"] = "/"
        else:
            identity["caps"]["pids"] = {"infinite": "max", "boolean": True, "broader-cap": 129}[change]
        value["digest"] = planning.digest(identity)
    with pytest.raises(ValueError):
        resources.from_descriptor(value)


def test_direct_native_apply_cannot_bypass_resource_policy_or_reuse_grant_under_broader_bounds(
    dispatch_scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "unused-runtime"
    root.mkdir(mode=0o700)
    runtime = isolation.Runtime(root, "sha256:" + "a" * 64, "/usr/bin/python3")
    case = make_executor_scenario(dispatch_scenario, runtime, tmp_path)
    value = descriptor()
    actual = copy.deepcopy(value["identity"])
    monkeypatch.setattr(resources, "_facts", lambda bounds, *_: {**actual, "bounds": asdict(bounds)})
    request = replace(case.request, executor_policy={**case.policy, "resource_guard": value})
    grant = executor.preview(case.documents, case.state_path, request)["authorization_template"]
    broader = resources.capture(
        replace(resources.PILOT_BOUNDS, memory_bytes=2 * 1024**3), expected_cgroup=actual["cgroup"]
    )
    changed = replace(request, executor_policy={**request.executor_policy, "resource_guard": broader.descriptor})
    before = case.authoritative_bytes(), case.journal.read_bytes()
    with pytest.raises(ValueError, match="authorization differs"):
        executor.apply(case.journal, case.ledger, case.documents, case.state_path, changed, authorization=grant)
    assert (case.authoritative_bytes(), case.journal.read_bytes()) == before
    actual["inode"] += 1
    with pytest.raises(ValueError, match="identity or caps changed"):
        executor.apply(case.journal, case.ledger, case.documents, case.state_path, request, authorization=grant)
    assert (case.authoritative_bytes(), case.journal.read_bytes()) == before
