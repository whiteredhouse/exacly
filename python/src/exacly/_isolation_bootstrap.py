"""Trusted, standalone bootstrap copied into one isolated runtime before candidate execution.

This file deliberately imports no Exacly or candidate module. Its stdout/stdin protocol ends before
the candidate is executed; subsequent logs and filesystem outputs are untrusted candidate data.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import resource
import stat
import sys

NAMESPACES = ("user", "mnt", "pid", "net", "ipc", "uts")
CAPABILITIES = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")


def require(value: bool, message: str) -> None:
    if not value:
        raise RuntimeError(message)


def main() -> None:
    with open("/run/exacly/config.json", encoding="utf-8") as stream:
        config = json.load(stream)
    namespaces = {name: os.readlink("/proc/self/ns/" + name) for name in NAMESPACES}
    require(all(namespaces[name] != config["host_namespaces"][name] for name in NAMESPACES), "namespace not isolated")
    with open("/proc/self/status", encoding="utf-8") as stream:
        status = dict(line.split(":", 1) for line in stream if ":" in line)
    capabilities = {key: status[key].strip() for key in CAPABILITIES}
    require(all(int(value, 16) == 0 for value in capabilities.values()), "capabilities remain available")
    require(status["NoNewPrivs"].strip() == "1", "privilege escalation is enabled")
    require(status["Seccomp"].strip() == "2", "syscall policy is not enforced")
    require(
        int(status["Seccomp_filters"].strip()) > config["host_seccomp_filters"],
        "required syscall policy was not installed for this launch",
    )
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    for name in ("keyctl", "io_uring_setup"):
        ctypes.set_errno(0)
        require(
            libc.syscall(config["syscalls"][name], 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM,
            "required privileged syscall denial is missing",
        )
    ctypes.set_errno(0)
    require(
        libc.syscall(config["syscalls"]["socket"], 40, 1, 0) == -1 and ctypes.get_errno() == errno.EAFNOSUPPORT,
        "unsupported socket family is not denied",
    )
    require(os.getppid() == 1 and os.getpid() != 1, "private PID namespace init is missing")
    require(os.statvfs("/").f_flag & os.ST_RDONLY != 0, "runtime root is writable")
    require(os.statvfs("/inputs").f_flag & os.ST_RDONLY != 0, "input snapshot is writable")
    require(os.statvfs("/outputs").f_flag & os.ST_RDONLY == 0, "output staging is not writable")
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        mounts = {parts[4]: parts for line in stream if (parts := line.split())}
    require("/proc" in mounts and mounts["/proc"][mounts["/proc"].index("-") + 1] == "proc", "private procfs missing")
    for path in ("/dev", "/tmp", "/work", "/outputs"):
        require(
            path in mounts and mounts[path][mounts[path].index("-") + 1] == "tmpfs", "private runtime mount missing"
        )
    for path in ("/proc/keys", "/proc/key-users"):
        with open(path, "rb") as stream:
            require(stream.read(1) == b"", "kernel keyring metadata is not masked")
    # os.listdir temporarily opens /proc/self/fd; ignore that now-closed descriptor, then verify
    # every remaining live descriptor. No host descriptor is retained for candidate execution.
    descriptors = []
    for name in os.listdir("/proc/self/fd"):
        try:
            os.fstat(int(name))
        except OSError:
            continue
        descriptors.append(int(name))
    require(sorted(descriptors) == [0, 1, 2], "unexpected inherited descriptor")
    require(stat.S_ISCHR(os.stat("/dev/null").st_mode), "private null device missing")
    facts = {
        "verified": True,
        "namespaces": namespaces,
        "capabilities": capabilities,
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
    sys.stdout.write(json.dumps(facts, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    # A trusted parent validates the exact launched process before releasing its command. EOF,
    # malformed release or setup failure never starts candidate-controlled code.
    require(sys.stdin.buffer.readline(64) == b"EXACLY-RUN\n", "candidate release was not authorized")
    null = os.open("/dev/null", os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (config["file_size_limit"], config["file_size_limit"]))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    os.umask(0o077)
    os.chdir("/work")
    os.execvpe(config["argv"][0], config["argv"], config["environment"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print("isolation bootstrap: " + str(error), file=sys.stderr)
        raise SystemExit(125) from None
