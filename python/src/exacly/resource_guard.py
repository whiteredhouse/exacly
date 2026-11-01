"""Kernel-checked cgroup v2 bounds for the reviewed local worker deployment.

The trusted operator provisions a transient systemd service. The guard inspects actual kernel
limits and membership, rather than trusting setup exit status or candidate resource claims. This
is one-machine containment; a new boot, cgroup or runner requires a separately approved contract.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import stat
import subprocess
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from exacly import planning

VERSION = "0.1.0"
BACKEND = "linux-cgroup2-v1"
_ROOT = Path("/sys/fs/cgroup")
_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_FORBIDDEN_ENV = {
    "PATH",
    "HOME",
    "SHELL",
    "ENV",
    "BASH_ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "IFS",
    "CDPATH",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SYSTEMD_BUS_ADDRESS",
    "SYSTEMD_UNIT_PATH",
}


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Bounds:
    """Finite approved aggregate ceilings; CPU is an exact quota/period ratio."""

    cpu_quota_us: int
    cpu_period_us: int
    memory_bytes: int
    memory_swap_bytes: int
    pids: int

    def __post_init__(self) -> None:
        _require(
            all(type(value) is int and 0 <= value <= 2**63 - 1 for value in asdict(self).values()),
            "resource bounds must be finite nonnegative integers",
        )
        _require(
            self.cpu_quota_us > 0 and 1000 <= self.cpu_period_us <= 1000000 and self.memory_bytes > 0 and self.pids > 0,
            "resource CPU, memory and process bounds must be positive",
        )


PILOT_BOUNDS = Bounds(100000, 100000, 1024**3, 0, 128)


def _read(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _require(stat.S_ISREG(os.fstat(descriptor).st_mode), "resource fact is not a kernel file")
        data = bytearray()
        while block := os.read(descriptor, min(65536, 65537 - len(data))):
            data.extend(block)
            _require(len(data) <= 65536, "resource fact exceeds its bound")
        return bytes(data).decode("ascii").strip()
    finally:
        os.close(descriptor)


def _membership(text: str) -> str:
    lines = text.splitlines()
    _require(len(lines) == 1 and lines[0].startswith("0::/"), "a unified cgroup v2 membership is required")
    path = lines[0][3:]
    _require(
        str(PurePosixPath(path)) == path and path.startswith("/") and ".." not in PurePosixPath(path).parts,
        "invalid kernel cgroup path",
    )
    return path


def _finite(value: str, label: str) -> int:
    _require(re.fullmatch(r"[0-9]{1,19}", value) is not None, "resource " + label + " must have an actual finite cap")
    result = int(value)
    _require(result <= 2**63 - 1, "resource " + label + " exceeds its supported bound")
    return result


def _caps(values: dict[str, str], bounds: Bounds) -> dict[str, int]:
    cpu = values["cpu.max"].split()
    _require(len(cpu) == 2, "invalid kernel CPU quota")
    actual = {
        "cpu_quota_us": _finite(cpu[0], "CPU quota"),
        "cpu_period_us": _finite(cpu[1], "CPU period"),
        "memory_bytes": _finite(values["memory.max"], "memory"),
        "memory_swap_bytes": _finite(values["memory.swap.max"], "swap"),
        "pids": _finite(values["pids.max"], "process count"),
    }
    _require(
        actual["cpu_quota_us"] > 0
        and actual["cpu_period_us"] > 0
        and actual["memory_bytes"] > 0
        and actual["pids"] > 0,
        "actual CPU, memory and process caps must be positive",
    )
    _require(
        actual["cpu_quota_us"] * bounds.cpu_period_us <= bounds.cpu_quota_us * actual["cpu_period_us"]
        and actual["memory_bytes"] <= bounds.memory_bytes
        and actual["memory_swap_bytes"] <= bounds.memory_swap_bytes
        and actual["pids"] <= bounds.pids,
        "actual resource caps exceed independently approved bounds",
    )
    return actual


def _facts(bounds: Bounds, expected_cgroup: str) -> dict[str, Any]:
    _require(isinstance(bounds, Bounds), "explicit approved resource bounds are required")
    _require(
        isinstance(expected_cgroup, str)
        and _membership("0::" + expected_cgroup) == expected_cgroup
        and expected_cgroup != "/",
        "an exact non-root coordination cgroup is required",
    )
    membership = _membership(_read(Path("/proc/self/cgroup")))
    _require(membership == expected_cgroup, "coordinator cgroup differs from the approved unit")
    mounts = []
    for line in _read(Path("/proc/self/mountinfo")).splitlines():
        fields = line.split()
        separator = fields.index("-") if "-" in fields else -1
        if separator > 5 and fields[separator + 1] == "cgroup2" and fields[4] == str(_ROOT):
            mounts.append(fields)
    _require(len(mounts) == 1 and mounts[0][3] == "/", "a complete canonical cgroup v2 mount is required")
    root = _ROOT / membership.lstrip("/")
    _require(root.resolve() == root, "resource cgroup traverses a symbolic link")
    info = root.stat(follow_symlinks=False)
    _require(stat.S_ISDIR(info.st_mode), "resource cgroup is not a directory")
    values = {name: _read(root / name) for name in ("cpu.max", "memory.max", "memory.swap.max", "pids.max")}
    boot = _read(Path("/proc/sys/kernel/random/boot_id"))
    _require(str(uuid.UUID(boot)) == boot, "invalid kernel boot identity")
    _require(
        _membership(_read(Path("/proc/self/cgroup"))) == membership, "resource membership changed during inspection"
    )
    current = root.stat(follow_symlinks=False)
    _require(
        (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), "resource cgroup changed during inspection"
    )
    return {
        "schema_version": VERSION,
        "backend": BACKEND,
        "boot_id": boot,
        "cgroup": membership,
        "mount_id": mounts[0][0],
        "mount_device": mounts[0][2],
        "device": info.st_dev,
        "inode": info.st_ino,
        "caps": _caps(values, bounds),
        "bounds": asdict(bounds),
    }


@dataclass(frozen=True)
class Guard:
    """A retained kernel identity; every use checks actual caps and membership again."""

    bounds: Bounds
    _identity: bytes

    @property
    def identity(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self._identity)
        return value

    @property
    def digest(self) -> str:
        return planning.digest(self.identity)

    @property
    def descriptor(self) -> dict[str, Any]:
        return {"schema_version": VERSION, "backend": BACKEND, "identity": self.identity, "digest": self.digest}

    def check(self) -> dict[str, Any]:
        retained = self.identity
        current = _facts(self.bounds, retained["cgroup"])
        _require(current == retained, "approved resource envelope identity or caps changed")
        return current

    def check_process(self, pid: int) -> None:
        _require(type(pid) is int and pid > 0, "an exact worker process is required")
        current = self.check()
        _require(
            _membership(_read(Path(f"/proc/{pid}/cgroup"))) == current["cgroup"],
            "worker process escaped the approved aggregate resource envelope",
        )


def capture(bounds: Bounds, *, expected_cgroup: str) -> Guard:
    """Capture actual enforced caps on the trusted-selected unit; no setup claim is accepted."""
    facts = _facts(bounds, expected_cgroup)
    return Guard(bounds, json.dumps(facts, sort_keys=True).encode())


def from_descriptor(value: Any) -> Guard:
    """Parse a closed retained policy descriptor without claiming it matches current kernel facts."""
    try:
        _require(
            isinstance(value, dict)
            and set(value) == {"schema_version", "backend", "identity", "digest"}
            and value["schema_version"] == VERSION
            and value["backend"] == BACKEND,
            "invalid resource guard descriptor",
        )
        identity = value["identity"]
        _require(
            isinstance(identity, dict)
            and set(identity)
            == {
                "schema_version",
                "backend",
                "boot_id",
                "cgroup",
                "mount_id",
                "mount_device",
                "device",
                "inode",
                "caps",
                "bounds",
            }
            and identity["schema_version"] == VERSION
            and identity["backend"] == BACKEND
            and value["digest"] == planning.digest(identity),
            "resource descriptor identity or digest changed",
        )
        _require(str(uuid.UUID(identity["boot_id"])) == identity["boot_id"], "invalid resource boot identity")
        _require(
            isinstance(identity["cgroup"], str)
            and identity["cgroup"] != "/"
            and _membership("0::" + identity["cgroup"]) == identity["cgroup"],
            "invalid resource coordination cgroup",
        )
        _require(
            isinstance(identity["mount_id"], str)
            and identity["mount_id"].isdigit()
            and isinstance(identity["mount_device"], str)
            and re.fullmatch(r"[0-9]+:[0-9]+", identity["mount_device"])
            and all(type(identity[key]) is int and identity[key] >= 0 for key in ("device", "inode")),
            "invalid physical resource mount or cgroup identity",
        )
        _require(
            isinstance(identity["bounds"], dict)
            and set(identity["bounds"]) == set(asdict(PILOT_BOUNDS))
            and isinstance(identity["caps"], dict)
            and set(identity["caps"]) == set(asdict(PILOT_BOUNDS)),
            "invalid resource bounds or caps fields",
        )
        bounds = Bounds(**identity["bounds"])
        actual = Bounds(**identity["caps"])
        _caps(
            {
                "cpu.max": f"{actual.cpu_quota_us} {actual.cpu_period_us}",
                "memory.max": str(actual.memory_bytes),
                "memory.swap.max": str(actual.memory_swap_bytes),
                "pids.max": str(actual.pids),
            },
            bounds,
        )
        return Guard(bounds, json.dumps(identity, sort_keys=True).encode())
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError("invalid retained resource guard descriptor") from error


def _environment(names: Sequence[str]) -> list[str]:
    _require(isinstance(names, (list, tuple)) and len(names) <= 32, "explicit bounded environment names are required")
    result = list(names)
    _require(len(set(result)) == len(result), "duplicate forwarded environment names")
    for name in result:
        _require(
            isinstance(name, str)
            and _ENVIRONMENT.fullmatch(name)
            and name not in _FORBIDDEN_ENV
            and not name.startswith(("LD_", "DYLD_", "PYTHON", "GIT_", "SYSTEMD_", "EXACLY_RESOURCE_")),
            "unsafe forwarded environment/control name",
        )
        _require(name in os.environ, "a trusted forwarded environment variable is unavailable")
    return result


def unit_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    bounds: Bounds,
    timeout: float,
    unit: str,
    forward_env: Sequence[str] = (),
) -> list[str]:
    """Build one explicit operator invocation, forwarding names without placing secrets in argv."""
    _require(isinstance(bounds, Bounds), "explicit approved resource bounds are required")
    _require(
        isinstance(argv, (list, tuple))
        and 0 < len(argv) <= 1024
        and all(isinstance(item, str) and "\0" not in item for item in argv)
        and Path(argv[0]).is_absolute(),
        "a bounded trusted absolute command vector is required",
    )
    _require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 600, "invalid unit deadline")
    _require(isinstance(unit, str) and re.fullmatch(r"exacly-[a-z0-9-]{1,80}", unit), "invalid trusted unit name")
    _require(
        isinstance(cwd, Path) and cwd.is_absolute() and cwd.resolve() == cwd and cwd.is_dir(),
        "invalid trusted unit cwd",
    )
    _require(os.geteuid() != 0 and os.getegid() != 0, "the resource service must run as a non-root operator")
    names = _environment(forward_env)
    # Round down the percentage so systemd can never broaden the approved quota ratio.
    percentage = bounds.cpu_quota_us * 10000 // bounds.cpu_period_us
    _require(percentage > 0, "CPU quota is below the supported percentage precision")
    properties = {
        "CPUQuota": f"{percentage // 100}.{percentage % 100:02d}%",
        "CPUQuotaPeriodSec": f"{bounds.cpu_period_us}us",
        "MemoryMax": str(bounds.memory_bytes),
        "MemorySwapMax": str(bounds.memory_swap_bytes),
        "TasksMax": str(bounds.pids),
        "RuntimeMaxSec": str(timeout),
        "KillMode": "control-group",
        "OOMPolicy": "continue",
    }
    return [
        "/usr/bin/sudo",
        "-n",
        *(["--preserve-env=" + ",".join(names)] if names else []),
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=" + unit,
        "--uid=" + str(os.geteuid()),
        "--gid=" + str(os.getegid()),
        "--working-directory=" + str(cwd),
        *("--property=" + key + "=" + value for key, value in properties.items()),
        "--setenv=EXACLY_RESOURCE_CGROUP=/system.slice/" + unit + ".service",
        *("--setenv=" + name for name in names),
        "--",
        *argv,
    ]


def run_unit(
    argv: Sequence[str],
    *,
    cwd: Path,
    bounds: Bounds,
    timeout: float,
    forward_env: Sequence[str] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run a trusted coordinator/test command in a disposable finite same-runner service.

    Candidate commands use the native isolated executor inside this service. This invocation is an
    operator action, not a provider selectable from candidate configuration. Only explicit named
    environment bindings reach the trusted service; the native launcher clears candidate env again.
    """
    unit = "exacly-resource-" + uuid.uuid4().hex
    command = unit_command(argv, cwd=cwd, bounds=bounds, timeout=timeout, unit=unit, forward_env=forward_env)
    names = _environment(forward_env)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **{name: os.environ[name] for name in names}}
    output, errors = bytearray(), bytearray()
    expires = time.monotonic() + timeout + 30
    process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    try:
        with selectors.DefaultSelector() as streams:
            for stream, target in ((process.stdout, output), (process.stderr, errors)):
                os.set_blocking(stream.fileno(), False)
                streams.register(stream, selectors.EVENT_READ, target)
            while streams.get_map():
                remaining = expires - time.monotonic()
                _require(remaining > 0, "trusted resource service exceeded its bounded deadline")
                for key, _ in streams.select(min(remaining, 0.1)):
                    block = os.read(key.fd, 65536)
                    if not block:
                        streams.unregister(key.fileobj)
                    else:
                        key.data.extend(block)
                        _require(len(key.data) <= 1024**2, "trusted resource service output exceeded its bound")
            result = process.wait(timeout=max(0.001, expires - time.monotonic()))
        return subprocess.CompletedProcess(command, result, bytes(output), bytes(errors))
    except BaseException:
        # Killing the client does not stop a service. Explicitly stop the whole named cgroup;
        # RuntimeMaxSec independently bounds it if the administrative transport is unavailable.
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", unit + ".service"],
                env={"PATH": "/usr/bin:/bin"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
