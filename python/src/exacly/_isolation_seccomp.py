"""Fixed native Linux syscall policy for the Bubblewrap boundary.

Use the operating system's libseccomp to compile architecture guards rather than maintaining
syscall-number or compatibility-ABI tables. This is a code-owned policy, with no caller overrides.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

DENIED = ("keyctl", "add_key", "request_key", "io_uring_setup", "io_uring_register", "io_uring_enter")
SOCKET_FAMILIES = {1: "AF_UNIX", 2: "AF_INET", 10: "AF_INET6"}
_ALLOW = 0x7FFF0000
_ERRNO = 0x00050000
_KILL_PROCESS = 0x80000000
_BAD_ARCH = 2
_COMPARE_NE = 1
_COMPARE_EQ = 4
_COMPARE_GT = 6
# Linux's stable UAPI values; Python 3.12 does not expose these on all builds.
_ADD_SEALS = 1033
_ALL_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008


class _Comparison(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]


@dataclass(frozen=True)
class NativeFilter:
    fd: int
    digest: str
    syscalls: dict[str, int]

    @property
    def description(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "denied_syscalls": list(DENIED),
            "socket_families": list(SOCKET_FAMILIES.values()),
            "socketpair_families": [SOCKET_FAMILIES[1]],
            "native_abi_only": True,
        }


def _check(result: int) -> None:
    if result < 0:
        raise ValueError(f"cannot compile the required syscall policy: {os.strerror(-result)}")


@contextmanager
def native_filter() -> Iterator[NativeFilter]:
    """Yield a sealed BPF descriptor; reject unsupported libraries or policies before launch."""
    library: Any = ctypes.CDLL("libseccomp.so.2")
    signatures: dict[str, tuple[list[Any], Any]] = {
        "seccomp_init": ([ctypes.c_uint32], ctypes.c_void_p),
        "seccomp_release": ([ctypes.c_void_p], None),
        "seccomp_attr_set": ([ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32], ctypes.c_int),
        "seccomp_syscall_resolve_name": ([ctypes.c_char_p], ctypes.c_int),
        "seccomp_rule_add_array": (
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Comparison)],
            ctypes.c_int,
        ),
        "seccomp_export_bpf": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    }
    for name, (arguments, result_type) in signatures.items():
        try:
            function = getattr(library, name)
        except AttributeError as error:
            raise ValueError("the installed libseccomp lacks a required compiler interface") from error
        function.argtypes, function.restype = arguments, result_type
    context = library.seccomp_init(_ALLOW)
    if not context:
        raise ValueError("cannot initialize the required syscall policy")
    fd: int | None = None
    try:
        # libseccomp starts with only the native ABI. Unsupported architectures (including x32
        # on an x86-64 installation) must terminate rather than bypass native syscall rules.
        _check(library.seccomp_attr_set(context, _BAD_ARCH, _KILL_PROCESS))
        names = (*DENIED, "socket", "socketpair")
        syscalls = {name: int(library.seccomp_syscall_resolve_name(name.encode())) for name in names}
        if any(number < 0 for number in syscalls.values()):
            raise ValueError("the installed libseccomp cannot identify all required native syscalls")

        def deny(name: str, error: int, comparison: _Comparison | None = None) -> None:
            pointer = ctypes.pointer(comparison) if comparison is not None else None
            _check(
                library.seccomp_rule_add_array(
                    context, _ERRNO | error, syscalls[name], int(comparison is not None), pointer
                )
            )

        for name in DENIED:
            deny(name, errno.EPERM)
        # One comparison per rule keeps the policy straightforward and portable across mature
        # libseccomp versions. Values above the largest allowed family are denied as well, so
        # adding a future kernel address family cannot silently expand candidate capability.
        maximum = max(SOCKET_FAMILIES)
        deny("socket", errno.EAFNOSUPPORT, _Comparison(0, _COMPARE_GT, maximum, 0))
        for family in range(maximum + 1):
            if family not in SOCKET_FAMILIES:
                deny("socket", errno.EAFNOSUPPORT, _Comparison(0, _COMPARE_EQ, family, 0))
        deny("socketpair", errno.EAFNOSUPPORT, _Comparison(0, _COMPARE_NE, 1, 0))
        fd = os.memfd_create("exacly-isolation-syscalls", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        _check(library.seccomp_export_bpf(context, fd))
        encoded = os.pread(fd, 1024 * 1024 + 1, 0)
        if not encoded or len(encoded) > 1024 * 1024:
            raise ValueError("compiled syscall policy has an unsupported size")
        fcntl.fcntl(fd, _ADD_SEALS, _ALL_SEALS)
        os.lseek(fd, 0, os.SEEK_SET)
        yield NativeFilter(fd, "sha256:" + hashlib.sha256(encoded).hexdigest(), syscalls)
    finally:
        if fd is not None:
            os.close(fd)
        library.seccomp_release(context)
