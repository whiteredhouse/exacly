"""Fail fast when the CI host cannot construct the real Linux isolation boundary.

These trusted probes change only their own newly created namespaces. They do not change host
kernel settings, AppArmor policy or the product's independent per-launch verification.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

CHILD = r"""
import errno,fcntl,json,os,socket,struct
facts={}
try:
    request=struct.pack('256s',b'lo')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as control:
        flags=fcntl.ioctl(control.fileno(),0x8913,request)
        facts['loopback_flags']=struct.unpack_from('H',flags,16)[0]
        for name,operation in [('address',0x8915),('netmask',0x891b)]:
            try:
                value=fcntl.ioctl(control.fileno(),operation,request)
                facts['loopback_'+name]=socket.inet_ntoa(value[20:24])
            except OSError as error:
                facts['loopback_'+name]={'errno':error.errno,'name':errno.errorcode.get(error.errno)}
    index=socket.if_nametoindex('lo')
    pid=os.getpid()
    address=socket.inet_aton('127.0.0.1')
    payload=struct.pack('BBBBI',socket.AF_INET,8,0x80,254,index)
    payload+=struct.pack('HH',8,2)+address
    payload+=struct.pack('HH',8,1)+address
    packet=struct.pack('IHHII',16+len(payload),20,1|4|0x200|0x400,1,pid)+payload
    with socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,0) as route:
        route.settimeout(2)
        route.bind((pid,0))
        route.sendto(packet,(0,0))
        response=route.recv(4096)
    offset=0
    while offset+16<=len(response):
        length,kind,flags,sequence,reply_pid=struct.unpack_from('IHHII',response,offset)
        if length<16 or offset+length>len(response):
            raise ValueError('malformed netlink reply')
        if kind==2 and sequence==1:
            result=struct.unpack_from('i',response,offset+16)[0]
            actual=-result
            facts['exclusive_RTM_NEWADDR']={'errno':actual,'name':errno.errorcode.get(actual,'SUCCESS')}
            break
        offset+=(length+3)&~3
    else:
        facts['exclusive_RTM_NEWADDR']={'error':'no matching NLMSG_ERROR acknowledgement'}
except (OSError,ValueError) as error:
    facts['probe_error']={'type':type(error).__name__,'message':str(error),'errno':getattr(error,'errno',None)}
print(json.dumps(facts,sort_keys=True))
"""


def read(path: str, limit: int = 1024) -> str:
    try:
        with Path(path).open() as stream:
            return stream.read(limit).strip()
    except OSError as error:
        return f"unavailable: {error.strerror}"


def probe(argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {"exit_code": completed.returncode, "stdout": completed.stdout[:4096], "stderr": completed.stderr[:4096]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"exit_code": None, "error": str(error)[:1024]}


def main() -> int:
    report: dict[str, Any] = {
        "platform": platform.uname()._asdict(),
        "uid": os.getuid(),
        "apparmor_context": read("/proc/self/attr/current"),
        "apparmor_restrict_unprivileged_userns": read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
        "bubblewrap_profiles": {
            name: (Path("/etc/apparmor.d") / name).exists()
            for name in ("bwrap", "usr.bin.bwrap", "bubblewrap", "exacly-bwrap")
        },
        "bubblewrap_version": probe(["/usr/bin/bwrap", "--version"]),
        "fresh_network_namespace": probe(
            ["/usr/bin/unshare", "--user", "--map-root-user", "--net", "/usr/bin/python3", "-I", "-S", "-c", CHILD]
        ),
    }
    # The executable here is trusted system true, never candidate-controlled code. These
    # read-only runtime mounts allow its loader to start without exposing a writable host tree.
    report["strict_bubblewrap"] = probe(
        [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--disable-userns",
            "--assert-userns-disabled",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--symlink",
            "usr/bin",
            "/bin",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/usr/bin/true",
        ]
    )
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    return 0 if report["strict_bubblewrap"]["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
