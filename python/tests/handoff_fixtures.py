"""Reviewed handoff fixtures with real journals and Git, and an explicit runtime test double.

The controlled runtime cases exercise coordinator semantics only. The end-to-end module supplies
the actual Bubblewrap runtime and establishes the physical execution boundary independently.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import Scenario
from exacly import executor_contract, executor_runtime, handoff, handoff_git, handoff_store, isolation, planning
from executor_fixtures import ExecutorScenario, make_executor_scenario
from test_admission import BASE
from test_reconciliation import selected

Doc = dict[str, Any]
PAYLOAD = b"def result():\n    return 42\n"
AUTHOR: Doc = {
    "name": "Reviewed operator",
    "email": "operator@example.invalid",
    "time": int(dt.datetime(2026, 10, 30, 10, tzinfo=dt.UTC).timestamp()),
}


def object_bytes(repository: Path, kind: str, content: bytes) -> str:
    """Install a trusted fixture object without executing repository config, hooks or filters."""
    raw = kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
    identity = hashlib.sha1(raw).hexdigest()
    directory = repository / "objects" / identity[:2]
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / identity[2:]
    if not path.exists():
        path.write_bytes(zlib.compress(raw))
        path.chmod(0o600)
    return identity


def seed_repository(repository: Path, case: Scenario) -> str:
    """Keep the controlled producer's actual files in the real base tree for the pilot."""
    files: dict[str, bytes] = {}
    for entry in case.source.tree:
        blob = case.source.pages[f"{BASE}/git/blobs/{entry['sha']}"]
        files[entry["path"]] = base64.b64decode(blob["content"])
    files["README.md"] = b"preserve this unrelated base file\n"
    tree: dict[str, Any] = {}
    for name, content in files.items():
        cursor = tree
        parts = name.split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = content

    def retain(entries: dict[str, Any]) -> str:
        encoded: list[tuple[bytes, bytes]] = []
        for name, value in entries.items():
            if isinstance(value, dict):
                mode, identity = b"40000", retain(value)
                order = name.encode() + b"/"
            else:
                mode, identity = b"100644", object_bytes(repository, "blob", value)
                order = name.encode()
            encoded.append((order, mode + b" " + name.encode() + b"\0" + bytes.fromhex(identity)))
        return object_bytes(repository, "tree", b"".join(data for _, data in sorted(encoded)))

    root = retain(tree)
    stamp = str(AUTHOR["time"])
    identity = object_bytes(
        repository,
        "commit",
        (
            f"tree {root}\n"
            f"author Reviewed operator <operator@example.invalid> {stamp} +0000\n"
            f"committer Reviewed operator <operator@example.invalid> {stamp} +0000\n\n"
            "Reviewed input implementation\n"
        ).encode(),
    )
    reference = repository / "refs/heads/base"
    reference.write_text(identity + "\n")
    reference.chmod(0o600)
    return identity


def stopped_identity(pid: int) -> Doc:
    return {"boot_id": "00000000-0000-0000-0000-000000000000", "pid": pid, "start_time": 1, "pid_namespace": "pid:[1]"}


def controlled_runtime(payload: bytes) -> Any:
    def run(
        runtime: isolation.Runtime,
        argv: Any,
        inputs: Any,
        out: Path,
        *,
        on_worker: Any,
        on_namespace: Any,
        **options: Any,
    ) -> Doc:
        worker, namespace = stopped_identity(999999), stopped_identity(999998)
        on_worker(worker)
        on_namespace(namespace)
        out.mkdir(mode=0o700)
        (out / "artifacts").mkdir(mode=0o700)
        contents = {"artifacts/result.txt": payload, "stdout.bin": b"", "stderr.bin": b""}
        for name, value in contents.items():
            path = out / name
            path.write_bytes(value)
            path.chmod(0o600)
        report: Doc = {
            "schema_version": isolation.VERSION,
            "backend": isolation.BACKEND,
            "status": "completed",
            "exit_code": 0,
            "argv": list(argv),
            "runtime": {"rootfs_digest": runtime.rootfs_digest, "python": runtime.python},
            "isolation": {"namespace_terminated": True},
            "inputs": executor_contract.roster(inputs),
            "stdout": executor_contract.roster({"stdout.bin": b""})[0],
            "stderr": executor_contract.roster({"stderr.bin": b""})[0],
            "artifacts": executor_contract.roster({"artifacts/result.txt": payload}),
            "claims": {
                "outputs_trusted": False,
                "evidence_admitted": False,
                "completion_authorized": False,
                "self_authenticating": False,
            },
        }
        report["digest"] = planning.digest(report)
        (out / "report.json").write_text(json.dumps(report))
        (out / "report.json").chmod(0o600)
        proofs = {
            key: {"identity": value, "terminated": True, "reason": "previous_boot"}
            for key, value in (("worker", worker), ("namespace", namespace))
        }
        return {"status": "completed", "termination": proofs, "reason": None, "report": report}

    return run


@dataclass
class HandoffCase:
    item: ExecutorScenario
    repository: Path
    journal: Path
    request: handoff.Request
    execution: Doc
    payload: bytes

    def preview(self, *, request: handoff.Request | None = None, **options: Any) -> Doc:
        return handoff.preview(self.request if request is None else request, **options)

    @property
    def authorization(self) -> Doc:
        return dict(self.preview()["authorization_template"])

    def apply(self, authorization: Doc | None = None, *, request: handoff.Request | None = None, **options: Any) -> Doc:
        return handoff.apply(
            self.request if request is None else request,
            authorization=self.authorization if authorization is None else authorization,
            **options,
        )

    @property
    def artifact(self) -> Path:
        return Path(self.execution["receipt"]["output"]) / "artifacts/result.txt"

    def authoritative_bytes(self) -> tuple[bytes, bytes, bytes]:
        return (*self.item.authoritative_bytes(), self.item.journal.read_bytes())


def make_handoff_scenario(
    case: Scenario,
    runtime: isolation.Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch | None = None,
    *,
    payload: bytes = PAYLOAD,
    real: bool = False,
) -> HandoffCase:
    private = tmp_path / "handoff-operator"
    private.mkdir(mode=0o700)
    repository = private / "destination.git"
    repository_identity = handoff_git.initialize_repository(repository)
    base = seed_repository(repository, case)
    case.request = replace(case.request, target=selected(base))
    item = make_executor_scenario(case, runtime, tmp_path)
    script = f"from pathlib import Path\nPath('/outputs/result.txt').write_bytes({payload!r})\n"
    item.request = item.command(script)
    if not real:
        assert monkeypatch is not None
        monkeypatch.setattr(executor_runtime, "run", controlled_runtime(payload))
    execution = item.apply()
    assert execution["status"] == "succeeded", execution
    journal = private / "handoff.sqlite3"
    initialized = handoff_store.initialize(journal, repository=repository_identity)
    policy: Doc = {
        "schema_version": "0.1.0",
        "name": "reviewed-local-handoff",
        "version": "1",
        "adapter": "local-git-v1",
        "storage": {
            "journal": str(journal),
            "journal_id": initialized["journal_id"],
            "executor_journal": str(item.journal),
            "executor_journal_id": item.policy["storage"]["journal_id"],
            "repository": str(repository),
            "repository_id": repository_identity["instance_id"],
        },
        "allowed_paths": ["src/"],
    }
    request = handoff.Request(
        policy,
        execution["execution_id"],
        1,
        base,
        "refs/heads/reviewed-result",
        None,
        {"artifacts/result.txt": "src/service.py"},
        "Apply separately reviewed executor output\n",
        copy.deepcopy(AUTHOR),
    )
    return HandoffCase(item, repository, journal, request, execution, payload)


@pytest.fixture
def handoff_case(dispatch_scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HandoffCase:
    root = tmp_path / "unused-runtime"
    root.mkdir(mode=0o700)
    runtime = isolation.Runtime(root, "sha256:" + "1" * 64, "/usr/bin/python3")
    return make_handoff_scenario(dispatch_scenario, runtime, tmp_path, monkeypatch)


def repository_bytes(repository: Path) -> dict[str, bytes]:
    return {str(path.relative_to(repository)): path.read_bytes() for path in repository.rglob("*") if path.is_file()}


def read_git_file(repository: Path, commit: str, path: str) -> bytes:
    # This command reads fixture-owned files only; production uses its separately tested Git boundary.
    import subprocess

    environment = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.check_output(["git", "--git-dir", str(repository), "show", commit + ":" + path], env=environment)
