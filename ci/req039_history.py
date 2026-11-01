"""Immutable Actions control-metadata fence for the single logical REQ-039 pilot goal.

Artifact bytes are workflow control metadata, never producer evidence. The unit's reader and
publisher credentials have Actions read permission only; the pinned workflow uploads the fence
outside candidate/unit authority. Missing, expired or incomplete prior history is never absence
of approval. Trusted repository owners deleting history are outside this cooperating boundary.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from typing import Any

from exacly import admission, planning
from exacly.execution_store import invalid_constant, unique_object

from ci import req039_bootstrap as setup

Doc = dict[str, Any]
COORDINATOR_WORKFLOW = ".github/workflows/req039-coordinator.yml"
LAUNCH_STEP = "Start bounded same-runner coordinator"
FENCE_STEP = "Retain immutable approved-goal fence"
NOAUTH_STEP = "Retain terminal no-authority receipt"
FENCE_PREFIX = "exacly-req039-approval-"
NOAUTH_PREFIX = "exacly-req039-noauthority-"
FENCE_MEMBER = "approval-receipt.json"
NOAUTH_MEMBER = "terminal-noauthority.json"
LIMIT = 128 * 1024


class FencePending(ValueError):
    """A successful workflow upload is not yet visible through the independent job API."""


def artifacts(source: admission.Source, run_id: int) -> list[Doc]:
    result: list[Doc] = []
    for page in range(1, 21):
        value = source.get_json(f"/repos/{setup.REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100&page={page}")
        setup.require(
            type(value.get("total_count")) is int
            and 0 <= value["total_count"] <= 2000
            and isinstance(value.get("artifacts"), list)
            and len(value["artifacts"]) <= 100,
            "artifact history is incomplete or exceeds its bounded scope",
        )
        result.extend(value["artifacts"])
        if len(value["artifacts"]) < 100:
            setup.require(len(result) == value["total_count"], "artifact history was only partially read")
            setup.require(
                len({item.get("id") for item in result}) == len(result),
                "artifact history contains duplicate identities",
            )
            return result
    raise ValueError("artifact history exceeds its bounded supported scope")


def read_artifact(
    source: admission.Source,
    identity: int,
    *,
    run_id: str,
    name: str,
    member: str,
    expected_digest: str | None = None,
    revision: str | None = None,
) -> tuple[Doc, Doc]:
    setup.require(type(identity) is int and identity > 0, "an immutable artifact ID is required")
    metadata = source.get_json(f"/repos/{setup.REPOSITORY}/actions/artifacts/{identity}")
    setup.require(
        metadata.get("id") == identity and metadata.get("name") == name and metadata.get("expired") is False,
        "consumed-goal artifact is absent, expired or has another identity",
    )
    run = metadata.get("workflow_run")
    setup.require(
        isinstance(run, dict)
        and run.get("id") == int(run_id)
        and run.get("repository_id") == run.get("head_repository_id") == setup.REPOSITORY_ID
        and (revision is None or run.get("head_sha") == revision),
        "consumed-goal artifact belongs to another producer/run/revision",
    )
    digest = metadata.get("digest")
    setup.require(
        isinstance(digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        and (expected_digest is None or digest == expected_digest),
        "artifact content digest is unavailable or changed",
    )
    archive = source.download_artifact(setup.REPOSITORY, identity)
    setup.require(
        isinstance(archive, bytes)
        and len(archive) <= LIMIT
        and "sha256:" + hashlib.sha256(archive).hexdigest() == digest,
        "artifact bytes differ from their independently fetched server digest",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            setup.require(
                len(entries) == 1
                and entries[0].filename == member
                and not entries[0].is_dir()
                and not entries[0].flag_bits & 1
                and 0 <= entries[0].file_size <= LIMIT,
                "unsupported consumed-goal artifact layout",
            )
            with zipped.open(entries[0]) as stream:
                content = stream.read(LIMIT + 1)
            setup.require(
                len(content) <= LIMIT and len(content) == entries[0].file_size, "artifact member exceeds its bound"
            )
            value = json.loads(content, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            setup.require(
                isinstance(value, dict) and content == (planning.canonical(value) + "\n").encode(),
                "artifact control metadata is not canonical",
            )
    except (zipfile.BadZipFile, RuntimeError, UnicodeDecodeError) as error:
        raise ValueError("consumed-goal artifact could not be independently read") from error
    return dict(value), metadata


def job(source: admission.Source, run_id: str, attempt: str) -> Doc:
    value = source.get_json(f"/repos/{setup.REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    setup.require(
        value.get("total_count") == 1 and isinstance(value.get("jobs"), list) and len(value["jobs"]) == 1,
        "the coordinator requires its fixed single-job contract",
    )
    result = value["jobs"][0]
    setup.require(
        isinstance(result, dict)
        and type(result.get("id")) is int
        and result["id"] > 0
        and result.get("run_id") == int(run_id)
        and isinstance(result.get("steps"), list),
        "coordinator job identity or step history is unavailable",
    )
    return dict(result)


def verify_fence(source: admission.Source, context: Doc, reference: Doc, expected: Doc) -> Doc:
    setup.require(set(reference) == {"artifact_id", "digest"}, "an artifact reference cannot approve local content")
    value, metadata = read_artifact(
        source,
        reference["artifact_id"],
        run_id=context["run_id"],
        name=FENCE_PREFIX + context["run_id"] + "-" + context["run_attempt"],
        member=FENCE_MEMBER,
        expected_digest=reference["digest"],
        revision=context["coordinator_revision"],
    )
    setup.require(value == expected, "uploaded fence differs from the exact independently approved packet/receipt")
    observed = job(source, context["run_id"], context["run_attempt"])
    setup.require(
        str(observed["id"]) == context["host"]["job_id"], "fence was not uploaded by the approved exact coordinator job"
    )
    steps = [item for item in observed["steps"] if item.get("name") == FENCE_STEP]
    if not steps or (len(steps) == 1 and steps[0].get("status") in {"queued", "in_progress"}):
        raise FencePending("immutable fence upload step is not yet independently visible")
    setup.require(
        len(steps) == 1 and steps[0].get("status") == "completed" and steps[0].get("conclusion") == "success",
        "immutable fence upload has no successful actual workflow step",
    )
    return {
        "artifact_id": metadata["id"],
        "digest": metadata["digest"],
        "name": metadata["name"],
        "run_id": context["run_id"],
        "run_attempt": context["run_attempt"],
        "job_id": context["host"]["job_id"],
        "packet_digest": expected["packet_digest"],
        "original_goal": setup.ORIGINAL_GOAL,
        "expires_at": metadata.get("expires_at"),
        "url": f"https://github.com/{setup.REPOSITORY}/actions/runs/{context['run_id']}/artifacts/{metadata['id']}",
    }


def workflow_bytes(source: admission.Source, revision: str) -> bytes:
    setup.require(
        isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision),
        "never-launched history needs an exact immutable workflow revision",
    )
    value = source.get_json(f"/repos/{setup.REPOSITORY}/contents/{COORDINATOR_WORKFLOW}?ref={revision}")
    setup.require(
        value.get("type") == "file"
        and value.get("path") == COORDINATOR_WORKFLOW
        and value.get("encoding") == "base64"
        and type(value.get("size")) is int
        and 0 < value["size"] <= 128 * 1024
        and isinstance(value.get("content"), str)
        and len(value["content"]) <= 256 * 1024,
        "immutable coordinator workflow bytes are unavailable",
    )
    content = base64.b64decode(value["content"].replace("\n", ""), validate=True)
    setup.require(
        len(content) == value["size"]
        and value.get("sha") == hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
        "immutable coordinator workflow blob identity differs",
    )
    return content


def never_launched(
    source: admission.Source, run: Doc, observed: Doc, stored: list[Doc], context: Doc, attempt: int
) -> bool:
    launches = [item for item in observed["steps"] if item.get("name") == LAUNCH_STEP]
    if not (
        len(launches) == 1 and launches[0].get("status") == "completed" and launches[0].get("conclusion") == "skipped"
    ):
        return False
    setup.require(
        run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and run.get("repository", {}).get("id") == run.get("head_repository", {}).get("id") == setup.REPOSITORY_ID
        and isinstance(run.get("head_sha"), str)
        and run.get("path") in {COORDINATOR_WORKFLOW, COORDINATOR_WORKFLOW + "@refs/heads/main"},
        "never-launched run provenance differs from the reviewed fixed coordinator",
    )
    suffix = str(run["id"]) + "-" + str(attempt)
    setup.require(
        not any(
            item.get("name") in {"exacly-req039-review-" + suffix, FENCE_PREFIX + suffix, NOAUTH_PREFIX + suffix}
            for item in stored
        ),
        "never-launched history conflicts with retained setup or authority metadata",
    )
    setup.require(
        workflow_bytes(source, run["head_sha"]) == workflow_bytes(source, context["coordinator_revision"]),
        "never-launched workflow differs from the exact reviewed current coordinator",
    )
    return True


def reject_prior_authority(source: admission.Source, context: Doc) -> None:
    """Require immutable no-authority proof for every earlier potentially relevant coordinator run."""
    workflow = source.get_json(f"/repos/{setup.REPOSITORY}/actions/workflows/req039-coordinator.yml")
    setup.require(
        workflow.get("id") == context["workflow_id"] and workflow.get("path") == COORDINATOR_WORKFLOW,
        "canonical coordinator workflow history changed",
    )
    runs: list[Doc] = []
    for page in range(1, 21):
        history = source.get_json(
            f"/repos/{setup.REPOSITORY}/actions/workflows/{context['workflow_id']}/runs?per_page=100&page={page}"
        )
        setup.require(
            type(history.get("total_count")) is int
            and 0 <= history["total_count"] <= 2000
            and isinstance(history.get("workflow_runs"), list)
            and len(history["workflow_runs"]) <= 100,
            "coordinator history is incomplete or exceeds its bounded scope",
        )
        runs.extend(history["workflow_runs"])
        if len(history["workflow_runs"]) < 100:
            setup.require(len(runs) == history["total_count"], "coordinator history was only partially read")
            break
    else:
        raise ValueError("coordinator history exceeds its bounded supported scope")
    setup.require(len({item.get("id") for item in runs}) == len(runs), "coordinator history has duplicate identities")
    setup.require(
        any(str(item.get("id")) == context["run_id"] for item in runs),
        "current coordinator is missing from canonical history",
    )
    actual_current = [item for item in runs if str(item.get("id")) == context["run_id"]]
    setup.require(
        len(actual_current) == 1 and actual_current[0].get("run_number") == context.get("run_number"),
        "current coordinator run ordering differs from fresh authoritative history",
    )
    for run in runs:
        setup.require(
            type(run.get("id")) is int
            and run["id"] > 0
            and run.get("workflow_id") == context["workflow_id"]
            and type(run.get("run_attempt")) is int
            and 1 <= run["run_attempt"] <= 10,
            "prior coordinator identity/attempt history is ambiguous",
        )
        setup.require(
            type(run.get("run_number")) is int
            and run["run_number"] > 0
            and type(context.get("run_number")) is int
            and context["run_number"] > 0,
            "authoritative coordinator run ordering is unavailable",
        )
        current = str(run["id"]) == context["run_id"]
        if (
            not current
            and run["run_number"] > context["run_number"]
            and run["run_attempt"] == 1
            and run.get("status") in {"queued", "waiting", "pending", "requested"}
        ):
            # The fixed global workflow queue cannot start this newer first attempt before us.
            # Completed/in-progress/rerun histories remain relevant regardless of run ordering.
            continue
        max_attempt = int(context["run_attempt"]) - 1 if current else run["run_attempt"]
        if not max_attempt:
            continue
        stored = artifacts(source, run["id"])
        if any(isinstance(item.get("name"), str) and item["name"].startswith(FENCE_PREFIX) for item in stored):
            raise ValueError("a prior consumed-goal fence forbids new-runner journals or objective budget reset")
        for attempt in range(1, max_attempt + 1):
            observed = job(source, str(run["id"]), str(attempt))
            setup.require(observed.get("status") == "completed", "prior coordinator job has not terminated")
            fence_steps = [item for item in observed["steps"] if item.get("name") == FENCE_STEP]
            setup.require(
                len(fence_steps) == 1 and fence_steps[0].get("conclusion") == "skipped",
                "prior run could have released authority; missing/expired/deleted fence is unknown",
            )
            if never_launched(source, run, observed, stored, context, attempt):
                continue
            proofs = [
                item for item in stored if item.get("name") == NOAUTH_PREFIX + str(run["id"]) + "-" + str(attempt)
            ]
            setup.require(len(proofs) == 1, "prior run has no immutable terminal no-authority proof")
            terminal_steps = [item for item in observed["steps"] if item.get("name") == NOAUTH_STEP]
            setup.require(
                len(terminal_steps) == 1
                and terminal_steps[0].get("conclusion") == "success"
                and terminal_steps[0].get("status") == "completed",
                "prior no-authority artifact lacks its actual successful workflow upload",
            )
            proof, _ = read_artifact(
                source, proofs[0]["id"], run_id=str(run["id"]), name=proofs[0]["name"], member=NOAUTH_MEMBER
            )
            setup.require(
                proof.get("schema_version") == "0.1.0"
                and proof.get("kind") == "req039-terminal-no-authority"
                and proof.get("original_goal") == setup.ORIGINAL_GOAL
                and proof.get("run_id") == str(run["id"])
                and proof.get("run_attempt") == str(attempt)
                and proof.get("independent_approval_seen") is False
                and proof.get("controller_sequence") == 0
                and proof.get("authority_decisions") == []
                and proof.get("reserved") == setup.units(0, 0),
                "prior terminal metadata does not prove that no authority was released",
            )
            stage, abort, old_context = (
                proof.get("preauthority_stage"),
                proof.get("terminal_abort"),
                proof.get("context"),
            )
            setup.require(
                isinstance(stage, dict)
                and set(stage) == {"schema_version", "stage", "launch_digest", "launch_device", "launch_inode"}
                and stage["schema_version"] == "0.1.0"
                and stage["stage"] == "preauthority"
                and type(stage["launch_device"]) is int
                and stage["launch_device"] >= 0
                and type(stage["launch_inode"]) is int
                and stage["launch_inode"] > 0
                and isinstance(stage["launch_digest"], str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", stage["launch_digest"])
                and isinstance(abort, dict)
                and abort
                == {
                    "schema_version": "0.1.0",
                    "launch_digest": stage["launch_digest"],
                    "stage": stage,
                    "reason": "terminal-never-authorized",
                }
                and isinstance(old_context, dict)
                and old_context.get("run_id") == str(run["id"])
                and old_context.get("run_attempt") == str(attempt)
                and old_context.get("host", {}).get("job_id") == str(observed["id"]),
                "terminal no-authority proof lacks its exact irreversible preauthority stop stage",
            )
            setup.require(
                proof.get("proof_kind") in {"setup-only-never-authorized", "native-zero-history"},
                "unsupported terminal no-authority proof stage",
            )
            if proof["proof_kind"] == "native-zero-history":
                account = proof.get("accounting_history")
                setup.require(
                    isinstance(account, dict)
                    and account.get("decisions") == []
                    and account.get("reserved") == setup.units(0, 0),
                    "native no-authority receipt lacks actual zero accounting history",
                )
