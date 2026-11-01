"""Pin one approved push producer and poll its exact first attempt without granting admission.

The caller supplies an independently approved contract. Digests identify that content; they do not
approve it. These read-only reports never trigger/rerun CI or establish authenticated observations.
Callers retain a selected identity before using it, and completion still obtains fresh native admission.
"""

from __future__ import annotations

import copy
import math
import re
import time
from typing import Any
from urllib.parse import quote

from exacly import admission, planning
from exacly.github_evidence import GitHubError

Doc = dict[str, Any]
VERSION = "0.1.0"
MAX_PAGES = 10
CLAIMS = {"evidence_admitted": False, "completion_authorized": False, "rerun_authorized": False}


def _require(condition: Any, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def validate_contract(value: Any) -> Doc:
    """Validate a trusted-selected native policy plus immutable workflow identity, without approving it."""
    _require(
        isinstance(value, dict)
        and set(value) == {"schema_version", "workflow_id", "policy", "policy_digest"}
        and value["schema_version"] == VERSION
        and admission.positive_id(value["workflow_id"]),
        "invalid closed producer selection contract",
    )
    chosen = admission.validate_policy(value["policy"])
    _require(value["policy_digest"] == planning.digest(chosen), "producer policy digest changed")
    return copy.deepcopy(value)


class _DeadlineSource:
    """Stop starting or accepting reads after the overall monotonic deadline.

    Native admission.Source has no per-call timeout argument. Its concrete implementation must also
    bound each in-flight read (GitHubClient uses 30 seconds); expiry rejects that read's result.
    """

    def __init__(self, source: admission.Source, timeout: float) -> None:
        _require(
            type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 3600,
            "producer timeout must be finite, positive and at most 3600 seconds",
        )
        self.source = source
        self.expires = time.monotonic() + timeout

    def remaining(self) -> float:
        return max(0.0, self.expires - time.monotonic())

    def get_json(self, path: str) -> Doc:
        if self.remaining() <= 0:
            raise TimeoutError("producer observation deadline expired")
        result = self.source.get_json(path)
        if self.remaining() <= 0:
            raise TimeoutError("producer observation deadline expired")
        return result

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise ValueError("producer selection cannot download or admit evidence")


def _producer(source: _DeadlineSource, contract: Doc, revision: str) -> str:
    chosen = contract["policy"]
    base = "/repos/" + str(chosen["repository"]["full_name"])
    info = source.get_json(base)
    _require(
        info.get("id") == chosen["repository"]["id"]
        and admission.positive_id(info.get("id"))
        and info.get("full_name") == chosen["repository"]["full_name"],
        "producer repository identity changed",
    )
    workflow = source.get_json(f"{base}/actions/workflows/{contract['workflow_id']}")
    _require(
        workflow.get("id") == contract["workflow_id"]
        and workflow.get("path") == chosen["workflow"]["path"]
        and workflow.get("state") == "active",
        "producer workflow identity or availability changed",
    )
    entries = admission.tree(source, base, revision)
    content = admission.blob(source, base, entries, chosen["workflow"]["path"])
    _require(admission.sha256(content) == chosen["workflow"]["digest"], "producer workflow definition changed")
    admission.workflow_contract(content, chosen)
    _require(
        admission.observer_digest(entries, chosen["observer"]["paths"]) == chosen["observer"]["digest"],
        "producer verifier closure changed",
    )
    return base


def _revision(value: Any) -> str:
    _require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "producer revision must be a full source SHA"
    )
    return str(value)


def _run(value: Any, contract: Doc, revision: str, run_id: int, attempt: int) -> Doc:
    chosen = contract["policy"]
    repository = chosen["repository"]
    _require(isinstance(value, dict), "invalid producer run metadata")
    _require(
        value.get("id") == run_id
        and admission.positive_id(value.get("id"))
        and value.get("run_attempt") == attempt
        and admission.positive_id(value.get("run_attempt")),
        "producer run or attempt changed",
    )
    _require(
        value.get("event") == "push"
        and value.get("head_branch") == chosen["branch"]
        and value.get("head_sha") == revision
        and value.get("workflow_id") == contract["workflow_id"]
        and admission.positive_id(value.get("workflow_id")),
        "producer run does not match the exact contract",
    )
    path = chosen["workflow"]["path"]
    _require(
        value.get("path") in (path, path + "@" + chosen["branch"], path + "@refs/heads/" + chosen["branch"]),
        "producer run workflow path changed",
    )
    for key in ("repository", "head_repository"):
        origin = value.get(key)
        _require(
            isinstance(origin, dict)
            and admission.positive_id(origin.get("id"))
            and origin.get("id") == repository["id"]
            and origin.get("full_name") == repository["full_name"],
            "producer run is from another repository",
        )
    return copy.deepcopy(value)


def _selection(contract: Doc, revision: str, run_id: int, attempt: int) -> Doc:
    value = {
        "schema_version": VERSION,
        "contract_digest": planning.digest(contract),
        "policy_digest": contract["policy_digest"],
        "repository": copy.deepcopy(contract["policy"]["repository"]),
        "workflow_id": contract["workflow_id"],
        "workflow_path": contract["policy"]["workflow"]["path"],
        "revision": revision,
        "event": "push",
        "branch": contract["policy"]["branch"],
        "run_id": run_id,
        "attempt": attempt,
    }
    value["digest"] = planning.digest(value)
    return value


def _retained(contract: Doc, value: Any) -> Doc:
    _require(isinstance(value, dict), "an exact retained producer selection is required")
    _require(
        admission.positive_id(value.get("run_id")) and value.get("attempt") == 1 and type(value.get("attempt")) is int,
        "rerun selection requires a separate reviewed contract",
    )
    expected = _selection(contract, _revision(value.get("revision")), value["run_id"], value["attempt"])
    _require(value == expected, "retained producer selection or approved policy changed")
    return copy.deepcopy(value)


def _report(status: str, reason: str, selection: Doc | None = None, **values: Any) -> Doc:
    value = {
        "schema_version": VERSION,
        "status": status,
        "reason": reason,
        "selection": copy.deepcopy(selection),
        "claims": copy.deepcopy(CLAIMS),
        **values,
    }
    value["digest"] = planning.digest(value)
    return value


def _discover(source: _DeadlineSource, base: str, contract: Doc, revision: str) -> list[Doc]:
    chosen = contract["policy"]
    prefix = (
        f"{base}/actions/workflows/{contract['workflow_id']}/runs?head_sha={revision}"
        f"&event=push&branch={quote(chosen['branch'], safe='')}&per_page=100"
    )
    result: list[Doc] = []
    count: int | None = None
    for page in range(1, MAX_PAGES + 1):
        found = source.get_json(prefix + f"&page={page}")
        _require(
            type(found.get("total_count")) is int
            and found["total_count"] >= 0
            and isinstance(found.get("workflow_runs"), list),
            "invalid producer run discovery",
        )
        _require(count is None or count == found["total_count"], "producer discovery changed during pagination")
        count = found["total_count"]
        _require(all(isinstance(item, dict) for item in found["workflow_runs"]), "invalid producer discovery entry")
        result.extend(found["workflow_runs"])
        if len(result) == count:
            return result
        _require(bool(found["workflow_runs"]) and len(result) < count, "incomplete producer discovery")
    raise ValueError("producer discovery exceeds its bounded page limit")


def select(
    source: admission.Source,
    contract: Doc,
    revision: str,
    *,
    run_id: int | None = None,
    attempt: int | None = None,
    timeout: float = 30,
) -> Doc:
    """Select a unique exact run without choosing by success or recency; retain selection before use."""
    approved = validate_contract(contract)
    revision = _revision(revision)
    _require(
        attempt is None or (type(attempt) is int and attempt == 1),
        "rerun selection requires a separate reviewed contract",
    )
    _require(run_id is None or admission.positive_id(run_id), "producer run ID must be positive")
    bounded = _DeadlineSource(source, timeout)
    try:
        base = _producer(bounded, approved, revision)
        if run_id is None:
            matches = _discover(bounded, base, approved, revision)
            if not matches:
                return _report("pending", "No exact approved push producer is available yet.")
            if len(matches) != 1:
                return _report("ambiguous", "Multiple producer runs match; select an independently reviewed exact run.")
            run_id = matches[0].get("id")
            _require(admission.positive_id(run_id), "discovered producer has an invalid run ID")
            assert run_id is not None
            _run(matches[0], approved, revision, run_id, 1)
        current = _run(bounded.get_json(f"{base}/actions/runs/{run_id}"), approved, revision, run_id, 1)
        exact = _run(bounded.get_json(f"{base}/actions/runs/{run_id}/attempts/1"), approved, revision, run_id, 1)
        _require(current.get("id") == exact["id"], "producer run changed during selection")
        return _report(
            "selected",
            "Exact first-attempt producer pinned; retain this selection before polling or admission.",
            _selection(approved, revision, run_id, 1),
        )
    except TimeoutError:
        return _report("timeout", "Producer selection deadline expired; no run was selected.")
    except (admission.Rejected, GitHubError, OSError, ValueError, KeyError, TypeError):
        return _report(
            "blocked", "Producer identity, approved definition or source availability could not be established."
        )


def _poll(source: _DeadlineSource, contract: Doc, selected: Doc) -> Doc:
    base = _producer(source, contract, selected["revision"])
    run_id, attempt = selected["run_id"], selected["attempt"]
    _run(source.get_json(f"{base}/actions/runs/{run_id}"), contract, selected["revision"], run_id, attempt)
    run = _run(
        source.get_json(f"{base}/actions/runs/{run_id}/attempts/{attempt}"),
        contract,
        selected["revision"],
        run_id,
        attempt,
    )
    if run.get("status") != "completed":
        _require(
            run.get("status") in ("queued", "in_progress", "waiting", "requested", "pending"),
            "unknown producer run status",
        )
        return _report("waiting", "The exact pinned producer is not terminal.", selected, run=run)
    jobs = admission.pages(source, f"{base}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    _require(len(jobs) == 1, "ambiguous or missing single producer job")
    job = jobs[0]
    _require(
        job.get("name") == contract["policy"]["workflow"]["job_name"]
        and admission.positive_id(job.get("id"))
        and admission.positive_id(job.get("run_id"))
        and job.get("run_id") == run_id
        and job.get("head_sha") == selected["revision"]
        and ("run_attempt" not in job or (type(job["run_attempt"]) is int and job["run_attempt"] == attempt)),
        "producer job identity changed",
    )
    _require(job.get("status") == "completed", "terminal run has an incomplete producer job")
    successful = run.get("conclusion") == job.get("conclusion") == "success"
    _require(
        run.get("conclusion")
        in ("success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale")
        and job.get("conclusion") == run.get("conclusion"),
        "producer job and run outcomes conflict",
    )
    return _report(
        "succeeded" if successful else "failed",
        "Exact producer terminated; fresh native admission is still required.",
        selected,
        run=run,
        job=job,
    )


def poll(source: admission.Source, contract: Doc, selection: Doc, *, timeout: float = 30) -> Doc:
    """Observe one pinned run without rediscovery or effects; caller schedules bounded later polls."""
    approved = validate_contract(contract)
    selected = _retained(approved, selection)
    bounded = _DeadlineSource(source, timeout)
    try:
        return _poll(bounded, approved, selected)
    except TimeoutError:
        return _report("timeout", "Pinned producer observation deadline expired.", selected)
    except (admission.Rejected, GitHubError, OSError, ValueError, KeyError, TypeError):
        return _report("blocked", "Pinned producer, verifier, policy or source availability changed.", selected)


def wait(
    source: admission.Source, contract: Doc, selection: Doc, *, timeout: float = 60, poll_interval: float = 1
) -> Doc:
    """Poll only this retained first attempt under one monotonic deadline; never trigger or rerun CI."""
    approved = validate_contract(contract)
    selected = _retained(approved, selection)
    _require(
        type(poll_interval) in (int, float) and math.isfinite(poll_interval) and 0 < poll_interval <= 60,
        "producer poll interval must be finite, positive and at most 60 seconds",
    )
    bounded = _DeadlineSource(source, timeout)
    while True:
        try:
            result = _poll(bounded, approved, selected)
        except TimeoutError:
            return _report("timeout", "Pinned producer wait deadline expired.", selected)
        except (admission.Rejected, GitHubError, OSError, ValueError, KeyError, TypeError):
            return _report("blocked", "Pinned producer, verifier, policy or source availability changed.", selected)
        if result["status"] != "waiting":
            return result
        time.sleep(min(float(poll_interval), bounded.remaining()))
