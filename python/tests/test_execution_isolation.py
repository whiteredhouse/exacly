"""REQ-025 is established by real candidate processes, descendants and failed setup attempts.

This suite deliberately fails when its supported Linux/Bubblewrap boundary is unavailable. A
skipped test, a mocked launcher or checking file modes cannot establish isolation from authority.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from exacly import isolation
from isolation_fixtures import isolation_runtime as isolation_runtime

Doc = dict[str, Any]
ROOT = Path(__file__).resolve().parents[2]


def candidate(
    runtime: isolation.Runtime,
    tmp_path: Path,
    script: str,
    *,
    inputs: dict[str, bytes] | None = None,
    timeout: float = 10,
) -> tuple[Doc, Path]:
    out = tmp_path / "bundle"
    report = isolation.run(
        runtime,
        [runtime.python, "-I", "-S", "/inputs/probe.py"],
        {"probe.py": script.encode(), **(inputs or {})},
        out,
        timeout=timeout,
    )
    return report, out


def result(report: Doc, out: Path) -> Doc:
    assert report["status"] == "completed", (report, (out / "stderr.bin").read_bytes())
    assert report["exit_code"] == 0
    return dict(json.loads((out / "artifacts/result.json").read_bytes()))


@contextmanager
def operator_process() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], stdin=subprocess.DEVNULL)
    try:
        assert process.pid > 20, "the host canary PID must not collide with the small guest PID scope"
        yield process
    finally:
        process.kill()
        process.wait(timeout=5)


def test_actual_candidate_has_private_namespaces_zero_capabilities_and_immutable_snapshots(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    before_root = isolation.rootfs_digest(isolation_runtime.rootfs)
    host_namespaces = {
        name: os.readlink(f"/proc/self/ns/{name}") for name in ("user", "mnt", "pid", "net", "ipc", "uts")
    }
    script = """
import json, os
value = open('/inputs/criteria/REQ-025.txt', 'rb').read()
try:
    open('/inputs/criteria/REQ-025.txt', 'wb').write(b'changed')
    immutable = False
except OSError:
    immutable = True
status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)
facts = {
    'input': value.decode(), 'immutable': immutable,
    'namespaces': {x: os.readlink('/proc/self/ns/'+x) for x in ('user','mnt','pid','net','ipc','uts')},
    'caps': {x: status[x].strip() for x in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')},
    'no_new_privs': status['NoNewPrivs'].strip(),
    'root_readonly': bool(os.statvfs('/').f_flag & os.ST_RDONLY),
    'inputs_readonly': bool(os.statvfs('/inputs').f_flag & os.ST_RDONLY),
    'visible_pids': [x for x in os.listdir('/proc') if x.isdigit()],
}
open('/outputs/result.json','w').write(json.dumps(facts))
open('/outputs/approved-copy.txt','wb').write(value)
"""
    report, out = candidate(
        isolation_runtime, tmp_path, script, inputs={"criteria/REQ-025.txt": b"approved immutable criterion"}
    )
    facts = result(report, out)
    assert facts["input"] == "approved immutable criterion"
    assert facts["immutable"] is True
    assert facts["root_readonly"] is True and facts["inputs_readonly"] is True
    assert facts["no_new_privs"] == "1"
    assert set(facts["caps"].values()) == {"0000000000000000"}
    assert all(value != host_namespaces[name] for name, value in facts["namespaces"].items())
    assert all(int(pid) < 20 for pid in facts["visible_pids"])
    assert (out / "artifacts/approved-copy.txt").read_bytes() == b"approved immutable criterion"
    assert report["isolation"]["verified"] is True
    assert isolation.rootfs_digest(isolation_runtime.rootfs) == before_root


def test_candidate_and_descendant_cannot_reach_operator_files_fds_environment_or_process(
    isolation_runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = tmp_path / "operator-authority"
    authority.mkdir()
    canaries = {
        "ledger.sqlite3": b"operator-ledger-canary",
        "state.json": b'{"authority":"operator-state-canary"}',
        "policy.json": b'{"approval":"operator-policy-canary"}',
        "credentials": b"synthetic-privileged-credential-canary",
    }
    for name, content in canaries.items():
        (authority / name).write_bytes(content)
    secret = "synthetic-environment-secret-canary"
    for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "EXACLY_TEST_SECRET", "HTTP_PROXY", "BASH_ENV", "ENV"):
        monkeypatch.setenv(name, secret)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{authority}/control.sock")
    inherited = os.open(authority / "credentials", os.O_RDONLY)
    os.set_inheritable(inherited, True)
    try:
        with operator_process() as host:
            parameters = {
                "paths": [str(authority / name) for name in canaries],
                "host_pid": host.pid,
                "fd": inherited,
                "secret": secret,
            }
            script = """
import json, os, signal, stat, subprocess, sys
params = json.loads(open('/inputs/parameters.json').read())
def probe():
    reads = []; writes = []
    paths = list(params['paths'])
    paths += ['/proc/self/root'+x for x in params['paths']]
    paths += ['/proc/1/root'+x for x in params['paths']]
    paths += ['/proc/'+str(params['host_pid'])+'/root'+x for x in params['paths']]
    for number, path in enumerate(paths):
        link = '/tmp/link-'+str(os.getpid())+'-'+str(number)
        os.symlink(path, link)
        for target in (path, link):
            try:
                open(target,'rb').read(); reads.append(target)
            except OSError:
                pass
            try:
                open(target,'wb').write(b'candidate-change'); writes.append(target)
            except OSError:
                pass
    fd_secret = False
    for directory in ('/proc/self/fd','/proc/1/fd'):
        try:
            descriptors = os.listdir(directory)
        except OSError:
            continue
        for name in descriptors:
            descriptor = None
            try:
                descriptor = os.open(directory+'/'+name, os.O_RDONLY | os.O_NONBLOCK)
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    data = os.pread(descriptor, 4096, 0)
                    fd_secret = fd_secret or b'privileged-credential-canary' in data
            except OSError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    host_proc = os.path.exists('/proc/'+str(params['host_pid']))
    try:
        os.kill(params['host_pid'], signal.SIGUSR1); signal_allowed = True
    except OSError:
        signal_allowed = False
    return {'reads':reads,'writes':writes,'fd_secret':fd_secret,'host_proc':host_proc,
            'signal_allowed':signal_allowed,
            'environment_secret':params['secret'] in list(os.environ.values())}
if len(sys.argv) > 1:
    print(json.dumps(probe()))
else:
    parent = probe()
    child = json.loads(subprocess.check_output([sys.executable,'-I','-S',__file__,'child']))
    open('/outputs/result.json','w').write(json.dumps({'candidate':parent,'descendant':child}))
"""
            report, out = candidate(
                isolation_runtime,
                tmp_path,
                script,
                inputs={"parameters.json": json.dumps(parameters).encode()},
            )
            facts = result(report, out)
            for process in (facts["candidate"], facts["descendant"]):
                assert process == {
                    "reads": [],
                    "writes": [],
                    "fd_secret": False,
                    "host_proc": False,
                    "signal_allowed": False,
                    "environment_secret": False,
                }
            assert host.poll() is None
    finally:
        os.close(inherited)
    assert {name: (authority / name).read_bytes() for name in canaries} == canaries


def test_host_loopback_filesystem_and_abstract_control_sockets_are_unreachable(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as filesystem,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abstract,
    ):
        tcp.bind(("127.0.0.1", 0))
        filesystem_path = str(tmp_path / "operator-control.sock")
        filesystem.bind(filesystem_path)
        abstract_path = "\0exacly-operator-" + uuid.uuid4().hex
        abstract.bind(abstract_path)
        for sock in (tcp, filesystem, abstract):
            sock.listen(1)
        parameters = {"port": tcp.getsockname()[1], "filesystem": filesystem_path, "abstract": abstract_path}
        script = """
import json, socket
params=json.loads(open('/inputs/parameters.json').read())
results={}
for name,family,address in [('loopback',socket.AF_INET,('127.0.0.1',params['port'])),
                            ('filesystem',socket.AF_UNIX,params['filesystem']),
                            ('abstract',socket.AF_UNIX,params['abstract'])]:
    with socket.socket(family,socket.SOCK_STREAM) as client:
        client.settimeout(0.25)
        try:
            client.connect(address); results[name]='reachable'
        except OSError:
            results[name]='denied'
open('/outputs/result.json','w').write(json.dumps(results))
"""
        report, out = candidate(
            isolation_runtime, tmp_path, script, inputs={"parameters.json": json.dumps(parameters).encode()}
        )
        assert result(report, out) == {"loopback": "denied", "filesystem": "denied", "abstract": "denied"}
        for sock in (tcp, filesystem, abstract):
            sock.settimeout(0.01)
            with pytest.raises(TimeoutError):
                sock.accept()


def test_candidate_cannot_create_nested_user_namespaces_or_privileged_sockets(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    script = """
import ctypes,json,os,socket
libc=ctypes.CDLL(None,use_errno=True)
nested=libc.unshare(0x10000000)
try:
    os.setuid(0); root_uid=True
except OSError:
    root_uid=False
try:
    raw=socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)
    raw.close(); raw_socket=True
except OSError:
    raw_socket=False
open('/outputs/result.json','w').write(json.dumps({
    'nested_userns':nested,'root_uid':root_uid,'raw_socket':raw_socket}))
"""
    report, out = candidate(isolation_runtime, tmp_path, script)
    assert result(report, out) == {"nested_userns": -1, "root_uid": False, "raw_socket": False}


def test_output_volume_limit_is_enforced_across_multiple_files_by_the_private_filesystem(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    # Every individual write is far below the per-file limit. The namespace's output filesystem
    # must enforce an aggregate bound before an untrusted producer can fill operator storage.
    script = """
import json,os
block=b'x'*(1024*1024)
failure=None
try:
    for number in range(100):
        with open('/outputs/file-'+str(number),'wb') as stream:
            stream.write(block)
except OSError as error:
    failure=error.errno
volume=os.statvfs('/outputs')
print(json.dumps({'errno':failure,'capacity':volume.f_blocks*volume.f_frsize,
                  'actual':sum(entry.stat().st_size for entry in os.scandir('/outputs'))}))
"""
    report, out = candidate(isolation_runtime, tmp_path, script, timeout=10)
    assert report["status"] == "completed"
    facts = json.loads((out / "stdout.bin").read_bytes())
    assert facts["errno"] == errno.ENOSPC
    assert 0 < facts["capacity"] <= isolation.MAX_ARTIFACT_BYTES
    assert 0 < facts["actual"] <= isolation.MAX_ARTIFACT_BYTES
    assert sum(entry["size"] for entry in report["artifacts"]) == facts["actual"]


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "socket"])
def test_untrusted_special_artifacts_are_rejected_without_following_or_hanging(
    isolation_runtime: isolation.Runtime, tmp_path: Path, kind: str
) -> None:
    protected = tmp_path / "operator-secret"
    protected.write_bytes(b"protected-output-canary")
    operations = {
        "symlink": f"os.symlink({str(protected)!r}, '/outputs/untrusted')",
        "fifo": "os.mkfifo('/outputs/untrusted')",
        "hardlink": "open('/outputs/first','w').write('untrusted'); os.link('/outputs/first','/outputs/untrusted')",
        "socket": "s=socket.socket(socket.AF_UNIX); s.bind('/outputs/untrusted')",
    }
    with pytest.raises(isolation.IsolationError):
        candidate(isolation_runtime, tmp_path, "import os,socket\n" + operations[kind] + "\n", timeout=5)
    assert not (tmp_path / "bundle").exists()
    assert protected.read_bytes() == b"protected-output-canary"


def test_unreadable_artifact_directory_is_rejected_and_private_staging_is_cleaned(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    script = "import os;os.mkdir('/outputs/unreadable');os.chmod('/outputs/unreadable',0)"
    with pytest.raises(isolation.IsolationError):
        candidate(isolation_runtime, tmp_path, script)
    assert not (tmp_path / "bundle").exists()
    assert list(tmp_path.glob(".exacly-isolation-*")) == []


def test_unavailable_runtime_never_starts_candidate_or_falls_back_to_host(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    marker = tmp_path / "host-candidate-marker"
    runtime = dataclasses.replace(isolation_runtime, bubblewrap=tmp_path / "missing-bwrap")
    with pytest.raises(isolation.IsolationError):
        isolation.run(
            runtime,
            [runtime.python, "-c", f"open({str(marker)!r},'w').write('started')"],
            {},
            tmp_path / "bundle",
        )
    assert not marker.exists()
    assert not (tmp_path / "bundle").exists()


def test_unavailable_guest_interpreter_starts_no_candidate(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    marker = tmp_path / "host-candidate-marker"
    # A missing runtime dependency must stop before releasing any candidate command.
    runtime = dataclasses.replace(isolation_runtime, python="/missing/guest-python")
    with pytest.raises(isolation.IsolationError):
        isolation.run(
            runtime,
            ["/usr/bin/python3", "-c", f"open({str(marker)!r},'w').write('started')"],
            {},
            tmp_path / "bundle",
        )
    assert not marker.exists()
    assert not (tmp_path / "bundle").exists()


def test_same_launch_verification_blocks_candidate_when_network_namespace_is_missing(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    # Fault-inject one missing namespace in the real launcher. A separate successful preflight
    # would be insufficient: the bootstrap must reject this exact launch before releasing argv.
    wrapper = tmp_path / "bwrap-without-network-isolation"
    wrapper.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys\n"
        "args=[]\n"
        "for arg in sys.argv[1:]:\n"
        " if arg=='--unshare-all':\n"
        "  args.extend(['--unshare-pid','--unshare-ipc','--unshare-uts'])\n"
        " elif arg!='--unshare-net':\n"
        "  args.append(arg)\n"
        f"os.execv({str(isolation_runtime.bubblewrap)!r},[{str(isolation_runtime.bubblewrap)!r},*args])\n"
    )
    wrapper.chmod(0o700)
    runtime = dataclasses.replace(isolation_runtime, bubblewrap=wrapper)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as canary:
        canary.bind(("127.0.0.1", 0))
        canary.listen(1)
        port = canary.getsockname()[1]
        command = (
            f"import socket;socket.create_connection(('127.0.0.1',{port}),timeout=1).sendall(b'candidate-started')"
        )
        with pytest.raises(isolation.IsolationError):
            isolation.run(runtime, [runtime.python, "-c", command], {}, tmp_path / "bundle")
        canary.settimeout(0.05)
        with pytest.raises(TimeoutError):
            canary.accept()
    assert not (tmp_path / "bundle").exists()


def named_processes(name: str) -> list[int]:
    result = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            if path.read_text().strip() == name:
                status = (path.parent / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if status != "Z":
                    result.append(int(path.parent.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return result


def descendant_script(name: str, *, finish_parent: bool = False) -> str:
    script = f"""
import ctypes, os, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        ctypes.CDLL(None).prctl(15, {name.encode()!r}, 0, 0, 0)
        open('/outputs/descendant-started','w').write('started')
        while True:
            time.sleep(0.05)
    os._exit(0)
"""
    if finish_parent:
        return (
            script
            + """
while not os.path.exists('/outputs/descendant-started'):
    time.sleep(0.01)
"""
        )
    return (
        script
        + """
while True:
    time.sleep(0.05)
"""
    )


def wait_for_named_process(name: str, *, present: bool) -> list[int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        processes = named_processes(name)
        if bool(processes) is present:
            return processes
        time.sleep(0.02)
    pytest.fail(f"candidate descendants {'did not start' if present else 'survived cleanup'}: {named_processes(name)}")


def require_dead_before_artifact_collection(monkeypatch: pytest.MonkeyPatch, name: str) -> list[bool]:
    """Observe the first artifact read, rather than allowing descendants to stop after run returns."""
    tree = isolation._tree
    collected: list[bool] = []

    def checked_tree(
        root: Path | int, destination: Path | None = None, *, artifacts: bool = False
    ) -> tuple[str, list[Doc]]:
        if artifacts:
            assert named_processes(name) == [], "candidate descendants were still alive when artifact collection began"
            collected.append(True)
        return tree(root, destination, artifacts=artifacts)

    monkeypatch.setattr(isolation, "_tree", checked_tree)
    return collected


def test_timeout_terminates_double_forked_new_session_descendants(
    isolation_runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "exacly-" + uuid.uuid4().hex[:8]
    collected = require_dead_before_artifact_collection(monkeypatch, name)
    try:
        report, out = candidate(isolation_runtime, tmp_path, descendant_script(name), timeout=0.75)
        assert report["status"] == "timed_out"
        assert (out / "artifacts/descendant-started").read_text() == "started"
        assert report["isolation"]["namespace_terminated"] is True
        assert collected == [True]
        assert named_processes(name) == []
    finally:
        for pid in named_processes(name):
            os.kill(pid, signal.SIGKILL)


def test_normal_candidate_exit_terminates_double_forked_new_session_descendants(
    isolation_runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "exacly-" + uuid.uuid4().hex[:8]
    collected = require_dead_before_artifact_collection(monkeypatch, name)
    try:
        report, out = candidate(isolation_runtime, tmp_path, descendant_script(name, finish_parent=True))
        assert report["status"] == "completed"
        assert (out / "artifacts/descendant-started").read_text() == "started"
        assert report["isolation"]["namespace_terminated"] is True
        assert collected == [True]
        assert named_processes(name) == []
    finally:
        for pid in named_processes(name):
            os.kill(pid, signal.SIGKILL)


def test_closed_log_pipes_do_not_end_supervision_before_configured_timeout(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    script = """
import os,time
os.close(1)
os.close(2)
open('/outputs/started','w').write('started')
time.sleep(60)
"""
    started = time.monotonic()
    report, out = candidate(isolation_runtime, tmp_path, script, timeout=0.25)
    assert report["status"] == "timed_out"
    assert (out / "artifacts/started").read_text() == "started"
    assert time.monotonic() - started < 3, "closing logs must not replace the configured deadline with a fixed wait"


def test_closed_logs_and_active_double_forked_writer_stop_before_first_artifact_read(
    isolation_runtime: isolation.Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "exacly-" + uuid.uuid4().hex[:8]
    collected = require_dead_before_artifact_collection(monkeypatch, name)
    script = "import os\nos.close(1)\nos.close(2)\n" + descendant_script(name).replace(
        "while True:\n            time.sleep(0.05)",
        "while True:\n            open('/outputs/active-writer','a').write('tick\\n')\n            time.sleep(0.01)",
        1,
    )
    try:
        report, out = candidate(isolation_runtime, tmp_path, script, timeout=0.5)
        assert report["status"] == "timed_out"
        assert report["isolation"]["namespace_terminated"] is True
        assert (out / "artifacts/descendant-started").read_text() == "started"
        assert (out / "artifacts/active-writer").read_bytes().startswith(b"tick\n")
        assert collected == [True]
        assert named_processes(name) == []
    finally:
        for pid in named_processes(name):
            os.kill(pid, signal.SIGKILL)


def test_supervisor_death_terminates_double_forked_new_session_descendants(
    isolation_runtime: isolation.Runtime, tmp_path: Path
) -> None:
    name = "exacly-" + uuid.uuid4().hex[:8]
    params = tmp_path / "supervisor.json"
    params.write_text(
        json.dumps(
            {
                "rootfs": str(isolation_runtime.rootfs),
                "digest": isolation_runtime.rootfs_digest,
                "python": isolation_runtime.python,
                "bubblewrap": str(isolation_runtime.bubblewrap),
                "script": descendant_script(name),
                "out": str(tmp_path / "bundle"),
            }
        )
    )
    script = """
import json,sys
from pathlib import Path
from exacly import isolation
data=json.loads(Path(sys.argv[1]).read_text())
runtime=isolation.Runtime(Path(data['rootfs']),data['digest'],data['python'],Path(data['bubblewrap']))
isolation.run(runtime,[runtime.python,'-I','-S','/inputs/probe.py'],
              {'probe.py':data['script'].encode()},Path(data['out']),timeout=30)
"""
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "python/src")}
    supervisor = subprocess.Popen(
        [sys.executable, "-c", script, str(params)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_named_process(name, present=True)
        supervisor.kill()
        supervisor.communicate(timeout=5)
        wait_for_named_process(name, present=False)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate(timeout=5)
        for pid in named_processes(name):
            os.kill(pid, signal.SIGKILL)
    assert not (tmp_path / "bundle").exists()
