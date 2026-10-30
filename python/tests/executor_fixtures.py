"""Registered local work and an explicitly reviewed snapshot-only executor contract."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import Scenario as DispatchScenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import executor, executor_store, isolation
from isolation_fixtures import isolation_runtime as isolation_runtime

Doc = dict[str, Any]
SUCCESS = "from pathlib import Path\nPath('/outputs/result.txt').write_bytes(b'identified build\\n')\n"


@dataclass
class ExecutorScenario:
    dispatch: DispatchScenario
    runtime: isolation.Runtime
    journal: Path
    output_root: Path
    policy: Doc
    request: executor.Request

    @property
    def documents(self) -> Doc:
        return self.dispatch.documents

    @property
    def state_path(self) -> Path:
        return self.dispatch.state_path

    @property
    def ledger(self) -> Path:
        return self.dispatch.ledger

    @property
    def authorization(self) -> Doc:
        return dict(self.preview()["authorization_template"])

    def preview(self, *, request: executor.Request | None = None) -> Doc:
        return executor.preview(self.documents, self.state_path, request or self.request)

    def apply(self, *, request: executor.Request | None = None, **options: Any) -> Doc:
        selected = request or self.request
        authorization = (
            options.pop("authorization")
            if "authorization" in options
            else executor.preview(self.documents, self.state_path, selected)["authorization_template"]
        )
        return executor.apply(
            self.journal,
            self.ledger,
            self.documents,
            self.state_path,
            selected,
            authorization=authorization,
            **options,
        )

    def command(self, script: str, **policy_changes: Any) -> executor.Request:
        policy = {**copy.deepcopy(self.policy), **policy_changes}
        return replace(self.request, executor_policy=policy, inputs={"candidate.py": script.encode()})

    def authoritative_bytes(self) -> tuple[bytes, bytes]:
        return self.state_path.read_bytes(), self.ledger.read_bytes()


def make_executor_scenario(case: DispatchScenario, runtime: isolation.Runtime, tmp_path: Path) -> ExecutorScenario:
    dispatched = case.apply()
    assert dispatched["status"] == "registered", dispatched
    journal = case.journal.parent / "executor.sqlite3"
    initialized = executor_store.initialize(journal, ledger=case.ledger, max_concurrency=1)
    output_root = tmp_path / "executor-outputs"
    output_root.mkdir(mode=0o700)
    policy: Doc = {
        "schema_version": "0.1.0",
        "name": "reviewed-local-executor",
        "version": "1",
        "adapter": "local-bubblewrap-v1",
        "storage": {
            "journal": str(journal),
            "journal_id": initialized["journal_id"],
            "ledger": str(case.ledger),
            "state": str(case.state_path),
            "output_root": str(output_root),
        },
        "runtime": {
            "schema_version": "0.1.0",
            "backend": isolation.BACKEND,
            "rootfs": str(runtime.rootfs),
            "rootfs_digest": runtime.rootfs_digest,
            "python": runtime.python,
            "bubblewrap": str(runtime.bubblewrap),
        },
        "argv": [runtime.python, "-I", "-S", "/inputs/candidate.py"],
        "expected_outputs": ["result.txt"],
        "max_attempts": 2,
        "timeout_seconds": 15,
        "permitted_repository_effects": [],
    }
    original = case.request
    request = executor.Request(
        original.plan,
        original.action_id,
        original.readiness_policy,
        original.trust_policy,
        policy,
        original.target,
        original.date,
        dispatched["receipt"]["binding_id"],
        {"candidate.py": SUCCESS.encode()},
    )
    return ExecutorScenario(case, runtime, journal, output_root, policy, request)


@pytest.fixture
def executor_scenario(
    dispatch_scenario: DispatchScenario, isolation_runtime: isolation.Runtime, tmp_path: Path
) -> ExecutorScenario:
    return make_executor_scenario(dispatch_scenario, isolation_runtime, tmp_path)
