"""Real local execution and Git publication, followed by controlled live-source admission.

The Linux boundary and local Git objects are real. The GitHub metadata/report transport is an
independent controlled source; this pilot creates no remote task, push, workflow or approval.
"""

from __future__ import annotations

import base64
import copy
import os
import subprocess
from pathlib import Path
from typing import Any

from dispatch_fixtures import DATE, Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import admission, completion, executions, isolation, state_writer
from exacly.applicability import Target
from handoff_fixtures import make_handoff_scenario
from isolation_fixtures import isolation_runtime as isolation_runtime
from test_admission import ATTEMPT, BASE, RUN_ID, Source
from test_applicability import A
from test_completion import NoNetwork

Doc = dict[str, Any]
OTHER_TASK = "issues/unrelated-handoff-pilot"


def observed_git_revision(source: Source, repository: Path, revision: str, tree: str) -> None:
    """Supply controlled producer metadata for the actual locally published implementation."""
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", "--git-dir", str(repository), *arguments],
            env=environment,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout

    assert git("rev-parse", revision + "^{tree}").decode().strip() == tree
    entries: list[Doc] = []
    for item in git("ls-tree", "-r", "-l", "-z", revision).split(b"\0"):
        if not item:
            continue
        header, name = item.split(b"\t", 1)
        mode, kind, identity, size = header.split()
        assert kind == b"blob", "the pilot base contains only ordinary source files"
        oid = identity.decode()
        data = git("cat-file", "blob", oid)
        assert len(data) == int(size)
        source.pages[f"{BASE}/git/blobs/{oid}"] = {
            "sha": oid,
            "encoding": "base64",
            "content": base64.b64encode(data).decode(),
            "size": len(data),
        }
        entries.append({"path": name.decode(), "mode": mode.decode(), "type": "blob", "sha": oid, "size": len(data)})

    # The implementation preserves the independently approved producer and acceptance files.
    assert admission.observer_digest(entries, source.observer_paths) == source.policy["observer"]["digest"]
    source.pages[f"{BASE}/git/commits/{revision}"] = {"sha": revision, "tree": {"sha": tree}}
    source.pages[f"{BASE}/git/trees/{tree}"] = {"sha": tree, "truncated": False, "tree": entries}
    source.pages[f"{BASE}/branches/{source.branch}"]["commit"] = {"sha": revision}
    source.run["head_sha"] = source.job["head_sha"] = revision
    source.artifact["workflow_run"]["head_sha"] = revision
    source.manifest["implementation_revision"] = revision
    source.rebundle()


def test_one_reviewed_task_reaches_completion_only_after_evidence_for_its_actual_handoff_revision(
    dispatch_scenario: Scenario,
    isolation_runtime: isolation.Runtime,
    tmp_path: Path,
) -> None:
    initial = state_writer.read(dispatch_scenario.state_path)
    state = initial.document
    assert state is not None
    state["serial"] += 1
    state["pending"] = [
        {"record": "REQ-1", "dimension": "delivery", "task": OTHER_TASK, "opened_at": "2026-10-26T08:00:00Z"}
    ]
    state["x-handoff-pilot"] = {"preserve": ["unrelated observations", {"reviewed": True}]}
    state_writer.publish(initial, state)

    case = make_handoff_scenario(dispatch_scenario, isolation_runtime, tmp_path, real=True)
    item = case.item
    authority_before = case.authoritative_bytes()
    approved = copy.deepcopy((item.dispatch.plan, item.request.readiness_policy, item.request.trust_policy))
    proposal = case.preview()
    assert proposal["status"] == "ready", proposal
    published = case.apply(proposal["authorization_template"])
    assert published["status"] == "published", published
    receipt = published["receipt"]
    assert receipt["source_receipt"] == case.execution["receipt"]["digest"]
    assert receipt["build"] == case.execution["receipt"]["build"]
    target = Target(receipt["target"])
    assert target.data == {"context": "main", "model_revision": A, "revision": receipt["commit"]}
    assert receipt["commit"] != case.request.base_revision
    assert case.authoritative_bytes() == authority_before
    assert case.apply(proposal["authorization_template"])["receipt"] == receipt
    assert case.authoritative_bytes() == authority_before

    original_binding = executions.get(item.ledger, item.request.binding_id)["binding"]
    pending = original_binding["contract"]["pending"]
    replacement = executions.register(
        item.ledger,
        executions.Registration(
            plan=item.dispatch.plan,
            action_id=item.request.action_id,
            state=item.dispatch.state,
            task=pending["task"],
            opened_at=pending["opened_at"],
            readiness_policy=item.request.readiness_policy,
            trust_policy=item.request.trust_policy,
            target=target,
            date=DATE,
            supersedes=item.request.binding_id,
        ),
    )
    assert replacement["supersedes"] == original_binding["id"]
    assert executions.get(item.ledger, original_binding["id"])["active"] is False
    assert item.state_path.read_bytes() == authority_before[0]

    arguments: Doc = {
        "source": item.dispatch.source,
        "plan_doc": item.dispatch.plan,
        "action_id": item.request.action_id,
        "readiness_policy": item.request.readiness_policy,
        "trust_policy": item.request.trust_policy,
        "output_target": target,
        "run_id": RUN_ID,
        "attempt": ATTEMPT,
        "date": DATE,
        "ledger": item.ledger,
        "binding_id": replacement["id"],
        "state_path": item.state_path,
    }
    # An executor success, local commit and replacement association do not supply CI evidence.
    before_rejected = item.authoritative_bytes()
    rejected = completion.complete(**arguments)
    assert rejected["status"] == "pending", rejected
    assert rejected["admission"]["status"] == "rejected"
    assert item.authoritative_bytes() == before_rejected

    source = item.dispatch.source
    observed_git_revision(source, case.repository, receipt["commit"], receipt["tree"])
    calls_before = len(source.calls)
    before_completion = item.dispatch.state
    completed = completion.complete(**arguments)
    assert completed["status"] == "completed", completed
    assert completed["admission"]["status"] == "admitted"
    assert len(source.calls) > calls_before
    assert completed["receipt"]["target"] == target.data
    after = item.dispatch.state
    assert after["pending"] == [entry for entry in state["pending"] if entry["task"] == OTHER_TASK]
    assert after["x-handoff-pilot"] == state["x-handoff-pilot"]
    assert after["records"]["REQ-1"]["assessments"][0]["subject"] == {
        "model_revision": A,
        "revision": receipt["commit"],
    }
    assert after["serial"] == before_completion["serial"] + 1
    assert (item.dispatch.plan, item.request.readiness_policy, item.request.trust_policy) == approved
    assert item.dispatch.client.calls.count("create") == 1
    assert len(case.execution["history"]["attempts"]) == 1

    completed_bytes = item.authoritative_bytes()
    replay = completion.complete(**{**arguments, "source": NoNetwork()})
    assert replay["receipt"] == completed["receipt"]
    assert item.authoritative_bytes() == completed_bytes
