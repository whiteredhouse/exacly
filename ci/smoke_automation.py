"""Exercise installed durable dispatch, real isolated execution and local Git handoff.

The issue adapter is an in-memory fixture, and no evidence is admitted or work completed.
The detached system-Python root is a disposable smoke fixture, not a production runtime builder.
Run from outside the checkout with the selected installed distribution and Python -I.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

Doc = dict[str, Any]
REQUIRED_MODULES = (
    "exacly/__init__.py",
    "exacly/dispatch.py",
    "exacly/dispatch_store.py",
    "exacly/executor.py",
    "exacly/executor_store.py",
    "exacly/executor_interfaces.py",
    "exacly/executor_local.py",
    "exacly/executor_runtime.py",
    "exacly/isolation.py",
    "exacly/_isolation_bootstrap.py",
    "exacly/_isolation_seccomp.py",
    "exacly/_executor_worker.py",
    "exacly/handoff.py",
    "exacly/handoff_git.py",
    "exacly/handoff_store.py",
    "exacly/executions.py",
    "exacly/execution_store.py",
    "exacly/state_writer.py",
)
EXTENSIONS = {
    "_bisect",
    "_blake2",
    "_contextvars",
    "_csv",
    "_ctypes",
    "_datetime",
    "_decimal",
    "_hashlib",
    "_heapq",
    "_json",
    "_md5",
    "_opcode",
    "_pickle",
    "_posixsubprocess",
    "_queue",
    "_random",
    "_sha1",
    "_sha2",
    "_sha3",
    "_sha256",
    "_sha512",
    "_socket",
    "_struct",
    "_typing",
    "array",
    "binascii",
    "fcntl",
    "math",
    "mmap",
    "resource",
    "select",
    "termios",
    "unicodedata",
    "zlib",
}
PAYLOAD = bytes(range(256)) + b"\x00installed-automation\xff"
UNRELATED = b"Preserve unrelated reviewed source.\n"
MODEL_FILE = "spec/smoke/model.json"
MODEL_REVISION = "a" * 40
WORKFLOW = ".github/workflows/smoke.yml"
CASE = "tests/test_result.py::test_result"
REPOSITORY = {"id": 456, "full_name": "acme/automation-smoke"}
CONSOLE = (
    "import socket; "
    "exec('def deny(*args, **kwargs):\\n raise AssertionError(\"automation smoke forbids network\")'); "
    "socket.create_connection=deny; socket.socket.connect=deny; "
    "from exacly.cli import exacly_main; raise SystemExit(exacly_main())"
)


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def installation(*, require_installed: bool = True) -> Doc:
    import exacly

    module = Path(exacly.__file__).resolve()
    distribution = importlib.metadata.distribution("exacly")
    result: Doc = {"version": distribution.version, "module": str(module), "verified_modules": 0}
    if not require_installed:
        return result
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    require(direct.get("dir_info", {}).get("editable") is not True, "automation smoke refuses an editable install")
    entries = {
        str(entry): entry for entry in distribution.files or () if entry.parts[0] == "exacly" and entry.suffix == ".py"
    }
    require(set(REQUIRED_MODULES) <= set(entries), "automation modules are missing from the installed package RECORD")
    require(
        Path(str(distribution.locate_file(entries["exacly/__init__.py"]))).resolve() == module,
        "imported exacly does not belong to the installed distribution",
    )
    for name in REQUIRED_MODULES:
        if Path(name).name.startswith("_"):
            continue
        loaded = importlib.import_module(name.removesuffix(".py").replace("/", "."))
        require(
            loaded.__file__ is not None
            and Path(loaded.__file__).resolve() == Path(str(distribution.locate_file(entries[name]))).resolve(),
            f"imported automation module does not belong to the installed distribution: {name}",
        )
    for name, entry in entries.items():
        source = Path(str(distribution.locate_file(entry))).resolve()
        require(source.is_relative_to(module.parent), f"installed module escapes the imported package: {name}")
        require(entry.hash is not None and entry.hash.mode == "sha256", f"installed module has no SHA256: {name}")
        content = source.read_bytes()
        identity = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        require(
            entry.hash is not None and entry.hash.value == identity and entry.size == len(content),
            f"installed module differs from its package RECORD: {name}",
        )
    result["verified_modules"] = len(entries)
    return result


def runtime_root(root: Path, python: Path = Path("/usr/bin/python3")) -> Path:
    """Copy only explicitly selected trusted system runtime files into a detached fixture."""
    require(sys.platform == "linux" and python.is_file(), "automation smoke requires Linux and /usr/bin/python3")
    selected = subprocess.run(
        [
            str(python),
            "-I",
            "-S",
            "-c",
            "import json,sysconfig; print(json.dumps({"
            "'stdlib':sysconfig.get_path('stdlib'),'extensions':sysconfig.get_config_var('DESTSHARED')}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    paths = json.loads(selected.stdout)
    root.mkdir(mode=0o700)
    copied: set[Path] = set()

    def retain(source: Path) -> None:
        target = root / source.relative_to("/")
        if target in copied:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=True)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
        copied.add(target)

    def dependencies(binary: Path) -> None:
        linked = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True, timeout=15).stdout
        require("not found" not in linked, f"missing system runtime dependency: {binary}")
        for line in linked.splitlines():
            found = re.search(r"(?:=>\s*)?(/[^\s]+)", line)
            if found is not None:
                retain(Path(found.group(1)))

    retain(python)
    dependencies(python)
    for source in sorted(Path(paths["stdlib"]).rglob("*.py")):
        if not any(part in {"dist-packages", "site-packages", "__pycache__", "test", "tests"} for part in source.parts):
            retain(source)
    for source in sorted(Path(paths["extensions"]).glob("*.so")):
        if source.name.split(".", 1)[0] in EXTENSIONS:
            retain(source)
            dependencies(source)
    for name in ("proc", "dev", "tmp", "work", "inputs", "outputs", "run"):
        (root / name).mkdir(exist_ok=True)
    return root


def model(date: dt.date) -> Doc:
    common = {"owner": "team", "lifecycle": "active", "evidence": [], "last_reviewed_at": date.isoformat()}

    def record(identity: str, kind: str, **fields: Any) -> Doc:
        return {
            "id": identity,
            "type": kind,
            "name": identity,
            "description": "Automation smoke fixture.",
            **common,
            **fields,
        }

    return {
        "exacly": "0.2.0",
        "model": {
            "namespace": "automation-smoke",
            "product": "Automation smoke",
            "owner": "team",
            "scope": {"covers": "Disposable fixture", "excludes": []},
            "environment": "main",
            "review_interval_days": 90,
        },
        "records": [
            record("OUT-1", "Outcome", beneficiary="Teams", success_criterion="Done", evaluation_period="Quarterly"),
            record("MEAS-1", "Measure", lifecycle="proposed"),
            record("CAP-1", "Capability", scope="Disposable fixture"),
            record(
                "PROC-1",
                "Process",
                perspective="business",
                actors=["Operator"],
                trigger="Start",
                steps=["Run reviewed local work"],
                completion="Done",
                exceptions=[],
            ),
            record(
                "APP-1",
                "Application",
                responsibility="Disposable fixture",
                interfaces=["CLI"],
                implementation_ref={"locator": "src/result.bin"},
            ),
            record(
                "REQ-1",
                "Requirement",
                statement="Retain the reviewed bytes.",
                acceptance_criteria=["The reviewed artifact becomes exact source bytes."],
                verification_method="Independent observation remains required.",
                observe={"tests": {"workflow": WORKFLOW, "job": "observe", "cases": [CASE]}},
            ),
        ],
        "relationships": [
            {"source": "MEAS-1", "type": "evaluates", "target": "OUT-1"},
            {"source": "CAP-1", "type": "contributes_to", "target": "OUT-1"},
            {"source": "PROC-1", "type": "uses_capability", "target": "CAP-1"},
            {"source": "PROC-1", "type": "uses", "target": "APP-1"},
            {"source": "REQ-1", "type": "supports", "target": "PROC-1"},
            {"source": "APP-1", "type": "implements", "target": "REQ-1"},
        ],
    }


def git(repository: Path, *arguments: str, data: bytes | None = None) -> bytes:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Smoke operator",
        "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
        "GIT_COMMITTER_NAME": "Smoke operator",
        "GIT_COMMITTER_EMAIL": "smoke@example.invalid",
        "GIT_AUTHOR_DATE": "1793367000 +0000",
        "GIT_COMMITTER_DATE": "1793367000 +0000",
    }
    return subprocess.run(
        ["/usr/bin/git", "--git-dir", str(repository), "-c", "core.hooksPath=/dev/null", *arguments],
        input=data,
        capture_output=True,
        check=True,
        env=environment,
        timeout=15,
    ).stdout


def seed_repository(repository: Path) -> str:
    blob = git(repository, "hash-object", "-w", "--stdin", data=UNRELATED).decode().strip()
    tree = git(repository, "mktree", data=f"100644 blob {blob}\tREADME.md\n".encode()).decode().strip()
    base = git(repository, "commit-tree", tree, data=b"Reviewed smoke input\n").decode().strip()
    git(repository, "update-ref", "refs/heads/base", base)
    return base


class Issues:
    """No HTTP: the trusted adapter boundary supplies controlled repository and task responses."""

    def __init__(self) -> None:
        self.items: list[Doc] = []
        self.creates = 0

    def repository(self, repository: Doc) -> Doc:
        return copy.deepcopy(repository)

    def discover(self, repository: Doc, marker: str, effect: Doc) -> list[Doc]:
        return copy.deepcopy([item for item in self.items if marker in item["body"]])

    def create(self, repository: Doc, effect: Doc) -> Doc:
        self.creates += 1
        item = {
            "repository": copy.deepcopy(repository),
            "id": 1001,
            "number": 1,
            "url": f"https://github.com/{repository['full_name']}/issues/1",
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            **copy.deepcopy(effect),
        }
        self.items.append(item)
        return copy.deepcopy(item)


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_bytes(repository: Path) -> Doc:
    return {str(path.relative_to(repository)): digest_file(path) for path in repository.rglob("*") if path.is_file()}


def write_json(path: Path, document: Doc) -> None:
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)


def cli(arguments: list[str], *, cwd: Path, expected: int = 0) -> Doc:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "GH_TOKEN", "GITHUB_TOKEN"}
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", CONSOLE, *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    require(
        result.returncode == expected,
        f"installed command exited {result.returncode}, expected {expected}: {result.stdout} {result.stderr}",
    )
    document = json.loads(result.stdout)
    require(isinstance(document, dict), "installed command returned no object")
    return dict(document)


def prepare_fixture(root: Path, runtime: Any) -> Doc:
    from exacly import (
        admission,
        dispatch,
        dispatch_store,
        executor,
        executor_store,
        handoff_git,
        handoff_store,
        planning,
    )
    from exacly.applicability import Target
    from exacly.refresh import Run, refresh
    from exacly.state_writer import serialize

    instant = dt.datetime.now(dt.UTC)
    date = instant.date()
    private = root / "operator"
    private.mkdir(mode=0o700)
    repository = private / "source.git"
    repository_identity = handoff_git.initialize_repository(repository)
    base = seed_repository(repository)
    documents = {MODEL_FILE: model(date)}
    target = Target({"context": "main", "model_revision": MODEL_REVISION, "revision": base})
    readiness = {
        "name": "smoke-verification",
        "version": "1",
        "requirements": [{"record": "REQ-1", "dimensions": ["verification"]}],
    }
    trust = {
        "name": "controlled-smoke-producer",
        "version": "1",
        "repository": REPOSITORY,
        "branch": "main",
        "workflow": {
            "path": WORKFLOW,
            "job_id": "observe",
            "job_name": "Observe",
            "digest": planning.digest({"fixture": "workflow"}),
            "steps": {"verification": "Verify", "package": "Package", "upload": "Retain"},
        },
        "artifact": {"name_prefix": "smoke-evidence"},
        "observer": {"paths": ["tests"], "digest": planning.digest({"fixture": "observer"})},
        "model": {"revision": MODEL_REVISION, "files": [MODEL_FILE], "digest": admission.config_digest(documents)},
    }
    plan = planning.create(documents, readiness, target, date)
    require(plan["status"] == "work_proposed" and len(plan["actions"]) == 1, "fixture did not propose one task")
    state = refresh(
        documents,
        {},
        Run(
            revision=base,
            model_revision=MODEL_REVISION,
            context="main",
            observed_at=instant,
            workflow=WORKFLOW,
            job="observe",
            job_status="success",
            url="https://example.invalid/controlled-smoke",
        ),
    )
    state["x-automation-smoke"] = {"preserve": ["unrelated observations", {"reviewed": True}]}
    state_path = private / "state.json"
    state_path.write_bytes(serialize(state))
    state_path.chmod(0o600)
    dispatch_journal, ledger = private / "dispatch.sqlite3", private / "bindings.sqlite3"
    initialized = dispatch_store.initialize(dispatch_journal)
    dispatch_policy = {
        "schema_version": "0.1.0",
        "name": "controlled-smoke-dispatch",
        "version": "1",
        "adapter": "github-issue-v1",
        "repository": REPOSITORY,
        "issue": {"title": "Verify smoke output", "body": "Controlled adapter; no live issue."},
        "max_attempts": 2,
        "storage": {
            "journal": str(dispatch_journal),
            "ledger": str(ledger),
            "state": str(state_path),
            "journal_id": initialized["journal_id"],
        },
    }
    request = dispatch.Request(plan, plan["actions"][0]["id"], readiness, trust, dispatch_policy, target, date)
    client = Issues()
    before = (state_path.read_bytes(), dispatch_journal.read_bytes())
    proposal = dispatch.preview(documents, state_path, request)
    require(proposal["status"] == "ready", f"dispatch preview blocked: {proposal}")
    require(
        before == (state_path.read_bytes(), dispatch_journal.read_bytes()) and not ledger.exists(),
        "dispatch preview changed protected storage",
    )
    result = dispatch.apply(
        dispatch_journal,
        ledger,
        documents,
        state_path,
        request,
        authorization=proposal["authorization_template"],
        client=client,
    )
    require(result["status"] == "registered" and client.creates == 1, f"dispatch failed: {result}")
    retained = (state_path.read_bytes(), ledger.read_bytes(), dispatch_journal.read_bytes())
    repeated = dispatch.apply(
        dispatch_journal,
        ledger,
        documents,
        state_path,
        request,
        authorization=proposal["authorization_template"],
        client=client,
    )
    require(repeated["receipt"] == result["receipt"] and client.creates == 1, "dispatch replay created another task")
    require(
        retained == (state_path.read_bytes(), ledger.read_bytes(), dispatch_journal.read_bytes()),
        "dispatch replay changed durable storage",
    )
    executor_journal = private / "executor.sqlite3"
    executor_identity = executor_store.initialize(executor_journal, ledger=ledger, max_concurrency=1)
    output_root = root / "candidate-outputs"
    output_root.mkdir(mode=0o700)
    executor_policy = {
        "schema_version": "0.1.0",
        "name": "reviewed-smoke-executor",
        "version": "1",
        "adapter": "local-bubblewrap-v1",
        "storage": {
            "journal": str(executor_journal),
            "journal_id": executor_identity["journal_id"],
            "ledger": str(ledger),
            "state": str(state_path),
            "output_root": str(output_root),
        },
        "runtime": {
            "schema_version": "0.1.0",
            "backend": "linux-bubblewrap-v1",
            "rootfs": str(runtime.rootfs),
            "rootfs_digest": runtime.rootfs_digest,
            "python": runtime.python,
            "bubblewrap": str(runtime.bubblewrap),
        },
        "argv": [runtime.python, "-I", "-S", "/inputs/candidate.py"],
        "expected_outputs": ["result.bin"],
        "max_attempts": 2,
        "timeout_seconds": 20,
        "permitted_repository_effects": [],
    }
    script = "from pathlib import Path\nPath('/outputs/result.bin').write_bytes(" + repr(PAYLOAD) + ")\n"
    execution = executor.Request(
        plan,
        plan["actions"][0]["id"],
        readiness,
        trust,
        executor_policy,
        target,
        date,
        result["receipt"]["binding_id"],
        {"candidate.py": script.encode()},
    )
    handoff_journal = private / "handoff.sqlite3"
    handoff_identity = handoff_store.initialize(handoff_journal, repository=repository_identity)
    handoff_policy = {
        "schema_version": "0.1.0",
        "name": "reviewed-smoke-handoff",
        "version": "1",
        "adapter": "local-git-v1",
        "storage": {
            "journal": str(handoff_journal),
            "journal_id": handoff_identity["journal_id"],
            "executor_journal": str(executor_journal),
            "executor_journal_id": executor_identity["journal_id"],
            "repository": str(repository),
            "repository_id": repository_identity["instance_id"],
        },
        "allowed_paths": ["src/"],
    }
    return {
        "documents": documents,
        "state": state_path,
        "ledger": ledger,
        "dispatch_journal": dispatch_journal,
        "executor_journal": executor_journal,
        "handoff_journal": handoff_journal,
        "repository": repository,
        "base": base,
        "execution": execution,
        "handoff_policy": handoff_policy,
        "issue_creates": client.creates,
        "authority": {path: digest_file(path) for path in (state_path, ledger, dispatch_journal)},
    }


def unchanged(fixture: Doc) -> None:
    require(
        all(digest_file(path) == identity for path, identity in fixture["authority"].items()),
        "execution or handoff changed state, binding or dispatch history",
    )


def exercise(root: Path, runtime: Any) -> Doc:
    from exacly import executor, handoff, handoff_git

    fixture = prepare_fixture(root, runtime)
    request = fixture["execution"]
    before = digest_file(fixture["executor_journal"])
    proposal = executor.preview(fixture["documents"], fixture["state"], request)
    require(proposal["status"] == "ready", f"executor preview blocked: {proposal}")
    require(digest_file(fixture["executor_journal"]) == before, "executor preview wrote history")
    unchanged(fixture)
    executed = executor.apply(
        fixture["executor_journal"],
        fixture["ledger"],
        fixture["documents"],
        fixture["state"],
        request,
        authorization=proposal["authorization_template"],
    )
    require(executed["status"] == "succeeded", f"real isolated execution failed: {executed}")
    require(executed["receipt"]["build"]["kind"] == "build", "executor did not identify its output bytes")
    unchanged(fixture)
    source_digest = digest_file(fixture["executor_journal"])
    repeated = executor.apply(
        fixture["executor_journal"],
        fixture["ledger"],
        fixture["documents"],
        fixture["state"],
        request,
        authorization=proposal["authorization_template"],
    )
    require(
        repeated["receipt"] == executed["receipt"] and digest_file(fixture["executor_journal"]) == source_digest,
        "executor replay changed retained history",
    )
    unchanged(fixture)
    repository = fixture["repository"]
    journal = fixture["handoff_journal"]
    documents: list[Doc] = []
    proposals: list[Doc] = []
    for name in ("published", "recoverable"):
        document = {
            "schema_version": "0.1.0",
            "policy": fixture["handoff_policy"],
            "execution_id": executed["execution_id"],
            "attempt": 1,
            "base_revision": fixture["base"],
            "ref": f"refs/heads/smoke/nested/{name}",
            "expected_revision": None,
            "mappings": {"artifacts/result.bin": "src/result.bin"},
            "message": f"Reviewed installed smoke {name}",
            "author": {"name": "Smoke operator", "email": "smoke@example.invalid", "time": 1793367000},
        }
        request_path = root / f"{name}-request.json"
        write_json(request_path, document)
        before_handoff = (digest_file(journal), repository_bytes(repository))
        preview = cli(["handoff", "local-git", "--request", str(request_path), "--json"], cwd=root)
        require(preview["status"] == "ready" and preview["dry_run"] is True, f"handoff preview blocked: {preview}")
        require(before_handoff == (digest_file(journal), repository_bytes(repository)), "handoff preview wrote storage")
        require(
            preview["authorization_template"]["source_receipt"] == executed["receipt"]["digest"],
            "handoff proposal does not identify the retained executor receipt",
        )
        write_json(root / f"{name}-authorization.json", preview["authorization_template"])
        documents.append(document)
        proposals.append(preview)
    published = cli(
        [
            "handoff",
            "local-git",
            "--request",
            str(root / "published-request.json"),
            "--apply",
            "--authorization",
            str(root / "published-authorization.json"),
            "--json",
        ],
        cwd=root,
    )
    require(published["status"] == "published", f"handoff publication failed: {published}")
    receipt = published["receipt"]
    require(receipt["commit"] == proposals[0]["authorization_template"]["commit"], "published an unapproved commit")
    require(git(repository, "show", receipt["commit"] + ":src/result.bin") == PAYLOAD, "binary output changed")
    require(git(repository, "show", receipt["commit"] + ":README.md") == UNRELATED, "unrelated source changed")
    require(
        receipt["target"] == {"context": "main", "model_revision": MODEL_REVISION, "revision": receipt["commit"]},
        "handoff changed the approved model or implementation target",
    )
    require(
        receipt["claims"]
        == {
            "artifacts_to_git_verified": True,
            "evidence_admitted": False,
            "completion_authorized": False,
            "remote_pushed": False,
            "binding_changed": False,
        },
        "handoff granted additional authority",
    )
    unchanged(fixture)
    require(digest_file(fixture["executor_journal"]) == source_digest, "handoff changed the source executor journal")

    def interrupt(*arguments: Any, **options: Any) -> Any:
        raise RuntimeError("smoke interruption after local ref publication")

    recover_request = handoff.request_from_document(documents[1])
    recover_authorization = proposals[1]["authorization_template"]
    with patch.object(handoff, "_finalize", interrupt):
        try:
            handoff.apply(recover_request, authorization=recover_authorization)
        except RuntimeError as error:
            require(
                str(error) == "smoke interruption after local ref publication", f"unexpected handoff failure: {error}"
            )
        else:
            raise RuntimeError("handoff did not reach the selected interruption boundary")
    require(
        handoff_git.read_ref(repository, recover_request.ref) == recover_authorization["commit"],
        "interrupted handoff did not retain its exact published ref",
    )
    identity = recover_authorization["handoff_id"]
    inspected = cli(["handoffs", "show", "--journal", str(journal), "--handoff", identity, "--json"], cwd=root)
    require(inspected["status"] == "prepared", "interrupted handoff is not inspectable as prepared")
    unchanged(fixture)
    shutil.rmtree(Path(executed["receipt"]["output"]))
    fixture["executor_journal"].unlink()
    fixture["documents"].clear()
    retained = (digest_file(journal), repository_bytes(repository))
    replay = cli(
        [
            "handoff",
            "local-git",
            "--request",
            str(root / "published-request.json"),
            "--apply",
            "--authorization",
            str(root / "published-authorization.json"),
            "--json",
        ],
        cwd=root,
    )
    require(
        replay["receipt"] == receipt and retained == (digest_file(journal), repository_bytes(repository)),
        "exact handoff replay changed history or required retired source inputs",
    )
    recovered = cli(["handoffs", "recover", "--journal", str(journal), "--handoff", identity, "--json"], cwd=root)
    require(
        recovered["status"] == "published" and recovered["receipt"]["commit"] == recover_authorization["commit"],
        f"model-independent recovery failed: {recovered}",
    )
    require(
        git(repository, "show", recover_authorization["commit"] + ":src/result.bin") == PAYLOAD,
        "recovery did not retain exact binary bytes",
    )
    require(
        git(repository, "show", recover_authorization["commit"] + ":README.md") == UNRELATED,
        "recovery changed unrelated source",
    )
    unchanged(fixture)
    return {
        "controlled_issue_creates": fixture["issue_creates"],
        "dispatch_replay": "same_receipt",
        "real_isolated_execution": "succeeded",
        "executor_replay": "same_receipt",
        "handoff_publication": "published",
        "binary_and_unrelated_bytes": "preserved",
        "handoff_replay_without_source": "same_receipt",
        "after_ref_interruption": "prepared",
        "recovery_without_model_source_or_artifacts": "published",
        "authority_bytes": "preserved",
        "live_remote_effects": 0,
        "evidence_admitted": False,
        "completion_authorized": False,
    }


def deny_network(*arguments: Any, **options: Any) -> Any:
    raise AssertionError("automation smoke forbids network")


def run(*, require_installed: bool = True) -> Doc:
    from exacly import isolation

    installed = installation(require_installed=require_installed)
    bubblewrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if bubblewrap is None:
        raise RuntimeError("automation smoke requires installed Bubblewrap")
    with tempfile.TemporaryDirectory(prefix="exacly-automation-wheel-") as directory:
        root = Path(directory).resolve()
        root.chmod(0o700)
        detached = runtime_root(root / "rootfs")
        runtime = isolation.Runtime(detached, isolation.rootfs_digest(detached), "/usr/bin/python3", Path(bubblewrap))
        with (
            patch.object(socket, "create_connection", deny_network),
            patch.object(socket.socket, "connect", deny_network),
        ):
            try:
                socket.create_connection(("127.0.0.1", 9), timeout=0.01)
            except AssertionError as error:
                require(str(error) == "automation smoke forbids network", "unexpected network positive-control failure")
            else:
                raise RuntimeError("automation smoke network guard is inactive")
            result = exercise(root, runtime)
    return {
        "installation": installed,
        "scope": "installed_local_automation_with_controlled_issue_adapter",
        "network_guard": "verified",
        **result,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
