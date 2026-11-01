"""Exact source snapshots and one fixed, deterministic snapshot-inventory recipe.

This preparation API is not an executor, approval mechanism or evidence producer. Its immutable
bytes can be supplied to ``executor.Request.inputs`` under separately approved executor policy.
Real isolation, resource containment, lifecycle supervision and verification remain mandatory.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from exacly import (
    execution_store,
    executor,
    executor_contract,
    executor_local,
    handoff_git,
    isolation,
    planning,
    resource_guard,
)
from exacly.executor_interfaces import CoordinatorBackends, InvocationLease, WorkerSpec

VERSION = "0.1.0"
RECIPE = "snapshot-inventory-v1"
SCRIPT = ".exacly-worker.py"
MANIFEST = ".exacly-worker-inputs.json"
OUTPUT = "source-digests.json"
ARGV = ("/usr/bin/python3", "-I", "-S", "/inputs/" + SCRIPT)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROTECTED_PARTS = {".github", ".git", "spec", "tests", "ci", "skills", "state"}
_PROTECTED_FILES = {
    "agents.md",
    "conftest.py",
    "pyproject.toml",
    "uv.lock",
    "mise.toml",
    ".gitattributes",
    ".gitmodules",
}
_SCRIPT = b'''"""Inventory approved snapshot bytes; no source code is executed or verified."""
import hashlib
import json
from pathlib import Path

root = Path("/inputs")
manifest = json.loads((root / ".exacly-worker-inputs.json").read_bytes())
files = []
for item in manifest["files"]:
    data = (root / item["input"]).read_bytes()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if len(data) != item["size"] or digest != item["digest"]:
        raise SystemExit("approved snapshot bytes changed")
    files.append({"path": item["path"], "size": len(data), "digest": digest})
result = {"schema_version": "0.1.0", "recipe": "snapshot-inventory-v1",
          "snapshot_digest": manifest["snapshot_digest"], "files": files,
          "verification_established": False, "evidence_admitted": False}
Path("/outputs/source-digests.json").write_text(
    json.dumps(result, sort_keys=True, separators=(",", ":")) + "\\n")
'''


@dataclass(frozen=True)
class PreparedWorker:
    """Immutable preparation result, carrying no execution or completion authority."""

    _files: tuple[tuple[str, bytes], ...]
    _identity: bytes

    @property
    def inputs(self) -> Mapping[str, bytes]:
        return MappingProxyType(dict(self._files))

    @property
    def identity(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self._identity)
        return result

    @property
    def argv(self) -> tuple[str, ...]:
        return ARGV

    @property
    def expected_outputs(self) -> tuple[str, ...]:
        return (OUTPUT,)


def _paths(value: Sequence[str], *, directories: bool) -> list[str]:
    execution_store.require(
        isinstance(value, (list, tuple)) and 0 < len(value) <= isolation.MAX_INPUT_FILES,
        "worker paths require a nonempty bounded list of literal approved paths",
    )
    result: list[str] = []
    for item in value:
        execution_store.require(isinstance(item, str), "worker paths must be strings")
        directory = directories and item.endswith("/")
        result.append(handoff_git.relative_path(item[:-1] if directory else item) + ("/" if directory else ""))
    execution_store.require(result == sorted(set(result)), "worker paths must be sorted and unique")
    execution_store.require(
        len({item.lower() for item in result}) == len(result), "worker paths have ambiguous case aliases"
    )
    return result


def _protected(path: str, protected: Sequence[str]) -> bool:
    folded = path.lower()
    parts = folded.split("/")
    return (
        any(part in _PROTECTED_PARTS or part.startswith(".exacly") for part in parts)
        or parts[-1] in _PROTECTED_FILES
        or folded.startswith("python/src/exacly/")
        or any(folded == item.lower() or (item.endswith("/") and folded.startswith(item.lower())) for item in protected)
    )


def prepare(
    repository: Path,
    *,
    repository_id: str,
    revision: str,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str],
    model_digest: str,
    timeout: float = 30,
) -> PreparedWorker:
    """Read exact approved regular blobs from an initialized, sealed local bare store.

    The trusted caller supplies the repository UUID, full commit OID, explicit source files and
    independently approved consumer-specific protected paths. Protected directory prefixes end in
    ``/``. Built-in model, verifier and reserved worker paths cannot be selected. This function
    never reads a worktree or resolves a ref, checks out source, runs filters or launches a worker.
    The separate approved model digest is retained without treating source bytes as model input.
    """
    budget = execution_store.Deadline(timeout)
    paths = _paths(allowed_paths, directories=False)
    protected = _paths(protected_paths, directories=True)
    execution_store.require(
        isinstance(model_digest, str) and _DIGEST.fullmatch(model_digest), "invalid approved worker model digest"
    )
    execution_store.require(
        isinstance(revision, str) and handoff_git.OID.fullmatch(revision),
        "worker requires a full exact commit identity",
    )
    execution_store.require(
        not any(_protected(path, protected) for path in paths), "worker source includes protected paths"
    )
    identity = handoff_git.inspect_repository(repository, timeout=budget.remaining())
    execution_store.require(identity["instance_id"] == repository_id, "worker source repository identity differs")
    handoff_git._verify_closure(repository, revision, budget)
    commit = handoff_git._object(repository, revision, "commit", budget)
    first = commit.split(b"\n", 1)[0]
    execution_store.require(re.fullmatch(rb"tree [0-9a-f]{40}", first), "worker commit has no exact tree identity")
    tree = first[5:].decode()
    files: dict[str, bytes] = {}
    roster: list[dict[str, Any]] = []
    total = 0
    trees: dict[str, dict[bytes, tuple[bytes, str]]] = {}
    for path in paths:
        current = tree
        parts = path.split("/")
        for index, part in enumerate(parts):
            budget.check()
            if current not in trees:
                entries = handoff_git._tree(handoff_git._object(repository, current, "tree", budget))
                execution_store.require(
                    len({name.lower() for name in entries}) == len(entries), "worker Git tree contains case aliases"
                )
                trees[current] = entries
            found = trees[current].get(part.encode())
            execution_store.require(found is not None, "approved worker source path does not exist: " + path)
            assert found is not None
            mode, current = found
            if index < len(parts) - 1:
                execution_store.require(mode == b"40000", "worker source traverses a non-directory Git entry")
            else:
                execution_store.require(mode in {b"100644", b"100755"}, "worker source must select regular Git blobs")
        # Bound the read by the executor's total snapshot capacity, not merely Git's object limit.
        size = handoff_git._run(repository, ["cat-file", "-s", current], budget, limit=128).strip()
        execution_store.require(
            size.isdigit() and total + int(size) <= isolation.MAX_INPUT_BYTES,
            "worker source snapshots exceed the byte limit",
        )
        data = handoff_git._object(repository, current, "blob", budget)
        total += len(data)
        name = "source/" + path
        files[name] = data
        roster.append(
            {
                "path": path,
                "input": name,
                "mode": mode.decode(),
                "oid": current,
                **executor_contract.roster({path: data})[0],
            }
        )
    snapshot = {
        "schema_version": VERSION,
        "recipe": RECIPE,
        "repository": identity,
        "revision": revision,
        "tree": tree,
        "model_digest": model_digest,
        "allowed_paths": paths,
        "protected_paths": protected,
        "files": roster,
    }
    snapshot_digest = planning.digest(snapshot)
    manifest = {"snapshot_digest": snapshot_digest, "files": roster}
    files[MANIFEST] = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    files[SCRIPT] = _SCRIPT
    executor_contract.snapshots(files)
    result = {
        **snapshot,
        "snapshot_digest": snapshot_digest,
        "recipe_digest": executor_contract.roster({SCRIPT: _SCRIPT})[0]["digest"],
        "inputs": executor_contract.roster(files),
        "argv": list(ARGV),
        "expected_outputs": [OUTPUT],
        "claims": {"execution_authorized": False, "verification_established": False, "evidence_admitted": False},
    }
    execution_store.require(
        handoff_git.inspect_repository(repository, timeout=budget.remaining()) == identity,
        "worker source repository identity changed during preparation",
    )
    return PreparedWorker(tuple(sorted(files.items())), json.dumps(result, sort_keys=True).encode())


def bind(request: executor.Request, prepared: PreparedWorker, *, guard: resource_guard.Guard) -> executor.Request:
    """Bind exact prepared bytes only to an already approved fixed-recipe executor policy.

    Source and model identity remain distinct, and this helper never rewrites policy, target,
    binding, deadline or attempt limits. Preview and exact native authorization remain required.
    """
    execution_store.require(isinstance(prepared, PreparedWorker), "an explicit prepared worker is required")
    identity = prepared.identity
    selected = executor_contract.validate_policy(request.executor_policy)
    execution_store.require(
        isinstance(guard, resource_guard.Guard) and selected.get("resource_guard") == guard.descriptor,
        "approved executor policy selects another exact resource envelope",
    )
    execution_store.require(
        selected["argv"] == list(ARGV)
        and selected["expected_outputs"] == [OUTPUT]
        and selected["runtime"]["python"] == ARGV[0],
        "approved executor policy differs from the fixed inventory recipe",
    )
    execution_store.require(
        identity["revision"] == request.target.data["revision"]
        and identity["model_digest"] == request.trust_policy["model"]["digest"],
        "prepared worker source or approved model differs from the native execution contract",
    )
    inputs = prepared.inputs
    execution_store.require(
        inputs.get(SCRIPT) == _SCRIPT and identity["inputs"] == executor_contract.roster(inputs),
        "prepared worker bytes differ from their retained identities",
    )
    snapshot = {
        key: identity[key]
        for key in (
            "schema_version",
            "recipe",
            "repository",
            "revision",
            "tree",
            "model_digest",
            "allowed_paths",
            "protected_paths",
            "files",
        )
    }
    execution_store.require(
        planning.digest(snapshot) == identity["snapshot_digest"]
        and json.loads(inputs[MANIFEST])
        == {"snapshot_digest": identity["snapshot_digest"], "files": identity["files"]},
        "prepared worker snapshot identity or fixed manifest changed",
    )
    return replace(request, inputs=inputs)


class _GuardedLocalWorker(executor_local.LocalWorkerLifecycle):
    """The fixed local lifecycle, with actual aggregate caps checked at both release gates."""

    def __init__(self, domain: object, guard: resource_guard.Guard) -> None:
        super().__init__(domain)
        self.guard = guard

    def supervise(
        self,
        spec: WorkerSpec,
        *,
        on_worker: Callable[[dict[str, Any]], None],
        on_boundary: Callable[[dict[str, Any]], None],
        is_cancelled: Callable[[], bool],
        lease: InvocationLease,
    ) -> dict[str, Any]:
        self.guard.check()

        def checked(callback: Callable[[dict[str, Any]], None], identity: dict[str, Any]) -> None:
            self.guard.check_process(identity["pid"])
            callback(identity)
            self.guard.check_process(identity["pid"])

        return super().supervise(
            spec,
            on_worker=lambda identity: checked(on_worker, identity),
            on_boundary=lambda identity: checked(on_boundary, identity),
            is_cancelled=is_cancelled,
            lease=lease,
        )


def guarded_backends(guard: resource_guard.Guard) -> CoordinatorBackends:
    """Construct only reviewed local adapters; native recovery and cancellation remain native."""
    execution_store.require(isinstance(guard, resource_guard.Guard), "an actual kernel resource guard is required")
    guard.check()
    storage = executor_local.LocalAttemptStorage()
    return CoordinatorBackends(
        storage,
        executor_local.LocalInvocationOwnership(storage.domain),
        _GuardedLocalWorker(storage.domain, guard),
    )


def preview(
    documents: Mapping[str, Any],
    state_path: Path,
    request: executor.Request,
    *,
    prepared: PreparedWorker,
    guard: resource_guard.Guard,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Preview the exact fixed worker under checked caps; this grants no execution authority."""
    return executor.preview(
        documents,
        state_path,
        bind(request, prepared, guard=guard),
        lock_timeout=lock_timeout,
        backends=guarded_backends(guard),
    )


def apply(
    journal: Path,
    ledger: Path,
    documents: Mapping[str, Any],
    state_path: Path,
    request: executor.Request,
    *,
    authorization: Any,
    prepared: PreparedWorker,
    guard: resource_guard.Guard,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Run the concrete deterministic worker through exact native authorization and journals."""
    return executor.apply(
        journal,
        ledger,
        documents,
        state_path,
        bind(request, prepared, guard=guard),
        authorization=authorization,
        lock_timeout=lock_timeout,
        backends=guarded_backends(guard),
    )
