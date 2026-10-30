"""Real kernel regressions for credential and socket interfaces outside filesystem namespaces.

The keyring canary belongs to a separate helper process, never the test runner's actual keyrings.
These tests require the supported runtime and do not replace missing kernel enforcement with mocks.
"""

from __future__ import annotations

import ctypes
import dataclasses
import errno
import json
import platform
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from exacly import _isolation_seccomp, isolation
from isolation_fixtures import isolation_runtime as isolation_runtime


def native_syscalls(*names: str) -> dict[str, int]:
    library = ctypes.CDLL("libseccomp.so.2")
    resolve = library.seccomp_syscall_resolve_name
    resolve.argtypes = [ctypes.c_char_p]
    resolve.restype = ctypes.c_int
    result = {name: int(resolve(name.encode())) for name in names}
    assert all(number >= 0 for number in result.values()), result
    return result


def candidate(
    runtime: isolation.Runtime, tmp_path: Path, script: str, parameters: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    out = tmp_path / "bundle"
    report = isolation.run(
        runtime,
        [runtime.python, "-I", "-S", "/inputs/probe.py"],
        {"probe.py": script.encode(), "parameters.json": json.dumps(parameters).encode()},
        out,
        timeout=10,
    )
    assert report["status"] == "completed", (report, (out / "stderr.bin").read_bytes())
    assert report["isolation"]["namespace_terminated"] is True
    return report, dict(json.loads((out / "artifacts/result.json").read_bytes()))


def test_compiled_filter_denies_performance_monitoring_in_a_disposable_process() -> None:
    # Null attributes cannot open a performance event, even on a permissive host. Installing
    # the actual compiled filter affects only this child, never the test runner or host policy.
    # An outer host filter may already return EPERM; that is not a reason to skip this check.
    script = """
import ctypes,json,os,sys
class Program(ctypes.Structure):
    _fields_=[('length',ctypes.c_ushort),('instructions',ctypes.c_void_p)]
libc=ctypes.CDLL(None,use_errno=True)
libc.syscall.restype=ctypes.c_long
libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
def attempt():
    ctypes.set_errno(0)
    value=libc.syscall(int(sys.argv[2]),ctypes.c_void_p(None),-1,0,-1,0)
    return {'value':value,'errno':ctypes.get_errno()}
before=attempt()
encoded=os.pread(int(sys.argv[1]),1024*1024,0)
assert encoded and len(encoded)%8==0
instructions=ctypes.create_string_buffer(encoded)
program=Program(len(encoded)//8,ctypes.addressof(instructions))
assert libc.prctl(38,1,0,0,0)==0,ctypes.get_errno()
assert libc.prctl(22,2,ctypes.addressof(program),0,0)==0,ctypes.get_errno()
print(json.dumps({'before':before,'filtered':attempt()}))
"""
    with _isolation_seccomp.native_filter() as policy:
        assert "perf_event_open" in policy.description["denied_syscalls"]
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script, str(policy.fd), str(policy.syscalls["perf_event_open"])],
            pass_fds=(policy.fd,),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr
    facts = json.loads(completed.stdout)
    assert facts["before"]["value"] == -1, facts
    assert facts["filtered"] == {"value": -1, "errno": errno.EPERM}, facts


def test_candidate_and_descendant_cannot_open_performance_monitoring_events(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    script = """
import ctypes,json,os
number=json.load(open('/inputs/parameters.json'))['number']
libc=ctypes.CDLL(None,use_errno=True)
libc.syscall.restype=ctypes.c_long
def attempts():
    results={}
    for name,pid,cpu in [('current_task',0,-1),('system_wide',-1,0),('namespace_init',1,-1)]:
        ctypes.set_errno(0)
        value=libc.syscall(number,ctypes.c_void_p(None),pid,cpu,-1,0)
        results[name]={'value':value,'errno':ctypes.get_errno()}
    return results
own=attempts()
child=os.fork()
if child==0:
    open('/outputs/descendant.json','w').write(json.dumps(attempts()))
    os._exit(0)
_,status=os.waitpid(child,0)
assert os.waitstatus_to_exitcode(status)==0
open('/outputs/result.json','w').write(json.dumps({
  'candidate':own,'descendant':json.load(open('/outputs/descendant.json'))}))
"""
    # Every probe has null event attributes, so omitting the denial cannot start monitoring.
    report, facts = candidate(
        isolation_runtime, tmp_path, script, {"number": native_syscalls("perf_event_open")["perf_event_open"]}
    )
    assert report["isolation"]["performance_monitoring_denied"] is True
    assert "perf_event_open" in report["isolation"]["syscall_policy"]["denied_syscalls"]
    expected = {name: {"value": -1, "errno": errno.EPERM} for name in ("current_task", "system_wide", "namespace_init")}
    assert facts == {"candidate": expected, "descendant": expected}


def test_failed_performance_monitoring_probe_blocks_candidate_in_the_actual_launch(
    isolation_runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compile_filter = _isolation_seccomp.native_filter
    getpid = native_syscalls("getpid")["getpid"]

    @contextmanager
    def successful_syscall_as_performance_probe() -> Iterator[_isolation_seccomp.NativeFilter]:
        with compile_filter() as policy:
            # Fault-inject a harmless successful syscall into the trusted probe configuration.
            # This makes the real bootstrap observe a missing denial regardless of host perf
            # restrictions, without removing another launch protection or creating an event.
            yield dataclasses.replace(policy, syscalls={**policy.syscalls, "perf_event_open": getpid})

    monkeypatch.setattr(_isolation_seccomp, "native_filter", successful_syscall_as_performance_probe)
    with pytest.raises(isolation.IsolationError, match="performance monitoring is not denied"):
        isolation.run(
            isolation_runtime,
            [isolation_runtime.python, "-c", "open('/outputs/candidate-started','w').write('started')"],
            {},
            tmp_path / "bundle",
            timeout=5,
        )
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.glob(".exacly-isolation-*"))


def test_candidate_and_descendant_cannot_read_or_change_operator_keyring_credentials(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    script = """
import ctypes,json,os
parameters=json.load(open('/inputs/parameters.json'))
numbers=parameters['numbers']
libc=ctypes.CDLL(None,use_errno=True)
libc.syscall.restype=ctypes.c_long
def call(name,*arguments):
    ctypes.set_errno(0)
    value=libc.syscall(numbers[name],*arguments)
    return {'value':value,'errno':ctypes.get_errno()}
def attempts():
    buffer=ctypes.create_string_buffer(256)
    return {
      'read':call('keyctl',11,parameters['key'],buffer,256),
      'session_read':call('keyctl',11,-3,buffer,256),
      'search':call('keyctl',10,parameters['ring'],b'user',parameters['name'].encode(),0),
      'update':call('keyctl',2,parameters['key'],b'candidate-change',16),
      'add':call('add_key',b'user',b'candidate-key',b'candidate-change',16,parameters['ring']),
      'request':call('request_key',b'user',parameters['name'].encode(),None,0),
    }
own=attempts()
child=os.fork()
if child==0:
    open('/outputs/descendant.json','w').write(json.dumps(attempts()))
    os._exit(0)
_,status=os.waitpid(child,0)
assert os.waitstatus_to_exitcode(status)==0
with open('/proc/keys') as stream:
    description_visible=parameters['name'] in stream.read()
with open('/proc/key-users') as stream:
    key_users_empty=not stream.read().strip()
open('/outputs/result.json','w').write(json.dumps({
  'candidate':own,'descendant':json.load(open('/outputs/descendant.json')),
  'operator_description_visible':description_visible,'key_users_empty':key_users_empty}))
"""
    # Joining a session keyring affects this fresh helper only. The real candidate inherits it,
    # reproducing the original credential leak without changing the test runner's keyrings.
    helper = """
import ctypes,json,sys,uuid
from pathlib import Path
request=json.load(sys.stdin)
sys.path.insert(0,request['package_root'])
from exacly import isolation
numbers=request['numbers']
libc=ctypes.CDLL(None,use_errno=True)
libc.syscall.restype=ctypes.c_long
ring=libc.syscall(numbers['keyctl'],1,None)
assert ring>0,('private session keyring unavailable',ctypes.get_errno())
name='exacly-synthetic-credential-'+uuid.uuid4().hex
secret=b'exacly-private-synthetic-keyring-canary'
key=libc.syscall(numbers['add_key'],b'user',name.encode(),secret,len(secret),ring)
assert key>0,('synthetic key unavailable',ctypes.get_errno())
buffer=ctypes.create_string_buffer(256)
assert libc.syscall(numbers['keyctl'],11,key,buffer,256)==len(secret)
assert buffer.raw[:len(secret)]==secret
runtime=isolation.Runtime(Path(request['rootfs']),request['digest'],request['python'],Path(request['bubblewrap']))
out=Path(request['out'])
parameters={'numbers':numbers,'ring':ring,'key':key,'name':name}
report=isolation.run(runtime,[runtime.python,'-I','-S','/inputs/probe.py'],
  {'probe.py':request['script'].encode(),'parameters.json':json.dumps(parameters).encode()},out,timeout=10)
assert report['status']=='completed',(report,(out/'stderr.bin').read_bytes())
assert report['isolation']['namespace_terminated'] is True
unchanged=libc.syscall(numbers['keyctl'],11,key,buffer,256)==len(secret) and buffer.raw[:len(secret)]==secret
print(json.dumps({'attempts':json.loads((out/'artifacts/result.json').read_bytes()),'unchanged':unchanged}))
"""
    request = {
        "rootfs": str(isolation_runtime.rootfs),
        "digest": isolation_runtime.rootfs_digest,
        "python": isolation_runtime.python,
        "bubblewrap": str(isolation_runtime.bubblewrap),
        "out": str(tmp_path / "bundle"),
        "numbers": native_syscalls("keyctl", "add_key", "request_key"),
        "script": script,
        "package_root": str(Path(isolation.__file__).resolve().parent.parent),
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-c", helper],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    facts = json.loads(completed.stdout)
    for process in ("candidate", "descendant"):
        assert facts["attempts"][process] == {
            name: {"value": -1, "errno": errno.EPERM}
            for name in ("read", "session_read", "search", "update", "add", "request")
        }
    assert facts["attempts"]["operator_description_visible"] is False
    assert facts["attempts"]["key_users_empty"] is True
    assert facts["unchanged"] is True


def test_kernel_policy_restricts_socket_families_and_io_uring(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    script = """
import ctypes,json,os,socket
numbers=json.load(open('/inputs/parameters.json'))['numbers']
libc=ctypes.CDLL(None,use_errno=True)
libc.syscall.restype=ctypes.c_long
def invoke(name,*arguments):
    ctypes.set_errno(0)
    value=libc.syscall(numbers[name],*arguments)
    return value,ctypes.get_errno()
allowed={}
for name,family in [('unix',socket.AF_UNIX),('ipv4',socket.AF_INET),('ipv6',socket.AF_INET6)]:
    value,error=invoke('socket',family,socket.SOCK_STREAM,0)
    allowed[name]={'created':value>=0,'errno':error}
    if value>=0:os.close(value)
unsupported={}
for family in (0,3,9,16,17,31,40,42,1000):
    value,error=invoke('socket',family,socket.SOCK_STREAM,0)
    unsupported[str(family)]={'value':value,'errno':error}
    if value>=0:os.close(value)
pairs={}
for family in (socket.AF_UNIX,socket.AF_INET,socket.AF_INET6,40):
    descriptors=(ctypes.c_int*2)()
    value,error=invoke('socketpair',family,socket.SOCK_STREAM,0,descriptors)
    pairs[str(family)]={'value':value,'errno':error}
    if value==0:
        for descriptor in descriptors:os.close(descriptor)
parameters=ctypes.create_string_buffer(256)
uring={}
for name,arguments in [('io_uring_setup',(8,parameters)),('io_uring_register',(-1,0,0,0)),
                       ('io_uring_enter',(-1,0,0,0,0,0))]:
    value,error=invoke(name,*arguments)
    uring[name]={'value':value,'errno':error}
    if name=='io_uring_setup' and value>=0:os.close(value)
open('/outputs/result.json','w').write(json.dumps({
  'allowed':allowed,'unsupported':unsupported,'socketpairs':pairs,'io_uring':uring}))
"""
    _, facts = candidate(
        isolation_runtime,
        tmp_path,
        script,
        {"numbers": native_syscalls("socket", "socketpair", "io_uring_setup", "io_uring_register", "io_uring_enter")},
    )
    assert facts["allowed"] == {name: {"created": True, "errno": 0} for name in ("unix", "ipv4", "ipv6")}
    assert facts["unsupported"] == {
        str(family): {"value": -1, "errno": errno.EAFNOSUPPORT} for family in (0, 3, 9, 16, 17, 31, 40, 42, 1000)
    }
    assert facts["socketpairs"] == {
        "1": {"value": 0, "errno": 0},
        **{str(family): {"value": -1, "errno": errno.EAFNOSUPPORT} for family in (2, 10, 40)},
    }
    assert facts["io_uring"] == {
        name: {"value": -1, "errno": errno.EPERM} for name in ("io_uring_setup", "io_uring_register", "io_uring_enter")
    }


if platform.machine() in ("x86_64", "amd64"):

    def test_non_native_i386_and_x32_syscalls_are_rejected(
        isolation_runtime: isolation.Runtime, tmp_path: Path
    ) -> None:
        probe = """
import ctypes,json,mmap,os,resource
results={}
for name,code in [('i386',bytes.fromhex('b814000000cd80c3')),
                  ('x32',bytes.fromhex('b8270000400f05c3'))]:
    child=os.fork()
    if child==0:
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        memory=mmap.mmap(-1,len(code),prot=mmap.PROT_READ|mmap.PROT_WRITE|mmap.PROT_EXEC)
        memory.write(code)
        function=ctypes.CFUNCTYPE(ctypes.c_long)(ctypes.addressof(ctypes.c_char.from_buffer(memory)))
        function()
        os._exit(73)
    _,status=os.waitpid(child,0)
    results[name]={'signal':os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
                   'exit':os.WEXITSTATUS(status) if os.WIFEXITED(status) else None}
"""
        control = subprocess.run(
            [sys.executable, "-I", "-c", probe + "\nprint(json.dumps(results))"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        baseline = json.loads(control.stdout)
        script = probe + "\nopen('/outputs/result.json','w').write(json.dumps(results))"
        _, facts = candidate(isolation_runtime, tmp_path, script, {})
        assert facts["x32"] == {"signal": signal.SIGSYS, "exit": None}
        # Some supported hosts have no IA32 emulation: int 0x80 faults before it can reach
        # seccomp. On hosts that execute the ABI, the native-only filter must kill it instead.
        expected = signal.SIGSEGV if baseline["i386"]["signal"] == signal.SIGSEGV else signal.SIGSYS
        assert facts["i386"] == {"signal": expected, "exit": None}


def test_missing_syscall_filter_blocks_candidate_in_the_actual_launch(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    wrapper = tmp_path / "bwrap-without-seccomp"
    wrapper.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys\n"
        "args=sys.argv[1:]\n"
        "if '--seccomp' in args:\n"
        "    index=args.index('--seccomp')\n"
        "    os.close(int(args[index+1]))\n"
        "    del args[index:index+2]\n"
        f"os.execv({str(isolation_runtime.bubblewrap)!r},[{str(isolation_runtime.bubblewrap)!r},*args])\n"
    )
    wrapper.chmod(0o700)
    runtime = dataclasses.replace(isolation_runtime, bubblewrap=wrapper)
    marker = tmp_path / "candidate-marker"
    with pytest.raises(isolation.IsolationError):
        isolation.run(
            runtime,
            [runtime.python, "-c", f"open({str(marker)!r},'w').write('started')"],
            {},
            tmp_path / "bundle",
            timeout=5,
        )
    assert not marker.exists()
    assert not (tmp_path / "bundle").exists()
