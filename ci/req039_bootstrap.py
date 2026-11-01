"""Protected same-runner setup for the explicitly selected REQ-039 consumer.

Setup creates empty journals and an honestly unchecked local observation. It cannot approve the
candidate producer/delegation, create pending work, publish remotely or establish verification.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from exacly import (
    admission,
    automation_authorization,
    automation_controller,
    automation_revocations,
    controller_store,
    dispatch_store,
    executor_store,
    handoff_git,
    handoff_store,
    live_pilot_evidence,
    output_association,
    planning,
    producer_selection,
    publication_key,
    remote_publication,
    resource_guard,
    standing_authorization,
    state_writer,
    worker_integration,
)
from exacly.applicability import Target
from exacly.execution_store import Deadline, invalid_constant, unique_object
from exacly.refresh import Run, refresh

Doc = dict[str, Any]
REPOSITORY = "whiteredhouse/exacly"
REPOSITORY_ID = 1411700094
BRANCH = "pilot/req039-consumer"
SOURCE = "fixtures/unattended-pilot/inputs/source.txt"
RESULT = "fixtures/unattended-pilot/results/source-digests.json"
MODEL = "fixtures/unattended-pilot/model.yaml"
WORKFLOW = ".github/workflows/req039-evidence.yml"
RECORD = "REQ-001"
NAMESPACE = "req039-consumer"
FIXED_CASE = "verification/req039_inventory_test.py::test_pilot_inventory_matches_approved_fixture"
MAX_PRODUCER_POLLS = 20
RESERVED_CALLS = 160
RESERVATION_ALLOCATION = {
    "six_native_phases": 48,
    "recovery": 16,
    "producer_reads": 20,
    "baseline_and_negative_reads": 7,
    "authority_preflights": 69,
}
RESERVED_MILLISECONDS = RESERVED_CALLS * 45000
ORIGINAL_GOAL = planning.digest(
    {
        "repository_id": REPOSITORY_ID,
        "branch": BRANCH,
        "record": RECORD,
        "namespace": NAMESPACE,
        "dimension": "verification",
        "source": SOURCE,
        "result": RESULT,
    }
)
OBSERVER_PATHS = [
    ".github",
    "ci",
    "conftest.py",
    MODEL,
    "mise.toml",
    "pyproject.toml",
    "python",
    "skills",
    "spec",
    "tests",
    "uv.lock",
    "verification",
]
PROTECTED_PATHS = sorted([".github/", "ci/", MODEL, "python/", "skills/", "spec/", "tests/", "verification/"])
PRODUCER = {
    "branch": BRANCH,
    "workflow": {
        "path": WORKFLOW,
        "job_id": "observe",
        "job_name": "Observe REQ-039 fixture",
        "steps": {
            "verification": "Run fixed fixture verification",
            "package": "Package verification evidence",
            "upload": "Upload verification evidence",
        },
    },
    "artifact": {"name_prefix": "exacly-req039-evidence"},
}


def require(value: Any, reason: str) -> None:
    if not value:
        raise ValueError(reason)


def private_directory(path: Path, *, create: bool = False) -> Path:
    require(path.is_absolute() and path.resolve() == path, "pilot storage must be canonical without aliases")
    if create:
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        "pilot storage must be an owner-private 0700 directory",
    )
    return path


def write_json(path: Path, value: Doc, *, replace: bool = False) -> None:
    private_directory(path.parent)
    content = (planning.canonical(value) + "\n").encode()
    require(len(content) <= 32 * 1024**2, "pilot document exceeds its bound")
    if not replace:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=".req039-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def read_json(path: Path) -> Doc:
    private_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_size <= 32 * 1024**2,
            "pilot document is not a protected bounded regular file",
        )
        content = bytearray()
        while block := os.read(descriptor, min(65536, 32 * 1024**2 + 1 - len(content))):
            content.extend(block)
            require(len(content) <= 32 * 1024**2, "pilot document grew beyond its bound")
        after = os.fstat(descriptor)
        current = path.lstat()
        require(
            len(
                {
                    (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
                    for value in (before, after, current)
                }
            )
            == 1,
            "pilot document changed while read",
        )
        result = json.loads(content, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        require(isinstance(result, dict), "pilot document must be an object")
        return dict(result)
    finally:
        os.close(descriptor)


def import_base(checkout: Path, repository: Path, revision: str, *, verify_revisions: tuple[str, ...] = ()) -> None:
    """Copy only bounded regular object bytes; import no config, refs, hooks, filters or helpers."""
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "pilot base must be a full exact commit identity")
    source = checkout / ".git" / "objects"
    require(source.resolve() == source and source.is_dir(), "trusted checkout needs a plain local object store")
    total = count = 0
    deadline = Deadline(120)
    for path in sorted(source.rglob("*")):
        deadline.check()
        info = path.lstat()
        relative = path.relative_to(source).as_posix()
        require(not stat.S_ISLNK(info.st_mode), "checkout object aliases are unsupported")
        if stat.S_ISDIR(info.st_mode):
            require(
                relative in {"pack", "info"} or re.fullmatch(r"[0-9a-f]{2}", relative),
                "unsupported checkout object directory",
            )
            continue
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid(),
            "checkout objects must be owned regular files",
        )
        require(
            re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{38}|pack/pack-[0-9a-f]{40}\.(?:pack|idx|rev)", relative),
            "unsupported object metadata; no alternates, promisor stores or helpers",
        )
        count += 1
        total += info.st_size
        require(count <= 100000 and total <= 512 * 1024**2, "checkout object import exceeds its bound")
        destination = repository / "objects" / relative
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        incoming = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        outgoing = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            copied = 0
            while block := os.read(incoming, 65536):
                deadline.check()
                copied += len(block)
                require(copied <= info.st_size, "checkout object changed while copying")
                offset = 0
                while offset < len(block):
                    offset += os.write(outgoing, block[offset:])
            found = os.fstat(incoming)
            require(
                (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)
                == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                and copied == info.st_size,
                "checkout object changed while copying",
            )
            os.fsync(outgoing)
        finally:
            os.close(incoming)
            os.close(outgoing)
    handoff_git.inspect_repository(repository)
    for selected in (revision, *verify_revisions):
        require(re.fullmatch(r"[0-9a-f]{40}", selected), "trusted closure needs a full exact revision")
        handoff_git._verify_closure(repository, selected, deadline)
    directories = [item for item in (repository / "objects").rglob("*") if item.is_dir()]
    for directory in [*sorted(directories, key=lambda item: len(item.parts), reverse=True), repository / "objects"]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    handoff_git.compare_and_swap(repository, "refs/heads/approved-base", None, revision)
    handoff_git.synchronize(repository, "refs/heads/approved-base")


def runtime_root(path: Path) -> Doc:
    """Build a detached reviewed system-Python runtime; no workspace or host runtime mount."""
    private_directory(path, create=True)
    python = Path("/usr/bin/python3")
    details = json.loads(
        subprocess.run(
            [
                str(python),
                "-I",
                "-S",
                "-c",
                "import json,sysconfig;print(json.dumps({'stdlib':sysconfig.get_path('stdlib'),"
                "'extensions':sysconfig.get_config_var('DESTSHARED')}))",
            ],
            capture_output=True,
            check=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin"},
        ).stdout
    )
    copied: set[Path] = set()
    total = 0

    def copy(source: Path) -> None:
        nonlocal total
        target = path / source.relative_to("/")
        if target in copied:
            return
        require(source.is_file(), "trusted runtime component is unavailable")
        total += source.stat().st_size
        require(total <= 1024**3 and len(copied) < 100000, "trusted runtime exceeds its bound")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=True)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
        copied.add(target)

    def dependencies(binary: Path) -> None:
        result = subprocess.run(
            ["/usr/bin/ldd", str(binary)],
            capture_output=True,
            check=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        ).stdout.decode()
        require("not found" not in result, "trusted runtime library is unavailable")
        for line in result.splitlines():
            found = re.search(r"(?:=>\s*)?(/[^\s]+)", line)
            if found:
                copy(Path(found[1]))

    copy(python)
    dependencies(python)
    for source in sorted(Path(details["stdlib"]).rglob("*.py")):
        if not any(part in {"dist-packages", "site-packages", "__pycache__", "test", "tests"} for part in source.parts):
            copy(source)
    extensions = {
        "_bisect",
        "_blake2",
        "_contextvars",
        "_ctypes",
        "_datetime",
        "_hashlib",
        "_json",
        "_opcode",
        "_pickle",
        "_posixsubprocess",
        "_random",
        "_sha1",
        "_sha2",
        "_sha3",
        "_sha256",
        "_sha512",
        "_socket",
        "_struct",
        "binascii",
        "fcntl",
        "math",
        "resource",
        "select",
        "unicodedata",
        "zlib",
    }
    for source in sorted(Path(details["extensions"]).glob("*.so")):
        if source.name.split(".", 1)[0] in extensions:
            copy(source)
            dependencies(source)
    for directory in ("proc", "dev", "tmp", "work", "inputs", "outputs", "run"):
        (path / directory).mkdir(exist_ok=True)
    from exacly import isolation

    return {
        "schema_version": "0.1.0",
        "backend": isolation.BACKEND,
        "rootfs": str(path),
        "rootfs_digest": isolation.rootfs_digest(path),
        "python": "/usr/bin/python3",
        "bubblewrap": "/usr/bin/bwrap",
    }


def units(attempts: int, milliseconds: int) -> Doc:
    return {"attempts": attempts, "wall_time_ms": milliseconds, "model_requests": 0, "model_spend_microunits": 0}


def bootstrap(
    root: Path,
    checkout: Path,
    base: str,
    context: Doc,
    source: admission.Source,
    guard: resource_guard.Guard,
    *,
    baseline_run: int | None = None,
    publisher: Doc | None = None,
) -> Doc:
    """Prepare the exact candidate configuration once; no authority is released here."""
    private_directory(root)
    names = (
        "state",
        "ledger",
        "anchor",
        "dispatch",
        "executor",
        "outputs",
        "git",
        "handoff",
        "remote",
        "association",
        "accounting",
        "revocations",
        "keys",
        "controller",
        "configuration",
        "runtime",
    )
    directories = {name: private_directory(root / name, create=True) for name in names}
    trust = admission.propose_policy(
        source,
        REPOSITORY,
        base,
        [MODEL],
        producer=PRODUCER,
        observer_paths=OBSERVER_PATHS,
        name="req039-fixed-push-producer",
        branch_protection="not_required",
    )
    require(
        trust["repository"] == {"id": REPOSITORY_ID, "full_name": REPOSITORY}, "consumer repository identity differs"
    )
    docs = admission.model_files(source, "/repos/" + REPOSITORY, trust["model"], dt.datetime.now(dt.UTC).date())
    approved_model = admission.validate_model(trust, docs, dt.datetime.now(dt.UTC).date())
    require(
        approved_model.graph.namespace == NAMESPACE,
        "fixed pilot requires the independently approved req039-consumer model namespace",
    )
    selected_record = approved_model.records.get(RECORD)
    require(
        isinstance(selected_record, dict)
        and selected_record.get("type") == "Requirement"
        and selected_record.get("observe", {}).get("tests")
        == {"workflow": WORKFLOW, "job": "observe", "cases": [FIXED_CASE]},
        "fixed pilot requires the exact approved REQ-001 fixture verification binding",
    )
    readiness = {
        "name": "req039-inventory-verification",
        "version": "1",
        "requirements": [{"record": RECORD, "dimensions": ["verification"]}],
    }
    target = Target({"context": "req039-pilot", "model_revision": base, "revision": base})
    current = dt.datetime.now(dt.UTC)
    state_path = directories["state"] / "current.json"
    state = refresh(docs, {}, Run(revision=base, model_revision=base, context="req039-pilot", observed_at=current))
    require(state.get("namespace") == NAMESPACE, "fixed pilot observation namespace differs from its approved goal")
    state_writer.publish(state_writer.read(state_path), state)
    plan = planning.create({**docs, "__preflight__": state}, readiness, target, current.date())
    require(
        plan["namespace"] == NAMESPACE and len(plan["actions"]) == 1 and plan["actions"][0]["record"] == RECORD,
        "preflight did not select one unchecked consumer verification goal",
    )
    repository = directories["git"] / "source.git"
    repository_identity = handoff_git.initialize_repository(repository)
    import_base(checkout, repository, base, verify_revisions=(context["coordinator_revision"],))
    implementation = live_pilot_evidence.implementation_fingerprint(
        checkout, repository, context["coordinator_revision"]
    )
    prepared = worker_integration.prepare(
        repository,
        repository_id=repository_identity["instance_id"],
        revision=base,
        allowed_paths=[SOURCE],
        protected_paths=PROTECTED_PATHS,
        model_digest=trust["model"]["digest"],
    )
    ledger = directories["ledger"] / "executions.sqlite3"
    anchor = controller_store.initialize_ledger_anchor(
        directories["anchor"] / "ledger.json", ledger=ledger, create_ledger=True
    )
    dispatch_journal = directories["dispatch"] / "journal.sqlite3"
    dispatch_id = dispatch_store.initialize(dispatch_journal)["journal_id"]
    executor_journal = directories["executor"] / "journal.sqlite3"
    executor_id = executor_store.initialize(executor_journal, ledger=ledger, max_concurrency=1)["journal_id"]
    handoff_journal = directories["handoff"] / "journal.sqlite3"
    handoff_id = handoff_store.initialize(handoff_journal, repository=repository_identity)["journal_id"]
    remote_journal = directories["remote"] / "journal.sqlite3"
    remote_id = remote_publication.initialize(remote_journal)
    association = output_association.initialize(
        directories["association"] / "journal.sqlite3",
        ledger=ledger,
        state_path=state_path,
        handoff_journal=handoff_journal,
        handoff_journal_id=handoff_id,
    )
    accounting = directories["accounting"] / "journal.sqlite3"
    accounting_id = standing_authorization.initialize(accounting)["store_id"]
    revocations = directories["revocations"] / "journal.sqlite3"
    revocation_id = automation_revocations.initialize(revocations)
    key_path = directories["keys"] / "publication.key"
    key_id = publication_key.initialize(key_path)
    workflow_info = source.get_json(f"/repos/{REPOSITORY}/actions/workflows/req039-evidence.yml")
    require(
        type(workflow_info.get("id")) is int and workflow_info["id"] > 0 and workflow_info.get("path") == WORKFLOW,
        "approved producer workflow identity is unavailable",
    )
    producer = {
        "schema_version": "0.1.0",
        "workflow_id": workflow_info["id"],
        "policy": trust,
        "policy_digest": planning.digest(trust),
    }
    selected = producer_selection.select(source, producer, base, run_id=baseline_run, timeout=30)
    require(selected["status"] == "selected", "exact initial failed-producer selection is unavailable or ambiguous")
    baseline = selected["selection"]
    native_stores = {
        "dispatch": {"path": str(dispatch_journal), "id": dispatch_id},
        "execute": {"path": str(executor_journal), "id": executor_id},
        "handoff": {"path": str(handoff_journal), "id": handoff_id},
        "publish": {"path": str(remote_journal), "id": remote_id},
        "associate": {"path": association["journal"], "id": association["journal_id"]},
        "complete": {"path": str(ledger), "id": anchor["digest"]},
    }
    contracts = {
        "model": plan["inputs"]["config"]["digest"],
        "criteria": plan["actions"][0]["criteria_digest"],
        "bindings": plan["actions"][0]["bindings_digest"],
        "readiness": planning.digest(readiness),
        "trust": planning.digest(trust),
        "observer": trust["observer"]["digest"],
    }
    dispatch_policy = {
        "schema_version": "0.1.0",
        "name": "req039-one-consumer-dispatch",
        "version": "1",
        "adapter": "github-issue-v1",
        "repository": trust["repository"],
        "issue": {
            "title": "REQ-039 approved inventory verification",
            "body": "Collect exact fixture inventory under the independently approved same-runner pilot.",
        },
        "max_attempts": 1,
        "storage": {
            "journal": str(dispatch_journal),
            "journal_id": dispatch_id,
            "ledger": str(ledger),
            "state": str(state_path),
        },
    }
    executor_policy = {
        "schema_version": "0.1.0",
        "name": "req039-fixed-inventory",
        "version": "1",
        "adapter": "local-bubblewrap-v1",
        "storage": {
            "journal": str(executor_journal),
            "journal_id": executor_id,
            "ledger": str(ledger),
            "state": str(state_path),
            "output_root": str(directories["outputs"]),
        },
        "runtime": runtime_root(directories["runtime"] / "rootfs"),
        "argv": list(prepared.argv),
        "expected_outputs": list(prepared.expected_outputs),
        "max_attempts": 1,
        "timeout_seconds": 30,
        "permitted_repository_effects": [],
        "resource_guard": guard.descriptor,
    }
    handoff_policy = {
        "schema_version": "0.1.0",
        "name": "req039-exact-fixture-result",
        "version": "1",
        "adapter": "local-git-v1",
        "storage": {
            "journal": str(handoff_journal),
            "journal_id": handoff_id,
            "executor_journal": str(executor_journal),
            "executor_journal_id": executor_id,
            "repository": str(repository),
            "repository_id": repository_identity["instance_id"],
        },
        "allowed_paths": [RESULT],
    }
    config_path = directories["configuration"] / "delegation.json"
    components: Doc = {
        "schema_version": "0.1.0",
        "profile": "single-producer-local-v1",
        "required_members": ["producer"],
        "configuration_path": str(directories["configuration"] / "components.json"),
        "date": current.date().isoformat(),
        "state": str(state_path),
        "ledger": str(ledger),
        "initial_binding_id": None,
        "ledger_anchor": anchor,
        "controller": {"journal": str(directories["controller"] / "workflow.sqlite3"), "journal_id": uuid.uuid4().hex},
        "dispatch_policy": dispatch_policy,
        "executor_policy": executor_policy,
        "handoff_policy": handoff_policy,
        "handoff": {
            "ref": "refs/heads/req039-local-output",
            "expected_revision": None,
            "mappings": {"artifacts/source-digests.json": RESULT},
            "message": "Retain approved REQ-039 fixture inventory\n",
            "author": {
                "name": "Exacly bounded pilot",
                "email": "req039-pilot@users.noreply.github.com",
                "time": int(current.timestamp()),
            },
        },
        "remote": {
            "schema_version": "0.1.0",
            "journal": str(remote_journal),
            "journal_id": remote_id,
            "handoff_journal": str(handoff_journal),
            "handoff_journal_id": handoff_id,
            "repository_id": str(REPOSITORY_ID),
            "adapter": "github-git-atomic-v1",
            "ref": "refs/heads/" + BRANCH,
            "expected_revision": base,
            "operation_marker_ref": "refs/tags/req039-operations/" + context["nonce"],
            "operation_key": str(key_path),
            "operation_key_id": key_id,
        },
        "association": association,
        "producer": producer,
        "native_stores": native_stores,
        "costs": {kind: units(1, 45000) for kind in native_stores},
        "control_bounds_ms": {
            kind: {method: 45000 for method in ("inspect", "invoke", "recover", "resume", "cancel")}
            for kind in native_stores
        },
        "baseline": {
            "schema_version": "0.1.0",
            "producer_contract_digest": planning.digest(producer),
            "target": target.data,
            "plan_digest": plan["digest"],
            "action_id": plan["actions"][0]["id"],
            "readiness_digest": planning.digest(readiness),
            "trust_digest": planning.digest(trust),
            "run_id": baseline["run_id"],
            "attempt": baseline["attempt"],
        },
    }
    if publisher is not None:
        components["publisher"] = publisher
    if publisher is not None:
        components["authority_preflight"] = {
            "schema_version": "0.1.0",
            "adapter_id": "req039-authority-preflight-v1",
            "repository": trust["repository"],
            "coordinator": {
                "run_id": context["run_id"],
                "run_attempt": context["run_attempt"],
                "job_id": context["host"]["job_id"],
                "boot_id": context["host"]["boot_id"],
                "nonce": context["nonce"],
                "workflow_id": context["workflow_id"],
                "revision": context["coordinator_revision"],
            },
            "producer_policy_digest": planning.digest(trust),
            "resource_guard_digest": guard.digest,
            "publisher": publisher,
            "approval_channel": {"repository": REPOSITORY, "issue": 93, "user_id": 1679024},
            "wall_time_ms": 45000,
            "max_producer_reads": MAX_PRODUCER_POLLS,
        }
    components["negatives"] = {
        "schema_version": "0.1.0",
        "producer_contract_digest": planning.digest(producer),
        "target": target.data,
        "plan_digest": plan["digest"],
        "action_id": plan["actions"][0]["id"],
        "readiness_digest": planning.digest(readiness),
        "trust_digest": planning.digest(trust),
        "cases": ["unavailable-producer", "wrong-revision", "wrong-attempt", "changed-criteria", "changed-verifier"],
        "unavailable_run_id": 1,
        "wrong_attempt": 2,
        "criteria_model_digest": planning.digest(
            {"probe": "unapproved-model-expectation", "approved": trust["model"]["digest"]}
        ),
        "verifier_digest": planning.digest(
            {"probe": "unapproved-verifier-expectation", "approved": trust["observer"]["digest"]}
        ),
        "wall_time_ms": 45000,
    }
    components["configuration_digest"] = planning.digest(components)
    objective = {
        "schema_version": "0.1.0",
        "id": planning.digest({"original_goal": ORIGINAL_GOAL}),
        "original_goal": ORIGINAL_GOAL,
    }
    scope_tests = {
        "budget": {
            "schema_version": "0.1.0",
            "original_goal": ORIGINAL_GOAL,
            "state": str(state_path),
            "native_configuration_digest": components["configuration_digest"],
            "wall_time_ms": 300000,
        },
        "expiry_semantics": "genuinely-expired-independent-delegation-version",
        "revocation_semantics": "revoke-main-delegation-after-native-completion-and-replay",
        "phase": "after-native-completion-and-replay",
        "new_objectives": False,
        "external_ref_drift_demonstrated": False,
    }
    scope_tests["remote_precondition"] = {
        "schema_version": "0.1.0",
        "adapter_id": "native-remote-precondition-v1",
        "repository": trust["repository"],
        "wall_time_ms": 45000,
        "request_base": {
            **components["remote"],
            "operation_marker_ref": "refs/tags/req039-operations/" + context["nonce"] + "-precondition",
        },
    }
    delegation = {
        "schema_version": "0.1.0",
        "id": "req039-same-runner-" + context["nonce"],
        "issuer": "github-user:1679024",
        "approval_id": "req039-approval-" + context["nonce"],
        "config_path": str(config_path),
        "accounting": {"path": str(accounting), "store_id": accounting_id},
        "revocations": {"path": str(revocations), "store_id": revocation_id},
        "objective": objective,
        "limits": units(RESERVED_CALLS, RESERVED_MILLISECONDS),
        "not_before": (current - dt.timedelta(seconds=5)).isoformat(),
        "expires_at": context["deadline_at"],
        "contract": contracts,
        "native_configuration_digest": components["configuration_digest"],
        "repository": trust["repository"],
        "base_revision": base,
        "allowed_paths": [SOURCE, RESULT],
        "protected_paths": PROTECTED_PATHS,
        "destinations": [{"repository_id": REPOSITORY_ID, "ref": "refs/heads/" + BRANCH}],
        "actions": [plan["actions"][0]["id"]],
        "effects": {
            kind: {
                "adapter_id": automation_controller.ADAPTERS[kind],
                "capabilities": sorted(
                    ["intent-before-effects-v1", automation_controller.ADAPTERS[kind]]
                    + (["finish-prepared-v1"] if kind in {"dispatch", "handoff", "associate"} else [])
                ),
                "native_store": native_stores[kind],
                "max_cost": components["costs"][kind],
                "cancel_inflight": False,
                "recover_after_revocation": True,
                "recover_after_expiry": True,
            }
            for kind in native_stores
        },
        "reads": {
            "producer-observe": {
                "adapter_id": "native-producer-observer-v1",
                "max_wall_time_ms": 30000,
                "contract_digest": planning.digest(producer),
            },
            "baseline-admission": {
                "adapter_id": "native-baseline-admission-v1",
                "max_wall_time_ms": 30000,
                "contract_digest": planning.digest(components["baseline"]),
            },
        },
    }
    for purpose, key, adapter in (
        ("authority-preflight", "authority_preflight", "req039-authority-preflight-v1"),
        ("negative-admission", "negatives", "native-negative-admission-v1"),
    ):
        if key in components:
            delegation["reads"][purpose] = {
                "adapter_id": adapter,
                "max_wall_time_ms": 45000,
                "contract_digest": planning.digest(components[key]),
            }
    delegation["reads"]["remote-precondition"] = {
        "adapter_id": "native-remote-precondition-v1",
        "max_wall_time_ms": 45000,
        "contract_digest": planning.digest(scope_tests["remote_precondition"]),
    }
    delegation["reads"]["budget-exhaustion"] = {
        "adapter_id": "req039-budget-exhaustion-v1",
        "max_wall_time_ms": 300000,
        "contract_digest": planning.digest(scope_tests["budget"]),
    }
    automation_authorization.validate_delegation(delegation)
    expired_delegation = json.loads(planning.canonical(delegation))
    expired_delegation.update(
        id=delegation["id"] + "-expired-proof",
        config_path=str(directories["configuration"] / "expired-delegation.json"),
        not_before=(current - dt.timedelta(days=2)).isoformat(),
        expires_at=(current - dt.timedelta(days=1)).isoformat(),
    )
    automation_authorization.validate_delegation(expired_delegation)
    write_json(Path(expired_delegation["config_path"]), expired_delegation)
    scope_tests["expired_delegation"] = expired_delegation
    standing_authorization.enroll(accounting, store_id=accounting_id, objective=objective, limits=delegation["limits"])
    authority_identity = {
        "schema_version": "0.1.0",
        "delegation_id": delegation["id"],
        "delegation_digest": planning.digest(delegation),
        "issuer": delegation["issuer"],
        "approval_id": delegation["approval_id"],
        "store": {"path": str(accounting), "id": accounting_id},
        "objective": {key: objective[key] for key in ("id", "original_goal")},
    }
    contract = automation_controller.native_contract(
        plan=plan,
        record=RECORD,
        readiness_policy=readiness,
        trust_policy=trust,
        components=components,
        authorizer_identity=authority_identity,
        host=context["host"],
        deadline_at=context["deadline_at"],
    )
    workflow = automation_controller.initialize(Path(components["controller"]["journal"]), contract)
    require(workflow["journal_id"] == components["controller"]["journal_id"], "initialized controller identity differs")
    bundle = {
        "context": context,
        "implementation": implementation,
        "scope_tests": scope_tests,
        "base": base,
        "documents": docs,
        "plan": plan,
        "readiness": readiness,
        "trust": trust,
        "components": components,
        "delegation": delegation,
        "contract": contract,
        "worker_identity": prepared.identity,
        "resource_guard": guard.descriptor,
        "baseline_selection": baseline,
    }
    for name, value in (
        ("producer-policy", trust),
        ("delegation", delegation),
        ("components", components),
        ("plan", plan),
        ("contract", contract),
    ):
        write_json(directories["configuration"] / (name + ".json"), value)
    write_json(root / "bootstrap.json", bundle)
    return bundle
