"""Mandatory real Ubuntu systemd/cgroup/Bubblewrap acceptance for the fixed inventory worker.

Each parent test provisions a disposable finite service running the same exact case as the runner
user. The child reads actual caps and proves inherited membership before native release. Missing
systemd, sudo, cgroup controls or isolation is a failure, never a skipped verification case.
"""

from __future__ import annotations

import copy
import errno
import json
import multiprocessing
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from exacly import executor, executor_runtime, planning, resource_guard, worker_integration
from exacly.applicability import Target
from test_unattended_foundations_integration import PreparedPath
from test_unattended_foundations_integration import dispatch_scenario as dispatch_scenario
from test_unattended_foundations_integration import isolation_runtime as isolation_runtime
from test_unattended_foundations_integration import prepared_path as prepared_path

Doc = dict[str, Any]
ROOT = Path(__file__).resolve().parents[2]


def guarded_case(
    request: pytest.FixtureRequest, tmp_path: Path, record_property: Callable[[str, object], None]
) -> resource_guard.Guard | None:
    selected = os.environ.get("EXACLY_RESOURCE_CGROUP")
    if selected is not None:
        guard = resource_guard.capture(resource_guard.PILOT_BOUNDS, expected_cgroup=selected)
        record_property("actual-resource-envelope", json.dumps(guard.identity, sort_keys=True))
        return guard
    report = tmp_path / "resource-child-junit.xml"
    result = resource_guard.run_unit(
        [sys.executable, "-m", "pytest", request.node.nodeid, "-q", "--junitxml=" + str(report)],
        cwd=ROOT,
        bounds=resource_guard.PILOT_BOUNDS,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    child = ElementTree.parse(report)
    properties = child.findall(".//property[@name='actual-resource-envelope']")
    assert len(properties) == 1, "child did not retain its actual resource identity"
    record_property("actual-resource-envelope", properties[0].attrib["value"])
    return None


def approved(
    case: PreparedPath, guard: resource_guard.Guard, *, request: executor.Request | None = None
) -> tuple[executor.Request, Doc]:
    item = case.item
    chosen = item.request if request is None else request
    chosen = replace(chosen, executor_policy={**chosen.executor_policy, "resource_guard": guard.descriptor})
    proposal = worker_integration.preview(item.documents, item.state_path, chosen, prepared=case.worker, guard=guard)
    assert proposal["status"] == "ready", proposal
    return chosen, proposal["authorization_template"]


def apply(case: PreparedPath, guard: resource_guard.Guard, chosen: executor.Request, authorization: Doc) -> Doc:
    item = case.item
    return worker_integration.apply(
        item.journal,
        item.ledger,
        item.documents,
        item.state_path,
        chosen,
        authorization=authorization,
        prepared=case.worker,
        guard=guard,
    )


def test_fixed_recipe_executes_with_actual_caps_and_exact_replay(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    prepared_path: PreparedPath,
) -> None:
    guard = guarded_case(request, tmp_path, record_property)
    if guard is None:
        return
    case = prepared_path
    chosen, grant = approved(case, guard)
    before = case.item.authoritative_bytes()
    for changes in (
        {"target": Target({**chosen.target.data, "revision": "f" * 40})},
        {
            "trust_policy": {
                **chosen.trust_policy,
                "model": {**chosen.trust_policy["model"], "digest": "sha256:" + "f" * 64},
            }
        },
    ):
        with pytest.raises(ValueError, match="source or approved model differs"):
            worker_integration.bind(replace(chosen, **changes), case.worker, guard=guard)
    changed = {**guard.identity, "inode": guard.identity["inode"] + 1}
    stale_guard = replace(guard, _identity=json.dumps(changed).encode())
    before_attempt = case.item.journal.read_bytes()
    with pytest.raises(ValueError, match="identity or caps changed"):
        worker_integration.preview(
            case.item.documents,
            case.item.state_path,
            replace(chosen, executor_policy={**chosen.executor_policy, "resource_guard": stale_guard.descriptor}),
            prepared=case.worker,
            guard=stale_guard,
        )
    assert case.item.journal.read_bytes() == before_attempt
    broader_guard = resource_guard.capture(
        replace(guard.bounds, memory_bytes=guard.bounds.memory_bytes * 2), expected_cgroup=guard.identity["cgroup"]
    )
    broader_request = replace(
        chosen, executor_policy={**chosen.executor_policy, "resource_guard": broader_guard.descriptor}
    )
    with pytest.raises(ValueError, match="authorization differs"):
        executor.apply(
            case.item.journal,
            case.item.ledger,
            case.item.documents,
            case.item.state_path,
            broader_request,
            authorization=grant,
        )
    assert case.item.journal.read_bytes() == before_attempt
    result = apply(case, guard, chosen, grant)
    assert result["status"] == "succeeded", result
    assert result["resource_guard"] == guard.descriptor
    assert result["history"]["intent"]["contract"]["policy"]["resource_guard"] == guard.descriptor
    assert result["receipt"]["authorization"]["contract_digest"] == planning.digest(
        result["history"]["intent"]["contract"]
    )
    output = Path(result["receipt"]["output"])
    report = json.loads((output / "report.json").read_bytes())
    inventory = json.loads((output / "artifacts/source-digests.json").read_bytes())
    assert report["argv"] == list(case.worker.argv)
    assert report["isolation"]["verified"] is True and report["isolation"]["namespace_terminated"] is True
    assert inventory["snapshot_digest"] == case.worker.identity["snapshot_digest"]
    assert inventory["verification_established"] is inventory["evidence_admitted"] is False
    assert case.item.authoritative_bytes() == before
    saved = case.item.journal.read_bytes()
    assert apply(case, guard, chosen, grant)["receipt"] == result["receipt"]
    with pytest.raises(ValueError, match="resource envelope differs"):
        executor.apply(
            case.item.journal,
            case.item.ledger,
            case.item.documents,
            case.item.state_path,
            broader_request,
            authorization=grant,
        )
    assert case.item.journal.read_bytes() == saved
    assert len(list(case.item.output_root.rglob("report.json"))) == 1


def test_fixed_recipe_setup_failure_preserves_pending_and_no_build(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    prepared_path: PreparedPath,
) -> None:
    guard = guarded_case(request, tmp_path, record_property)
    if guard is None:
        return
    case = prepared_path
    policy = copy.deepcopy(case.item.request.executor_policy)
    policy["runtime"]["rootfs"] = str(tmp_path / "unavailable-approved-rootfs")
    chosen, grant = approved(case, guard, request=replace(case.item.request, executor_policy=policy))
    before = case.item.authoritative_bytes()
    result = apply(case, guard, chosen, grant)
    assert result["status"] == "failed", result
    assert result["receipt"]["build"] is None
    assert case.item.authoritative_bytes() == before
    assert not list(case.item.output_root.rglob("report.json"))


def paused_apply(
    case: PreparedPath, guard: resource_guard.Guard, chosen: executor.Request, grant: Doc, channel: Connection
) -> None:
    original = executor_runtime.run

    def gated(*args: Any, **kwargs: Any) -> Doc:
        callback = kwargs["on_namespace"]

        def retain_then_pause(identity: Doc) -> None:
            callback(identity)  # Includes native durable identity and actual cgroup checks.
            channel.send(("paused", identity))
            channel.recv()

        return original(*args, **{**kwargs, "on_namespace": retain_then_pause})

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(executor_runtime, "run", gated)
            result = apply(case, guard, chosen, grant)
            channel.send(("result", result))
    finally:
        channel.close()


@pytest.mark.parametrize("interruption", ["cancel", "process-crash"])
def test_native_cancellation_and_same_unit_process_recovery_retain_failed_attempt(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    prepared_path: PreparedPath,
    interruption: str,
) -> None:
    guard = guarded_case(request, tmp_path, record_property)
    if guard is None:
        return
    case = prepared_path
    chosen, grant = approved(case, guard)
    before = case.item.authoritative_bytes()
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=paused_apply, args=(case, guard, chosen, grant, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30), "worker never reached its durable release gate"
        paused, identity = parent.recv()
        assert paused == "paused"
        guard.check_process(identity["pid"])
        if interruption == "cancel":
            cancellation = executor.cancel(
                case.item.journal, grant["execution_id"], backends=worker_integration.guarded_backends(guard)
            )
            assert cancellation["status"] in ("cancelled", "cancellation_requested", "running")
            parent.send("continue")
            assert parent.poll(20)
            kind, result = parent.recv()
            assert kind == "result" and result["status"] == "cancelled", result
            process.join(timeout=10)
            assert process.exitcode == 0
        else:
            assert process.pid is not None
            os.kill(process.pid, signal.SIGKILL)
            process.join(timeout=10)
            assert process.exitcode == -signal.SIGKILL
            result = executor.recover(
                case.item.journal, grant["execution_id"], backends=worker_integration.guarded_backends(guard)
            )
            assert result["status"] == "interrupted", result
        assert result["receipt"]["build"] is None
        assert case.item.authoritative_bytes() == before
        retained = executor.show(case.item.journal, grant["execution_id"])
        assert len(retained["history"]["attempts"]) == 1
        assert not list(case.item.output_root.rglob("report.json"))
        assert guard.digest == planning.digest(guard.check())
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()


def counters(path: Path) -> dict[str, int]:
    return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}


def test_kernel_enforces_aggregate_cpu_memory_and_process_caps(
    request: pytest.FixtureRequest, tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    guard = guarded_case(request, tmp_path, record_property)
    if guard is None:
        return
    directory = Path("/sys/fs/cgroup") / guard.identity["cgroup"].lstrip("/")
    before_cpu = counters(directory / "cpu.stat")
    script = "import time\nend=time.monotonic()+2\nwhile time.monotonic()<end: pass\n"
    workers = [subprocess.Popen([sys.executable, "-I", "-c", script]) for _ in range(4)]
    try:
        for worker in workers:
            worker.wait(timeout=10)
            assert worker.returncode == 0
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
    assert counters(directory / "cpu.stat")["nr_throttled"] > before_cpu["nr_throttled"]

    before_pids = counters(directory / "pids.events")
    sleepers: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(guard.bounds.pids * 2):
            try:
                sleepers.append(subprocess.Popen(["/usr/bin/sleep", "30"]))
            except OSError as error:
                assert error.errno == errno.EAGAIN
                break
        else:
            raise AssertionError("the declared process cap allowed unlimited children")
        assert counters(directory / "pids.events")["max"] > before_pids["max"]
        assert int((directory / "pids.current").read_text()) <= guard.bounds.pids
    finally:
        for sleeper in sleepers:
            sleeper.kill()
        for sleeper in sleepers:
            sleeper.wait(timeout=5)

    before_memory = counters(directory / "memory.events")
    memory = subprocess.Popen(
        [sys.executable, "-I", "-c", "a=bytearray(2*1024**3)\nfor n in range(0,len(a),4096): a[n]=1\n"],
    )
    try:
        memory.wait(timeout=30)
        assert memory.returncode == -signal.SIGKILL
    finally:
        if memory.poll() is None:
            memory.kill()
        memory.wait(timeout=5)
    assert counters(directory / "memory.events")["oom_kill"] > before_memory["oom_kill"]
    guard.check()
