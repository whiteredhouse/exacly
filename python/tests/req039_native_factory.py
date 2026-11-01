"""Production-shaped fixed-consumer journals under explicit controlled physical/API seams.

The real bootstrap, PreparedWorker, controller, Authorizer, grants, journals, Git, admission and
completion run unchanged. Kernel facts, worker execution and the remote GitHub receive operation
are controlled fixtures. This factory establishes no physical containment or live GitHub claim.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import multiprocessing
import os
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import test_admission as admission_fixture
import test_handoff_end_to_end as handoff_fixture
import yaml
from dispatch_fixtures import FakeIssues
from exacly import (
    admission,
    automation_authorization,
    automation_controller,
    executor_runtime,
    git_remote_transport,
    live_pilot_evidence,
    planning,
    producer_selection,
    resource_guard,
    worker_integration,
)
from exacly.execution_store import Deadline
from exacly.github_evidence import GitHubError
from handoff_fixtures import controlled_runtime, object_bytes

from ci import req039_bootstrap as setup
from ci import req039_pilot as pilot

Doc = dict[str, Any]
ROOT = Path(__file__).resolve().parents[2]
BASE = "repos/" + setup.REPOSITORY


def _closure() -> dict[str, tuple[str, bytes]]:
    """Use actual executing files, preserving modes, before the approved Git snapshot exists."""
    selected: dict[str, tuple[str, bytes]] = {}
    for selector in sorted(set(setup.OBSERVER_PATHS + live_pilot_evidence.IMPLEMENTATION_PATHS)):
        path = ROOT / selector
        if not path.exists():
            continue
        for member in [path] if path.is_file() else path.rglob("*"):
            if member.is_file() and "__pycache__" not in member.parts and member.suffix != ".pyc":
                assert not member.is_symlink()
                selected[str(member.relative_to(ROOT))] = (
                    "100755" if member.stat().st_mode & 0o111 else "100644",
                    member.read_bytes(),
                )
    for name in (setup.SOURCE, *live_pilot_evidence._CONTRACT_PATHS):
        selected[name] = ("100644", (ROOT / name).read_bytes())
    return selected


def _checkout(root: Path, files: dict[str, tuple[str, bytes]]) -> tuple[Path, str, str, list[Doc]]:
    checkout = setup.private_directory(root / "checkout", create=True)
    repository = checkout / ".git"
    (repository / "objects" / "info").mkdir(mode=0o700, parents=True)
    (repository / "objects" / "pack").mkdir(mode=0o700)
    structure: dict[str, Any] = {}
    entries = []
    for name, (mode, raw) in files.items():
        identity = object_bytes(repository, "blob", raw)
        cursor = structure
        parts = name.split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = (mode, identity)
        entries.append({"path": name, "mode": mode, "type": "blob", "sha": identity, "size": len(raw)})

    def tree(items: Doc) -> str:
        encoded = []
        for name, value in items.items():
            mode, identity = ("40000", tree(value)) if isinstance(value, dict) else value
            encoded.append(
                (
                    name + ("/" if mode == "40000" else ""),
                    mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(identity),
                )
            )
        return object_bytes(repository, "tree", b"".join(raw for _, raw in sorted(encoded)))

    tree_id = tree(structure)
    revision = object_bytes(
        repository,
        "commit",
        (
            f"tree {tree_id}\nauthor Controlled fixture <fixture@example.invalid> 1 +0000\n"
            "committer Controlled fixture <fixture@example.invalid> 1 +0000\n\nExact fixed-consumer source\n"
        ).encode(),
    )
    return checkout, revision, tree_id, entries


class Source(admission_fixture.Source):
    """Controlled GitHub reads backed by actual source/Git objects and retained archives."""

    archives: dict[int, bytes]
    runs: dict[int, Doc]

    def get_json(self, relative_path: str) -> Doc:
        self.calls.append(relative_path)
        url = urlsplit(relative_path)
        path = unquote(url.path).lstrip("/")
        query = parse_qs(url.query)
        if path.endswith("/actions/workflows/99/runs"):
            found = [
                item
                for item in self.runs.values()
                if all(
                    item[field] == query[key][0]
                    for key, field in (("head_sha", "head_sha"), ("branch", "head_branch"), ("event", "event"))
                    if key in query
                )
            ]
            return {"total_count": len(found), "workflow_runs": copy.deepcopy(found)}
        if path.endswith(("/jobs", "/artifacts")):
            path += "?page=" + query.get("page", ["1"])[0]
        if path not in self.pages:
            raise GitHubError("GitHub evidence request returned HTTP 404")
        return copy.deepcopy(self.pages[path])

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        assert repository == setup.REPOSITORY
        self.downloads.append((repository, artifact_id))
        return self.archives[artifact_id]

    def retain_run(self, revision: str, tree: str, *, run_id: int, artifact_id: int, outcome: str) -> None:
        self.run, self.job, self.artifact, self.manifest = (
            copy.deepcopy(value) for value in (self.run, self.job, self.artifact, self.manifest)
        )
        self.run.update(id=run_id, head_sha=revision, conclusion=outcome)
        self.job.update(run_id=run_id, head_sha=revision, conclusion=outcome)
        for step in self.job["steps"]:
            step["conclusion"] = outcome if step["name"] == self.step_names["verification"] else "success"
        self.artifact.update(id=artifact_id, name=f"{self.artifact_prefix}-{run_id}-1")
        self.artifact["workflow_run"].update(id=run_id, head_sha=revision)
        self.manifest.update(run_id=run_id, implementation_revision=revision, check_outcome=outcome)
        file, name = setup.FIXED_CASE.split("::")
        self.report = (
            f'<testsuite><testcase file="{file}" name="{name}">'.encode()
            + (b"<failure/>" if outcome == "failure" else b"")
            + b"</testcase></testsuite>"
        )
        self.manifest["report_digest"] = admission.sha256(self.report)
        finished = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        started = finished - dt.timedelta(seconds=2)
        self.job.update(started_at=started.isoformat(), completed_at=finished.isoformat())
        for step in self.job["steps"]:
            step.update(started_at=started.isoformat(), completed_at=finished.isoformat())
        self.artifact.update(
            created_at=finished.isoformat(),
            updated_at=finished.isoformat(),
            expires_at=(finished + dt.timedelta(days=14)).isoformat(),
        )
        self.rebundle()
        self.archives[artifact_id] = self.archive
        self.pages.update(
            {
                f"{BASE}/actions/runs/{run_id}": self.run,
                f"{BASE}/actions/runs/{run_id}/attempts/1": self.run,
                f"{BASE}/actions/runs/{run_id}/attempts/1/jobs?page=1": {"total_count": 1, "jobs": [self.job]},
                f"{BASE}/actions/runs/{run_id}/artifacts?page=1": {"total_count": 1, "artifacts": [self.artifact]},
                f"{BASE}/actions/artifacts/{artifact_id}": self.artifact,
            }
        )
        self.runs[run_id] = self.run
        self.pages[f"{BASE}/actions/workflows/99/runs"] = {
            "total_count": len(self.runs),
            "workflow_runs": list(self.runs.values()),
        }
        self.tree = self.pages[f"{BASE}/git/trees/{tree}"]["tree"]


class Issues(FakeIssues):
    """A controlled external API persists its issue across the real controller process death."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def create(self, repository: Doc, effect: Doc) -> Doc:
        self.issues = json.loads(self.path.read_bytes())["issues"] if self.path.exists() else []
        result = super().create(repository, effect)
        result["created_at"] = dt.datetime.now(dt.UTC).isoformat()
        self.issues[-1] = result
        setup.write_json(self.path, {"issues": self.issues}, replace=self.path.exists())
        return result


class Remote(git_remote_transport.GitHubGitTransport):
    """Real exact remote inspection; controlled receive mutates only the fixture API."""

    def __init__(self, source: Source, packet: Doc, root: Path) -> None:
        super().__init__("https://github.com/" + setup.REPOSITORY + ".git", str(setup.REPOSITORY_ID), source)
        self.packet, self.root, self.sends = packet, root, 0

    def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
        checked = self._operation(operation, Deadline(timeout))
        before = self.inspect(checked, timeout=timeout)
        assert before["revision"] == checked["request"]["expected_revision"]
        source = self.source
        assert isinstance(source, Source)
        self.sends += 1
        authorization = checked["authorization"]
        revision, tree = authorization["commit"], authorization["tree"]
        # Preserve the actual initial run objects before the new producer changes revision.
        source.run, source.job, source.artifact, source.manifest = (
            copy.deepcopy(value) for value in (source.run, source.job, source.artifact, source.manifest)
        )
        handoff_fixture.observed_git_revision(source, repository, revision, tree)
        source.pages[f"{BASE}/git/commits/{revision}"]["parents"] = [{"sha": authorization["base_revision"]}]
        source.retain_run(revision, tree, run_id=778, artifact_id=889, outcome="success")
        marker = authorization["operation_marker"]
        source.pages[f"{BASE}/git/ref/" + checked["request"]["ref"].removeprefix("refs/")] = {
            "ref": checked["request"]["ref"],
            "object": {"type": "commit", "sha": revision},
        }
        source.pages[f"{BASE}/git/ref/" + marker["ref"].removeprefix("refs/")] = {
            "ref": marker["ref"],
            "object": {"type": "tag", "sha": marker["object"]},
        }
        source.pages[f"{BASE}/git/tags/{marker['object']}"] = {
            "sha": marker["object"],
            "tag": marker["ref"].removeprefix("refs/tags/"),
            "object": {"type": "commit", "sha": revision},
            "message": marker["content"].split("\n\n", 1)[1],
            "tagger": {
                "name": "Exacly publication",
                "email": "publication@exacly.invalid",
                "date": "1970-01-01T00:00:00Z",
            },
        }
        setup.write_json(
            self.root / "publication-response-dropped.json",
            {
                "packet_digest": self.packet["digest"],
                "operation_id": checked["id"],
                "actual_exchange_returned": True,
            },
        )
        raise TimeoutError("controlled loss of the actual fixture receive response; discover, never send again")


def build(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Doc:
    """Build and run the actual fixed profile; return authentic exports plus controlled API objects."""
    del request
    current = dt.datetime.now(dt.UTC)
    host = automation_controller.host_identity(run_id="900001", job_id="900002")
    bounds = resource_guard.PILOT_BOUNDS
    identity = {
        "schema_version": "0.1.0",
        "backend": resource_guard.BACKEND,
        "boot_id": host["boot_id"],
        "cgroup": "/controlled-req039-fixture",
        "mount_id": "1",
        "mount_device": "0:1",
        "device": 1,
        "inode": 2,
        "caps": asdict(bounds),
        "bounds": asdict(bounds),
    }
    monkeypatch.setattr(resource_guard, "_facts", lambda *args: copy.deepcopy(identity))
    monkeypatch.setattr(resource_guard.Guard, "check_process", lambda guard, pid: guard.check())
    guard = resource_guard.capture(bounds, expected_cgroup=identity["cgroup"])
    model = yaml.safe_load((ROOT / setup.MODEL).read_bytes())
    for name, value in {
        "ROOT": ROOT,
        "REPOSITORY": setup.REPOSITORY,
        "REPOSITORY_ID": setup.REPOSITORY_ID,
        "BASE": BASE,
        "WORKFLOW": setup.WORKFLOW,
        "MODEL_FILE": setup.MODEL,
        "CASE": setup.FIXED_CASE,
        "STANDARD_PRODUCER": setup.PRODUCER,
    }.items():
        monkeypatch.setattr(admission_fixture, name, copy.deepcopy(value))
    monkeypatch.setattr(admission_fixture, "config", lambda *args, **kwargs: copy.deepcopy(model))
    monkeypatch.setattr(handoff_fixture, "BASE", BASE)
    files = _closure()
    checkout, base, tree, entries = _checkout(tmp_path, files)
    api = Source("failure")
    api.archives, api.runs = {}, {}
    api.tree, api.observer_paths = entries, setup.OBSERVER_PATHS
    api.policy["observer"] = {
        "paths": setup.OBSERVER_PATHS,
        "digest": admission.observer_digest(entries, setup.OBSERVER_PATHS),
    }
    api.policy["branch_protection"] = "not_required"
    api.policy["model"]["revision"] = base
    api.pages[BASE]["clone_url"] = "https://github.com/" + setup.REPOSITORY + ".git"
    for entry in entries:
        api.pages[f"{BASE}/git/blobs/{entry['sha']}"] = {
            "sha": entry["sha"],
            "size": entry["size"],
            "encoding": "base64",
            "content": base64.b64encode(files[entry["path"]][1]).decode(),
        }
    api.pages[f"{BASE}/git/commits/{base}"] = {"sha": base, "tree": {"sha": tree}, "parents": []}
    api.pages[f"{BASE}/git/trees/{tree}"] = {"sha": tree, "truncated": False, "tree": entries}
    api.pages[f"{BASE}/actions/workflows/req039-evidence.yml"] = api.pages[f"{BASE}/actions/workflows/99"]
    api.pages[f"{BASE}/branches/{setup.BRANCH}"].update(protected=False, commit={"sha": base})
    api.pages[f"{BASE}/git/ref/heads/{setup.BRANCH}"] = {
        "ref": "refs/heads/" + setup.BRANCH,
        "object": {"type": "commit", "sha": base},
    }
    api.retain_run(base, tree, run_id=777, artifact_id=888, outcome="failure")
    baseline_source = copy.deepcopy(api)
    original_import = setup.import_base
    monkeypatch.setattr(
        setup,
        "import_base",
        lambda selected, repository, revision, **kwargs: original_import(checkout, repository, revision, **kwargs),
    )

    def runtime_root(path: Path) -> Doc:
        setup.private_directory(path, create=True)
        return {
            "schema_version": "0.1.0",
            "backend": "linux-bubblewrap-v1",
            "rootfs": str(path),
            "rootfs_digest": planning.digest("explicit controlled absent physical rootfs"),
            "python": "/usr/bin/python3",
            "bubblewrap": "/usr/bin/bwrap",
        }

    monkeypatch.setattr(setup, "runtime_root", runtime_root)
    context = {
        "run_id": host["run_id"],
        "run_attempt": "1",
        "job_key": "coordinate",
        "host": host,
        "nonce": "a" * 32,
        "workflow_id": 100,
        "run_number": 7,
        "workflow_path": live_pilot_evidence.COORDINATOR,
        "coordinator_revision": base,
        "created_at": current.isoformat(),
        "deadline_at": (current + dt.timedelta(minutes=29)).isoformat(),
    }
    publisher = {
        "environment": pilot.PUBLISH_TOKEN,
        "present": True,
        "scope_verified": True,
        "app_id": 12345,
        "installation_id": 54321,
        "expires_at": (current + dt.timedelta(hours=1)).isoformat(),
        "issuer_evidence": "trusted-workflow-mint-metadata",
        "issuer_server_verified": False,
    }
    root = setup.private_directory(tmp_path / "native-pilot", create=True)
    bundle = setup.bootstrap(root, ROOT, base, context, api, guard, baseline_run=777, publisher=publisher)
    packet = pilot.packet_for(bundle, publisher_available=True)
    setup.write_json(root / "approval.json", {"channel": "controlled-authenticated-reader"})
    prepared = worker_integration.prepare(
        Path(bundle["components"]["handoff_policy"]["storage"]["repository"]),
        repository_id=bundle["worker_identity"]["repository"]["instance_id"],
        revision=base,
        allowed_paths=[setup.SOURCE],
        protected_paths=setup.PROTECTED_PATHS,
        model_digest=bundle["trust"]["model"]["digest"],
    )
    payload = {
        "schema_version": "0.1.0",
        "recipe": worker_integration.RECIPE,
        "snapshot_digest": prepared.identity["snapshot_digest"],
        "files": [{key: item[key] for key in ("path", "size", "digest")} for item in prepared.identity["files"]],
        "verification_established": False,
        "evidence_admitted": False,
    }
    controlled = controlled_runtime((planning.canonical(payload) + "\n").encode())

    def worker(*args: Any, **kwargs: Any) -> Doc:
        for callback in ("on_worker", "on_namespace"):
            original = kwargs[callback]
            kwargs[callback] = lambda value, original=original: original({**value, "boot_id": host["boot_id"]})
        result = controlled(*args, **kwargs)
        output = args[3]
        (output / "artifacts/result.txt").rename(output / "artifacts/source-digests.json")
        result["report"]["artifacts"][0]["path"] = "artifacts/source-digests.json"
        result["report"].pop("digest")
        result["report"]["digest"] = planning.digest(result["report"])
        for proof in result["termination"].values():
            proof["identity"]["boot_id"] = host["boot_id"]
            proof["reason"] = "not_running"
        (output / "report.json").write_text(planning.canonical(result["report"]))
        return dict(result)

    monkeypatch.setattr(executor_runtime, "run", worker)
    transport = Remote(api, packet, root)
    issues = Issues(root / "controlled-external-issues.json")
    services = automation_controller.NativeServices(
        api,
        transport,
        prepared,
        guard,
        producer_selection,
        dispatch_client=issues,
        boundary=lambda operation, timeout: operation(),
    )

    class Restart(automation_controller.NativeWorkflow):
        def accept(self, phase: str, proposal: Doc, receipt: Doc, correlations: Doc) -> Doc:
            result = super().accept(phase, proposal, receipt, correlations)
            marker = root / "dispatch-controller-crash.json"
            if phase == "dispatch" and not marker.exists():
                setup.write_json(marker, {"packet_digest": packet["digest"], "phase": phase, "native_receipt": receipt})
                os.kill(os.getpid(), signal.SIGKILL)
            return result

    workflow = Restart(
        contract=bundle["contract"],
        documents=bundle["documents"],
        plan=bundle["plan"],
        readiness_policy=bundle["readiness"],
        trust_policy=bundle["trust"],
        components=bundle["components"],
        services=services,
    )
    authority = automation_authorization.Authorizer(
        Path(bundle["delegation"]["config_path"]),
        approved_digest=planning.digest(bundle["delegation"]),
        issuer=bundle["delegation"]["issuer"],
        approval_id=bundle["delegation"]["approval_id"],
        effects=workflow.effect_registry(),
        readers={
            **workflow.reader_registry(),
            "authority-preflight": pilot.AuthorityPreflight(root, bundle, packet, workflow),
            "budget-exhaustion": pilot.BudgetProbe(bundle, workflow),
            "remote-precondition": pilot.RemotePrecondition(bundle, workflow),
        },
    )
    assert authority.identity() == bundle["contract"]["authorizer"]
    monkeypatch.setattr(pilot, "load_runtime", lambda selected: (bundle, packet, workflow, authority))
    monkeypatch.setattr(pilot, "runtime_objects", lambda selected: (bundle, packet, workflow, authority))
    selected = bundle["components"]["controller"]
    journal, journal_id = Path(selected["journal"]), selected["journal_id"]
    before = automation_controller.show(journal, journal_id)
    process = multiprocessing.get_context("fork").Process(
        target=lambda: automation_controller.step(journal, journal_id, workflow=workflow, authority=authority)
    )
    process.start()
    process.join(20)
    assert process.exitcode == -signal.SIGKILL
    after_termination = automation_controller.show(journal, journal_id)
    resumed = automation_controller.step(journal, journal_id, workflow=workflow, authority=authority)
    setup.write_json(
        root / "recovery-observations.json",
        {
            "controller_restarts": [
                {
                    "returncode": process.exitcode,
                    "pid": process.pid,
                    "host": host,
                    "run_attempt": "1",
                    "before": before,
                    "after_termination": after_termination,
                    "after_restart": {"controller": resumed, "returncode": 0, "pid": os.getpid()},
                    "native_dispatch_receipt": setup.read_json(root / "dispatch-controller-crash.json")[
                        "native_receipt"
                    ],
                }
            ]
        },
    )
    pilot.baseline_probe(root, resumed)
    clock = dt.datetime.now(dt.UTC)

    def scheduled_time() -> dt.datetime:
        return clock

    for _ in range(30):
        retained = automation_controller.show(journal, journal_id)
        if retained["event"]["status"] == "completed":
            break
        assert retained["event"]["status"] != "paused", retained["event"]["reason"]
        if retained["event"]["phase"] == "associate" and not retained["event"]["pending"]:
            pilot.negative_probes(root, retained)
        automation_controller.step(journal, journal_id, workflow=workflow, authority=authority, clock=scheduled_time)
        clock += dt.timedelta(seconds=30)
    else:
        raise AssertionError("fixed native factory exceeded its finite controlled schedule")
    setup.write_json(root / "fence.json", {"controlled": "not-live-authenticated"})
    evidence = pilot.export_acceptance(root, bundle, packet, retained)
    assert transport.sends == 1
    return {
        "bundle": bundle,
        "packet": packet,
        "controller": retained,
        "source": api,
        "baseline_source": baseline_source,
        "workflow": workflow,
        "authority": authority,
        "evidence": evidence,
        "root": root,
    }
