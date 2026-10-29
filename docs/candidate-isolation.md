# Candidate command isolation

Exacly runs an explicitly authorized candidate command through one supported backend: Linux Bubblewrap with a reviewed root filesystem. The launcher verifies the actual namespace before releasing the command. Its acceptance contract is [REQ-025](../spec/exacly/automation.yaml); the command launcher is separate from durable dispatch and bounded executor attempts.

The supported boundary protects operator files, environment, processes and control interfaces from candidate processes and their descendants. It relies on a trusted host kernel, Bubblewrap and libseccomp installations, runtime root filesystem and operator. It uses Linux namespaces, not a virtual machine, and does not claim protection from a hostile operator, kernel or runtime exploits, or general resource exhaustion.

## Prepare a trusted runtime

Use Linux with kernel PID file descriptors and `Seccomp_filters` reporting in `/proc/self/status`, Python exposing `os.memfd_create`, `os.pidfd_open` and `signal.pidfd_send_signal`, Bubblewrap 0.8 or later, the system `libseccomp2` library and permission to create user, mount, PID, network, IPC and UTS namespaces. These kernel interfaces are available together in upstream Linux 5.9 or later; the launcher checks the actual required features. The required `--disable-userns`, `--assert-userns-disabled`, `--info-fd` and `--seccomp` features must be available. If the host forbids a required namespace or other protection, the launcher fails before starting the candidate. It never retries on the host.

On Ubuntu 24.04, AppArmor can deny unprivileged user namespace construction even when Bubblewrap has every required feature. With `kernel.apparmor_restrict_unprivileged_userns=1`, the selected executable needs a suitable host AppArmor profile. This is an operator-managed host prerequisite; Exacly never installs a profile, changes a sysctl or relaxes a failed launch automatically.

The repository supplies a reviewed [Bubblewrap profile template](../ci/apparmor/exacly-bwrap) attached to `/usr/bin/bwrap`. It follows Ubuntu's `flags=(unconfined)` plus `userns` pattern to permit namespace construction by that trusted tool. It grants no additional host capabilities. Candidate confinement still comes from the launcher's required namespaces, read-only mounts, dropped capabilities, `NoNewPrivs`, disabled nested user namespaces and fixed syscall filter, all checked for each launch.

For the disposable Ubuntu 24.04 CI host, the explicit administrator [setup script](../ci/setup_isolation_runtime.sh) installs AppArmor, Bubblewrap and libseccomp2, then loads this exact template with `apparmor_parser --replace`. It requires AppArmor enforcement to be enabled and the existing user namespace restriction to be `1`, and verifies that both remain unchanged. It stops when it finds a known vendor Bubblewrap profile so the administrator can review that existing attachment rather than load a conflicting one. On an administered host with an existing profile, review and adapt that policy through the host's normal administration process.

From a reviewed repository checkout on the matching CI host, run the explicit setup and then the [runtime preflight](../ci/check_isolation_runtime.py):

```sh
sh ci/setup_isolation_runtime.sh
/usr/bin/python3 ci/check_isolation_runtime.py
```

The setup uses administrator privileges; the diagnostic preflight runs trusted system probes and reports kernel, AppArmor and namespace details. It fails before the full test suite when strict Bubblewrap construction is unavailable. Passing this host preflight does not replace the candidate launch's own verification gate or the real denial tests.

Prepare a detached root filesystem using reviewed operating-system packages or a reviewed image build. Supply the guest Python interpreter and the standard library and shared libraries it needs, including `ctypes` and libc for the trusted denial probes. The libseccomp library is used on the host to compile the filter and is not required inside the guest runtime. The selected interpreter path must name a regular executable file inside the root filesystem, with plain directory ancestors; select the actual versioned binary or create the regular interpreter file while building the runtime instead of selecting a symbolic link. The trusted bootstrap runs that interpreter with `-I -S`, so the runtime must work without user site packages or inherited Python configuration. Keep this runtime separate from operator storage; it must contain only files approved for candidate access.

The launcher accepts regular files, directories and symbolic links within the reviewed runtime tree; it rejects hard links, privileged setuid/setgid files and unsupported nodes such as devices, sockets and FIFOs. It copies runtime content into private staging and verifies the copy's digest against the reviewed policy. The command does not mount host `/usr`, `/lib`, home directories, workspaces or runtime-management sockets. A root filesystem digest identifies reviewed content; computing a digest does not approve that content or authenticate its supplier.

Inspect the prepared runtime without executing it:

```sh
exacly isolation inspect --rootfs /srv/exacly/runtimes/python-rootfs --json
```

The report has operation `inspect-rootfs`, scope `content_identity`, the absolute root filesystem path and its `digest`. Review the content and place that exact digest in a runtime policy outside the recursively loaded model directory:

```json
{
  "schema_version": "0.1.0",
  "backend": "linux-bubblewrap-v1",
  "rootfs": "/srv/exacly/runtimes/python-rootfs",
  "rootfs_digest": "sha256:<64 lowercase hexadecimal characters from inspection>",
  "python": "/usr/bin/python3",
  "bubblewrap": "/usr/bin/bwrap"
}
```

`rootfs` and `bubblewrap` are absolute host paths; use a canonical root filesystem path without symbolic-link ancestors. `python` is an absolute path inside the reviewed runtime. Policy keys and the backend are closed: callers cannot supply extra mounts, arbitrary Bubblewrap options or a different network mode. The installed Bubblewrap executable and the selected interpreter are part of the operator's trusted runtime. Runtime changes need a new inspection and policy review.

## Supply explicit snapshots and run a command

The CLI accepts an optional JSON input manifest containing explicit relative paths and UTF-8 file contents. It reads no source directory implicitly and accepts no host file paths in that manifest. Keep this file outside the model directory too:

```json
{
  "schema_version": "0.1.0",
  "files": {
    "hello.py": "from pathlib import Path\nPath('/outputs/result.txt').write_text('hello\\n')\n"
  }
}
```

Run the command with a new output destination outside the recursively loaded model directory:

```sh
mkdir -p runs
exacly isolate --runtime-policy trusted/runtime.json \
  --inputs approved-inputs.json --out runs/hello --timeout 30 --json -- \
  /usr/bin/python3 -I -S /inputs/hello.py
```

The command vector is passed directly, without shell interpolation. Invoking `isolate` authorizes this one command and these explicit snapshots; it does not authorize another action or grant dispatch, verification or completion authority. Omitting `--inputs` supplies an empty snapshot. Binary snapshots can be supplied through the Python API.

The runtime root and `/inputs` are read-only. The guest has private `/proc`, a minimal private `/dev` and private temporary filesystems for `/tmp`, `/work` and `/outputs`. Each of these three private filesystems has a kernel-enforced aggregate size limit of 64 MiB. Candidate code receives no writable host-backed mount; the published host bundle is created later by the supervisor. Host filesystem paths retain no authority inside this root. The network namespace provides no host or external network access, including the host's loopback and abstract Unix sockets. Child processes inherit the boundary.

The launcher clears inherited environment variables and closes unrelated descriptors. It supplies fixed guest environment values, captures candidate stdout and stderr and detaches candidate stdin from the launch control channel. It does not expose inherited tokens, proxy settings, Python import paths, host terminals or privileged control sockets.

## Verify the actual launch

Before the candidate starts, a trusted bootstrap inside the same launch checks the required namespace identities against the supervisor's identities, zero capabilities, `NoNewPrivs`, the active seccomp filter and expected denial probes, private process visibility and mount protections. It reports the checked facts and waits for the supervisor to release the command. The supervisor obtains the actual namespace init process from Bubblewrap's `--info-fd`, verifies its PID namespace and namespace PID 1 identity, and pins that exact process with a kernel PID file descriptor before releasing candidate execution. This avoids relying on a numeric PID that can later be reused. Before release, the supervisor also pins a directory descriptor for the private output filesystem through that verified init process. Candidate code does not inherit these control descriptors. An invalid report, unsupported PID file descriptor operation, failed setup or closed control channel starts no candidate command.

This gate checks the namespace in which the command will run. A separate version check or successful probe does not establish the boundary. Namespace setup uses strict required protections, and nested user namespaces are disabled. Candidate subprocesses remain inside this boundary; no subprocess option grants host execution.

The launcher also supplies a mandatory fixed syscall filter, compiled with the installed libseccomp library and passed to Bubblewrap before the trusted bootstrap starts. The filter denies `keyctl`, `add_key` and `request_key`, preventing candidate access to the operator's kernel keyrings even when those keyrings would otherwise be inherited across namespaces. The procfs keyring listings `/proc/keys` and `/proc/key-users` are also masked with empty read-only files and checked before release. It denies `io_uring_setup`, `io_uring_enter` and `io_uring_register` so an asynchronous socket operation cannot bypass the socket-family restriction. `socket` permits only Unix, IPv4 and IPv6 families; `socketpair` permits only Unix. These permitted families still operate inside the private namespaces and cannot reach the host network or host sockets.

The filter supports the native syscall ABI only and terminates compatibility or unsupported ABI calls. The bootstrap requires `Seccomp: 2` and a `Seccomp_filters` count greater than the supervisor's baseline, then verifies `EPERM` denial for keyring and asynchronous I/O operations and `EAFNOSUPPORT` for a VSOCK socket probe before the supervisor releases the command. Missing libseccomp, an unsupported ABI, failed filter compilation or failed checks stop the launch before candidate execution. Callers cannot opt out, supply a replacement filter or add arbitrary syscall rules. Reports retain `isolation.syscall_policy` with the compiled BPF digest, denied syscalls, permitted socket and socket-pair families and the native-ABI-only rule. The digest identifies the installed filter bytes; it does not authenticate the report.

Normal completion, timeout and supervisor termination tear down the namespace and descendants. On timeout or a captured-log limit, the supervisor signals namespace init through its pinned PID file descriptor and waits for that exact process to terminate. It also waits for namespace termination after normal candidate exit. Only then does it report `isolation.namespace_terminated: true` and collect artifacts from the pinned private output directory, using traversal that does not follow symbolic links. It rejects hard links and special nodes, including sockets, devices and FIFOs, instead of reading them or hanging on extraction. These checks also apply when candidate code returns a nonzero status.

If the supervisor is killed with `SIGKILL`, Bubblewrap's parent-death protection kills the namespace and descendants, but the supervisor cannot execute its Python cleanup. A private `.exacly-isolation-*` staging directory may remain without a successfully published bundle. After confirming that the related sandbox processes have exited, an operator can inspect and remove abandoned staging. A new invocation creates fresh staging and never implicitly resumes, trusts or cleans up an old run.

## Results and limits

A successful bundle publication creates a new private destination containing `report.json`, `stdout.bin`, `stderr.bin` and accepted files under `artifacts/`. An existing destination is never overwritten. Setup failure, a changed runtime or unsafe artifacts raise an error and publish no successful bundle. The original runtime and input snapshots remain unchanged. An I/O error after final publication can leave the complete destination in place; inspect it before retrying with another new destination.

Reports identify the runtime, snapshot digests and verified launch facts separately from candidate outcomes. Candidate exit codes, logs and artifacts remain untrusted. A successful command does not authenticate evidence, establish test honesty, prove a link between an executor and a produced revision, or authorize task completion. Existing live evidence admission and local completion retain their own contracts.

The CLI exits 0 for `completed`; 1 for `candidate_failed`, `timed_out` or `output_limit`; and 2 for invalid inputs, unsupported setup or operational errors. Candidate logs are retained in the bundle rather than rendered directly to the terminal. Inspect them as untrusted content.

The per-run timeout must be finite, greater than zero and at most 3,600 seconds, with a default of 30 seconds. Input snapshots are limited to 1,024 files and 8 MiB total, each captured log to 1 MiB, collected artifacts to 4,096 entries including directories and 64 MiB total, and a runtime tree to 100,000 entries and 1 GiB. The private output filesystem enforces its aggregate storage limit during execution; safe collection separately checks the artifact count and byte limits. The remaining limits bound the launcher's copying, collection and waiting work. They do not establish cgroup containment, a general denial-of-service defense, durable retry budgets or concurrency accounting. Those executor controls remain the separate REQ-027 contract.

## Python API

```python
from pathlib import Path

from exacly.isolation import Runtime, rootfs_digest, run

rootfs = Path("/srv/exacly/runtimes/python-rootfs")
# Inspect and independently review this value before using it as approved policy.
digest = rootfs_digest(rootfs)
runtime = Runtime(rootfs, digest, "/usr/bin/python3", Path("/usr/bin/bwrap"))
Path("runs").mkdir(exist_ok=True)

report = run(
    runtime,
    ["/usr/bin/python3", "-I", "-S", "/inputs/hello.py"],
    {"hello.py": b"from pathlib import Path\nPath('/outputs/result.txt').write_bytes(b'hello\\n')\n"},
    Path("runs/hello"),
    timeout=30,
)
```

`parse_policy(document)` validates the same strict policy used by the CLI. `IsolationError` describes invalid inputs, unsupported setup, integrity errors and unsafe outputs. A candidate nonzero exit is a reported outcome. The API receives snapshot bytes and no operator ledger, state or completion handles.

## Verify the supported platform

The repository's real integration suites are [`test_execution_isolation.py`](../python/tests/test_execution_isolation.py) and [`test_isolation_syscalls.py`](../python/tests/test_isolation_syscalls.py). CI applies the reviewed Ubuntu 24.04 [host setup](../ci/setup_isolation_runtime.sh) and requires the [runtime preflight](../ci/check_isolation_runtime.py) to pass before running the ordinary test and model checks. These cases fail when the required runtime is unavailable; skipped or mocked namespace checks cannot establish successful verification of REQ-025.

The repository's [evidence producer proposal](../ci/evidence-producer.yaml) includes the host setup script, AppArmor template and preflight helper in its observer fingerprint scope. Review those bytes at the approved revision when generating a producer trust policy; changing them changes that fingerprint. Select the workflow and observer paths explicitly in your own reviewed producer proposal.

The tests use [`isolation_fixtures.py`](../python/tests/isolation_fixtures.py) to construct a detached runtime from an explicitly selected system Python interpreter, its standard library and library dependencies. This is a controlled test fixture, not an automatic approval or production root filesystem builder. Real candidate processes and descendants attempt host file, process, descriptor and socket access, while permitted immutable input and regular output exchange are also checked.

Run the suite in a Linux environment permitted to create the required namespaces:

```sh
PYTHONPATH=python/src pytest python/tests/test_execution_isolation.py python/tests/test_isolation_syscalls.py
```

A host sandbox or security policy may prevent namespace creation. Diagnose that platform restriction; an unavailable boundary is a failed requirement check, not permission to execute the candidate outside it.
