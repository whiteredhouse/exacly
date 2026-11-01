"""One explicitly approved same-runner REQ-039 pilot; no deployment or release authority.

Launch keeps protected journals and a bounded service alive while a human reviews the sanitized
packet. Only a fresh, unedited server comment by the selected immutable GitHub user can approve
that exact packet. No command posts comments or accepts a local JSON approval assertion.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request

from exacly import (
    automation_authorization,
    automation_controller,
    automation_revocations,
    executions,
    git_remote_transport,
    live_pilot_evidence,
    planning,
    producer_selection,
    remote_publication,
    resource_guard,
    standing_authorization,
    state_writer,
    worker_integration,
)
from exacly.execution_store import invalid_constant, unique_object
from exacly.github_dispatch import GitHubIssueAdapter
from exacly.github_evidence import API_ORIGIN, GitHubClient, read_bounded

from ci import req039_bootstrap as setup
from ci import req039_history as history

Doc = dict[str, Any]
ISSUER_ID = 1679024
APPROVAL_ISSUE = 93
HEADER = "EXACLY REQ-039 SAME-RUNNER APPROVAL"
UNIT_SECONDS = 1800
APPROVAL_SECONDS = 900
PUBLISH_TOKEN = "EXACLY_REQ039_PUBLISH_TOKEN"
PUBLISH_APP = "EXACLY_REQ039_PUBLISH_APP_ID"
PUBLISH_INSTALLATION = "EXACLY_REQ039_PUBLISH_INSTALLATION_ID"
PUBLISH_EXPIRY = "EXACLY_REQ039_PUBLISH_EXPIRES_AT"
FORWARDED = (
    "GITHUB_TOKEN",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_REPOSITORY",
    "GITHUB_SHA",
    "GITHUB_JOB",
    "RUNNER_NAME",
)
APPROVAL_FIELDS = {
    "schema_version",
    "decision",
    "original_goal",
    "repository_id",
    "run_id",
    "run_attempt",
    "job_id",
    "boot_id",
    "nonce",
    "packet_digest",
    "producer_policy_digest",
    "delegation_digest",
    "workflow_contract_digest",
    "resource_guard_digest",
    "publisher_mint_permissions_confirmed",
}
APPROVAL_QUERY = """query Req039OriginalHumanApproval($id: ID!) {
  node(id: $id) { ... on IssueComment {
    id fullDatabaseId body createdAt updatedAt lastEditedAt
    author { __typename ... on User { databaseId } }
    editor { __typename }
    issue { number repository { databaseId nameWithOwner } }
  } }
}"""


class Comments(Protocol):
    def comments(self, page: int) -> list[Doc]: ...
    def comment(self, identity: int) -> Doc: ...


class ApprovalReader:
    """Read-only authenticated GitHub source; never uses the publisher credential."""

    def __init__(self, source: GitHubClient) -> None:
        self.source = source

    def comments(self, page: int) -> list[Doc]:
        path = f"/repos/{setup.REPOSITORY}/issues/{APPROVAL_ISSUE}/comments?per_page=100&page={page}"
        status, _, content = self.source._request(API_ORIGIN + path, authenticated=True, limit=2 * 1024**2)
        setup.require(status == 200, "approval history is unavailable")
        values = json.loads(content)
        setup.require(
            isinstance(values, list) and len(values) <= 100 and all(isinstance(item, dict) for item in values),
            "approval history has an unsupported response",
        )
        return list(values)

    def comment(self, identity: int) -> Doc:
        setup.require(type(identity) is int and identity > 0, "invalid approval comment identity")
        value = self.source.get_json(f"/repos/{setup.REPOSITORY}/issues/comments/{identity}")
        node = value.get("node_id")
        setup.require(
            isinstance(node, str) and 0 < len(node) <= 256, "approval comment has no immutable GraphQL node identity"
        )
        request = Request(
            API_ORIGIN + "/graphql",
            data=json.dumps({"query": APPROVAL_QUERY, "variables": {"id": node}}).encode(),
            headers={
                "Authorization": "Bearer " + self.source._token,
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
                "User-Agent": "exacly-req039-read-only-approval",
            },
            method="POST",
        )
        try:
            # A literal query, never a GraphQL mutation or comment-posting operation.
            with self.source._opener.open(request, timeout=30) as response:
                setup.require(response.getcode() == 200, "immutable approval edit history is unavailable")
                result = json.loads(
                    read_bounded(response, 128 * 1024), object_pairs_hook=unique_object, parse_constant=invalid_constant
                )
        except (HTTPError, URLError, OSError, ValueError):
            raise ValueError("immutable original-human approval facts are unavailable") from None
        setup.require(
            isinstance(result, dict) and not result.get("errors") and isinstance(result.get("data"), dict),
            "approval GraphQL query did not establish complete facts",
        )
        original = result["data"].get("node")
        setup.require(
            isinstance(original, dict)
            and original.get("id") == node
            and decimal_identity(original.get("fullDatabaseId")) == identity
            and original.get("body") == value.get("body")
            and utc(original["createdAt"]) == utc(value["created_at"])
            and utc(original["updatedAt"]) == utc(value["updated_at"])
            and original.get("issue", {}).get("number") == APPROVAL_ISSUE
            and original.get("issue", {}).get("repository", {}).get("databaseId") == setup.REPOSITORY_ID
            and original.get("issue", {}).get("repository", {}).get("nameWithOwner") == setup.REPOSITORY,
            "REST and original approval node facts conflict",
        )
        value["original_approval"] = original
        return value


def decimal_identity(value: object) -> int | None:
    """Normalize GraphQL BigInt without accepting booleans, exponent/decimal forms or aliases."""
    if type(value) is int and 0 < value <= 2**63 - 1:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        parsed = int(value)
        if parsed <= 2**63 - 1:
            return parsed
    return None


def utc(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    setup.require(value.utcoffset() is not None, "approval timestamps must identify a timezone")
    return value.astimezone(dt.UTC)


def approval_value(packet: Doc) -> Doc:
    context = packet["context"]
    return {
        "schema_version": "0.1.0",
        "decision": "approve",
        "original_goal": setup.ORIGINAL_GOAL,
        "repository_id": setup.REPOSITORY_ID,
        "run_id": context["run_id"],
        "run_attempt": context["run_attempt"],
        "job_id": context["host"]["job_id"],
        "boot_id": context["host"]["boot_id"],
        "nonce": context["nonce"],
        "packet_digest": packet["digest"],
        "producer_policy_digest": planning.digest(packet["producer_policy"]),
        "delegation_digest": planning.digest(packet["delegation"]),
        "workflow_contract_digest": planning.digest(packet["workflow_contract"]),
        "resource_guard_digest": packet["resource_guard"]["digest"],
        "publisher_mint_permissions_confirmed": True,
    }


def approval_body(packet: Doc) -> str:
    return HEADER + "\n" + planning.canonical(approval_value(packet))


def parsed_approval(comment: Doc) -> Doc | None:
    """Only immutable human-issued server fields establish an approval-channel message."""
    user = comment.get("user")
    if not (
        isinstance(user, dict)
        and type(user.get("id")) is int
        and user["id"] == ISSUER_ID
        and user.get("type") == "User"
        and "performed_via_github_app" in comment
        and comment["performed_via_github_app"] is None
        and isinstance(comment.get("created_at"), str)
        and comment.get("updated_at") == comment["created_at"]
        and comment.get("issue_url") == f"{API_ORIGIN}/repos/{setup.REPOSITORY}/issues/{APPROVAL_ISSUE}"
    ):
        return None
    original = comment.get("original_approval")
    if not (
        isinstance(original, dict)
        and "lastEditedAt" in original
        and original["lastEditedAt"] is None
        and "editor" in original
        and original["editor"] is None
        and original.get("body") == comment.get("body")
        and original.get("author", {}).get("__typename") == "User"
        and type(original.get("author", {}).get("databaseId")) is int
        and original["author"]["databaseId"] == ISSUER_ID
    ):
        return None
    body = comment.get("body")
    if not isinstance(body, str) or len(body.encode()) > 16384 or not body.startswith(HEADER + "\n"):
        return None
    try:
        value = json.loads(body[len(HEADER) + 1 :], object_pairs_hook=unique_object, parse_constant=invalid_constant)
        setup.require(
            isinstance(value, dict)
            and set(value) == APPROVAL_FIELDS
            and value["schema_version"] == "0.1.0"
            and value["decision"] == "approve"
            and value["publisher_mint_permissions_confirmed"] is True
            and type(value["repository_id"]) is int
            and value["repository_id"] == setup.REPOSITORY_ID,
            "unsupported approval message",
        )
        for name in (
            "original_goal",
            "packet_digest",
            "producer_policy_digest",
            "delegation_digest",
            "workflow_contract_digest",
            "resource_guard_digest",
        ):
            setup.require(
                isinstance(value[name], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value[name]),
                "invalid approval digest",
            )
        for name in ("run_id", "run_attempt", "job_id"):
            setup.require(
                isinstance(value[name], str) and re.fullmatch(r"[1-9][0-9]{0,19}", value[name]),
                "invalid approval run identity",
            )
        setup.require(
            isinstance(value["nonce"], str) and re.fullmatch(r"[0-9a-f]{32}", value["nonce"]), "invalid approval nonce"
        )
        setup.require(str(uuid.UUID(value["boot_id"])) == value["boot_id"], "invalid approval boot")
        utc(comment["created_at"])
        setup.require(
            body == HEADER + "\n" + planning.canonical(value), "approval body must match the exact reviewed packet text"
        )
        return dict(value)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def find_approval(reader: Comments, packet: Doc) -> Doc | None:
    """Fresh discovery also tombstones prior approvals; another store/run cannot reset this goal."""
    expected = approval_value(packet)
    match: Doc | None = None
    for page in range(1, 21):
        comments = reader.comments(page)
        for candidate in comments:
            identity = candidate.get("id")
            if type(identity) is not int or identity < 1:
                raise ValueError("approval history contains an ambiguous comment identity")
            fresh = reader.comment(identity)
            if fresh.get("user", {}).get("id") == ISSUER_ID:
                original = fresh.get("original_approval")
                setup.require(
                    isinstance(original, dict)
                    and "lastEditedAt" in original
                    and original["lastEditedAt"] is None
                    and "editor" in original
                    and original["editor"] is None
                    and fresh.get("created_at") == fresh.get("updated_at")
                    and "performed_via_github_app" in fresh
                    and fresh["performed_via_github_app"] is None,
                    "edited human approval history is ambiguous; it cannot reset an existing logical goal",
                )
            value = parsed_approval(fresh)
            if value is None or value["original_goal"] != setup.ORIGINAL_GOAL:
                continue
            setup.require(
                value == expected, "prior human approval tombstones this logical goal on another packet/run/store"
            )
            created = utc(fresh["created_at"])
            setup.require(
                utc(packet["context"]["created_at"]) <= created <= utc(packet["context"]["deadline_at"]),
                "approval is outside this same-runner packet lifetime",
            )
            setup.require(fresh.get("id") == identity, "fresh approval identity changed")
            match = {
                "comment_id": identity,
                "user_id": ISSUER_ID,
                "url": fresh.get("html_url"),
                "created_at": fresh["created_at"],
                "packet_digest": packet["digest"],
                "body_digest": planning.digest(fresh["body"]),
                "original_comment": fresh["original_approval"],
            }
        if len(comments) < 100:
            return match
    raise ValueError("approval history exceeds the bounded supported scan; preserve journals and review")


def verify_approval(reader: Comments, packet: Doc, receipt: Doc) -> None:
    fresh = reader.comment(receipt["comment_id"])
    setup.require(
        parsed_approval(fresh) == approval_value(packet)
        and planning.digest(fresh.get("body")) == receipt["body_digest"]
        and fresh.get("id") == receipt["comment_id"]
        and receipt["user_id"] == ISSUER_ID
        and fresh.get("original_approval") == receipt["original_comment"],
        "independent approval is unavailable, edited or stale",
    )


def source() -> GitHubClient:
    token = os.environ.get("GITHUB_TOKEN", "")
    setup.require(bool(token), "read-only GITHUB_TOKEN is required for fresh approval and producer reads")
    return GitHubClient(token)


def publisher_metadata(context: Doc, api: GitHubClient) -> Doc:
    """Combine externally approved mint metadata with actual same-token repository scope."""
    publisher = os.environ.get(PUBLISH_TOKEN, "")

    def positive(name: str) -> int | None:
        value = os.environ.get(name, "")
        return int(value) if re.fullmatch(r"[1-9][0-9]{0,19}", value) else None

    app, installation = positive(PUBLISH_APP), positive(PUBLISH_INSTALLATION)
    # An explicitly excluded identity is unavailable candidate metadata; it cannot compile into live authority.
    if app == 15368:
        app = None
    expiry: str | None = os.environ.get(PUBLISH_EXPIRY) or None
    try:
        parsed = utc(expiry) if expiry is not None else None
    except (ValueError, TypeError):
        expiry, parsed = None, None
    value = {
        "environment": PUBLISH_TOKEN,
        "present": bool(publisher),
        "scope_verified": False,
        "app_id": app,
        "installation_id": installation,
        "expires_at": expiry,
        "issuer_evidence": "trusted-workflow-mint-metadata",
        "issuer_server_verified": False,
    }
    if (
        not publisher
        or publisher == os.environ.get("GITHUB_TOKEN")
        or app in {None, 15368}
        or installation is None
        or parsed is None
        or parsed < utc(context["created_at"]) + dt.timedelta(seconds=UNIT_SECONDS)
    ):
        return value
    transport = git_remote_transport.GitHubGitTransport(
        "https://github.com/" + setup.REPOSITORY + ".git",
        str(setup.REPOSITORY_ID),
        api,
        token_environment=PUBLISH_TOKEN,
    )
    try:
        transport._publisher_scope(token=publisher, timeout=30)
    except (ValueError, OSError):
        return value
    value["scope_verified"] = True
    return value


def same_host(launch: Doc) -> None:
    context = launch["context"]
    setup.require(
        os.environ.get("GITHUB_REPOSITORY") == setup.REPOSITORY
        and os.environ.get("GITHUB_RUN_ID") == context["run_id"]
        and os.environ.get("GITHUB_RUN_ATTEMPT") == context["run_attempt"]
        and os.environ.get("GITHUB_JOB") == context["job_key"]
        and Path("/proc/sys/kernel/random/boot_id").read_text().strip() == context["host"]["boot_id"],
        "new-runner/run/store recovery is unsupported; do not recreate journals",
    )
    storage = launch.get("storage")
    setup.require(
        isinstance(storage, dict) and set(storage) == {"path", "device", "inode"},
        "launch storage identity is unavailable",
    )
    assert isinstance(storage, dict)
    path = setup.private_directory(Path(storage["path"]))
    found = path.stat()
    setup.require(
        (found.st_dev, found.st_ino) == (storage["device"], storage["inode"]), "private launch storage was replaced"
    )


def preauthority_stage(root: Path, launch: Doc) -> Doc:
    value = setup.read_json(root / "preauthority-stage.json")
    found = (root / "launch.json").lstat()
    setup.require(
        value
        == {
            "schema_version": "0.1.0",
            "stage": "preauthority",
            "launch_digest": planning.digest(launch),
            "launch_device": found.st_dev,
            "launch_inode": found.st_ino,
        },
        "durable preauthority stage or launch file identity changed",
    )
    return value


def not_aborted(root: Path) -> None:
    setup.require(
        not os.path.lexists(root / "terminal-abort.json"),
        "this coordinator was irreversibly stopped; new authority is forbidden",
    )


def live_context(api: GitHubClient) -> Doc:
    setup.require(
        os.environ.get("GITHUB_REPOSITORY") == setup.REPOSITORY, "pilot is restricted to its approved consumer"
    )
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    setup.require(
        re.fullmatch(r"[1-9][0-9]*", run_id) and re.fullmatch(r"[1-9][0-9]*", attempt),
        "actual Actions run/attempt required",
    )
    run = api.get_json(f"/repos/{setup.REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}")
    setup.require(
        run.get("id") == int(run_id)
        and run.get("run_attempt") == int(attempt)
        and run.get("repository", {}).get("id") == setup.REPOSITORY_ID
        and run.get("head_repository", {}).get("id") == setup.REPOSITORY_ID
        and run.get("head_sha") == os.environ.get("GITHUB_SHA")
        and isinstance(run.get("head_sha"), str)
        and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
        and run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch"
        and type(run.get("workflow_id")) is int
        and run["workflow_id"] > 0
        and type(run.get("run_number")) is int
        and run["run_number"] > 0
        and run.get("path") in {history.COORDINATOR_WORKFLOW, history.COORDINATOR_WORKFLOW + "@refs/heads/main"},
        "coordinator run repository or identity differs",
    )
    jobs = api.get_json(f"/repos/{setup.REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    matches = [
        job
        for job in jobs.get("jobs", [])
        if job.get("runner_name") == os.environ.get("RUNNER_NAME")
        and job.get("name") == os.environ.get("GITHUB_JOB")
        and job.get("status") == "in_progress"
    ]
    setup.require(
        len(matches) == 1 and type(matches[0].get("id")) is int, "one actual current coordinator job is required"
    )
    created = dt.datetime.now(dt.UTC).replace(microsecond=0)
    return {
        "run_id": run_id,
        "run_attempt": attempt,
        "job_key": os.environ["GITHUB_JOB"],
        "nonce": uuid.uuid4().hex,
        "coordinator_revision": os.environ.get("GITHUB_SHA"),
        "workflow_id": run["workflow_id"],
        "run_number": run["run_number"],
        "workflow_path": history.COORDINATOR_WORKFLOW,
        "created_at": created.isoformat(),
        "deadline_at": (created + dt.timedelta(seconds=UNIT_SECONDS - 60)).isoformat(),
        "host": automation_controller.host_identity(run_id=run_id, job_id=str(matches[0]["id"])),
    }


def unit_command(root: Path, checkout: Path, base: str, context: Doc, unit: str, baseline_run: int | None) -> list[str]:
    """Closed 1800-second coordinator profile, separately reviewed from the 600-second helper."""
    setup.require(re.fullmatch(r"exacly-req039-[0-9a-f]{32}", unit), "invalid exact coordinator unit identity")
    setup.require(os.geteuid() != 0 and os.getegid() != 0, "pilot service must run as the current non-root operator")
    setup.require(
        checkout.is_absolute() and checkout.resolve() == checkout and checkout.is_dir(), "invalid trusted checkout"
    )
    setup.private_directory(root)
    setup.require(re.fullmatch(r"[0-9a-f]{40}", base), "an exact approved consumer base is required")
    names = [
        *FORWARDED,
        *(name for name in (PUBLISH_TOKEN, PUBLISH_APP, PUBLISH_INSTALLATION, PUBLISH_EXPIRY) if os.environ.get(name)),
    ]
    setup.require(
        all(name in os.environ for name in FORWARDED), "actual Actions context and reader token must be configured"
    )
    # These names are literal trusted configuration. Values never appear in the command vector.
    arguments = [
        sys.executable,
        "-m",
        "ci.req039_pilot",
        "coordinate",
        "--root",
        str(root),
        "--checkout",
        str(checkout),
        "--base",
        base,
    ]
    if baseline_run is not None:
        setup.require(type(baseline_run) is int and baseline_run > 0, "invalid initial producer run")
        arguments += ["--baseline-run", str(baseline_run)]
    return [
        "/usr/bin/sudo",
        "-n",
        "--preserve-env=" + ",".join(names),
        "/usr/bin/systemd-run",
        "--quiet",
        "--collect",
        "--unit=" + unit,
        "--uid=" + str(os.geteuid()),
        "--gid=" + str(os.getegid()),
        "--working-directory=" + str(checkout),
        "--property=CPUQuota=100%",
        "--property=CPUQuotaPeriodSec=100ms",
        "--property=MemoryMax=1073741824",
        "--property=MemorySwapMax=0",
        "--property=TasksMax=128",
        "--property=RuntimeMaxSec=1800",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=continue",
        "--setenv=EXACLY_RESOURCE_CGROUP=/system.slice/" + unit + ".service",
        *("--setenv=" + name for name in names),
        "--",
        *arguments,
    ]


def status(root: Path, state: str, **values: Any) -> None:
    setup.write_json(
        root / "public" / "status.json", {"schema_version": "0.1.0", "status": state, **values}, replace=True
    )


def packet_for(bundle: Doc, *, publisher_available: bool) -> Doc:
    packet = {
        "schema_version": "0.1.0",
        "scope": "req039-fixed-single-consumer-same-runner",
        "context": bundle["context"],
        "repository": {"id": setup.REPOSITORY_ID, "full_name": setup.REPOSITORY},
        "consumer_branch": setup.BRANCH,
        "input_revision": bundle["base"],
        "original_goal": setup.ORIGINAL_GOAL,
        "producer_policy": bundle["trust"],
        "delegation": bundle["delegation"],
        "workflow_contract": bundle["contract"],
        "components": bundle["components"],
        "resource_guard": bundle["resource_guard"],
        "worker_identity": bundle["worker_identity"],
        "baseline_selection": bundle["baseline_selection"],
        "implementation": bundle.get("implementation"),
        "scope_tests": bundle.get("scope_tests"),
        "faults": ["controller-crash-after-native-dispatch", "drop-first-real-publication-response"],
        "credential_requirements": {
            "environment": PUBLISH_TOKEN,
            "type": "repository-scoped GitHub App installation token",
            "repository_ids": [setup.REPOSITORY_ID],
            "permissions": {"contents": "write", "actions": "read", "issues": "write"},
            "permissions_server_verified": False,
            "permissions_independent_approval_required": True,
            "scope_verification": "same-token-single-repository-only",
            "required_environment": [PUBLISH_TOKEN, PUBLISH_APP, PUBLISH_INSTALLATION, PUBLISH_EXPIRY],
            "unavailable_requirements": [
                name
                for name, key in (
                    (PUBLISH_TOKEN, "present"),
                    (PUBLISH_APP, "app_id"),
                    (PUBLISH_INSTALLATION, "installation_id"),
                    (PUBLISH_EXPIRY, "expires_at"),
                )
                if not bundle["components"].get("publisher", {}).get(key)
            ]
            + (
                ["same-token-repository-scope"]
                if not bundle["components"].get("publisher", {}).get("scope_verified")
                else []
            )
            + ["independently-confirmed-mint-permissions"],
            **bundle["components"].get(
                "publisher",
                {
                    "environment": PUBLISH_TOKEN,
                    "present": publisher_available,
                    "scope_verified": False,
                    "app_id": None,
                    "installation_id": None,
                    "expires_at": None,
                    "issuer_evidence": "trusted-workflow-mint-metadata",
                    "issuer_server_verified": False,
                },
            ),
        },
        "limits": {
            "unit_seconds": UNIT_SECONDS,
            "approval_seconds": APPROVAL_SECONDS,
            "runner_loss_recovery": False,
            "coding_agent": False,
            "merge_release_deployment": False,
            "max_producer_polls": setup.MAX_PRODUCER_POLLS,
            "conservative_wall_reservation_units": setup.RESERVED_CALLS,
            "reservation_wall_time_ms": setup.RESERVED_MILLISECONDS,
            "reservation_allocation": setup.RESERVATION_ALLOCATION,
            "reservation_unit_ms": 45000,
            "hard_callback_count_claimed": False,
        },
        "approval_channel": {
            "repository": setup.REPOSITORY,
            "issue": APPROVAL_ISSUE,
            "user_id": ISSUER_ID,
            "unedited_human_comment_required": True,
        },
    }
    packet["digest"] = planning.digest(packet)
    return packet


def fence_document(packet: Doc, receipt: Doc) -> Doc:
    """Control metadata for the fixed workflow uploader, not verification evidence."""
    return {
        "schema_version": "0.1.0",
        "kind": "req039-consumed-goal-fence",
        "original_goal": setup.ORIGINAL_GOAL,
        "packet_digest": packet["digest"],
        "context": packet["context"],
        "approval": receipt,
        "approved_contracts": approval_value(packet),
    }


def terminal_noauthority(root: Path) -> bool:
    """Export no-authority proof only from intact native zero-history stores before approval."""
    launch = setup.read_json(root / "launch.json")
    same_host(launch)
    setup.require(
        unit_state(launch["unit"]) in {"inactive", "failed"}, "whole unit termination is not independently established"
    )
    stage = preauthority_stage(root, launch)
    if any(
        os.path.lexists(root / name)
        for name in (
            "approval.json",
            "fence.json",
            "authority-construction.json",
            "publication-fault-intent.json",
            "dispatch-controller-crash.json",
        )
    ):
        return False
    abort = {
        "schema_version": "0.1.0",
        "launch_digest": planning.digest(launch),
        "stage": stage,
        "reason": "terminal-never-authorized",
    }
    if not os.path.lexists(root / "terminal-abort.json"):
        setup.write_json(root / "terminal-abort.json", abort)
    else:
        setup.require(setup.read_json(root / "terminal-abort.json") == abort, "terminal abort marker changed")
    if not os.path.lexists(root / "bootstrap.json"):
        setup.require(
            {item.name for item in root.iterdir()}
            <= {"launch.json", "preauthority-stage.json", "public", "terminal-abort.json"},
            "partial private stores cannot establish setup-only never-authorized proof",
        )
        setup.require(
            {item.name for item in (root / "public").iterdir()} <= {"status.json", history.NOAUTH_MEMBER},
            "unknown public setup data cannot establish never-authorized proof",
        )
        proof = {
            "schema_version": "0.1.0",
            "kind": "req039-terminal-no-authority",
            "proof_kind": "setup-only-never-authorized",
            "original_goal": setup.ORIGINAL_GOAL,
            "run_id": launch["context"]["run_id"],
            "run_attempt": launch["context"]["run_attempt"],
            "context": launch["context"],
            "independent_approval_seen": False,
            "controller_sequence": 0,
            "authority_decisions": [],
            "reserved": setup.units(0, 0),
            "preauthority_stage": stage,
            "terminal_abort": abort,
        }
        destination = root / "public" / history.NOAUTH_MEMBER
        if not destination.exists():
            setup.write_json(destination, proof)
        else:
            setup.require(setup.read_json(destination) == proof, "never-authorized terminal receipt changed")
        return True
    bundle = setup.read_json(root / "bootstrap.json")
    setup.require(bundle["context"] == launch["context"], "bootstrap differs from intact original preauthority launch")
    delegation = bundle["delegation"]
    accounting = standing_authorization.inspect(
        Path(delegation["accounting"]["path"]),
        store_id=delegation["accounting"]["store_id"],
        objective_id=delegation["objective"]["id"],
    )
    controller = bundle["components"]["controller"]
    retained = automation_controller.show(Path(controller["journal"]), controller["journal_id"])
    setup.require(
        accounting["decisions"] == []
        and accounting["reserved"] == setup.units(0, 0)
        and retained["sequence"] == 0
        and retained["event"]["pending"] is None,
        "native history does not establish a terminal no-authority case",
    )
    proof = {
        "schema_version": "0.1.0",
        "kind": "req039-terminal-no-authority",
        "proof_kind": "native-zero-history",
        "original_goal": setup.ORIGINAL_GOAL,
        "run_id": bundle["context"]["run_id"],
        "run_attempt": bundle["context"]["run_attempt"],
        "context": bundle["context"],
        "independent_approval_seen": False,
        "controller_sequence": 0,
        "controller_head": retained["head"],
        "authority_decisions": [],
        "reserved": accounting["reserved"],
        "accounting_history": accounting,
        "preauthority_stage": stage,
        "terminal_abort": abort,
    }
    path = root / "public" / history.NOAUTH_MEMBER
    if not path.exists():
        setup.write_json(path, proof)
    else:
        setup.require(setup.read_json(path) == proof, "terminal no-authority metadata changed")
    return True


def runtime_objects(
    root: Path,
) -> tuple[Doc, Doc, automation_controller.NativeWorkflow, automation_authorization.Authorizer]:
    launch = setup.read_json(root / "launch.json")
    same_host(launch)
    not_aborted(root)
    preauthority_stage(root, launch)
    bundle = setup.read_json(root / "bootstrap.json")
    packet = setup.read_json(root / "public" / "packet.json")
    setup.require(
        planning.digest({key: value for key, value in packet.items() if key != "digest"}) == packet["digest"],
        "reviewed packet changed",
    )
    setup.require(
        bundle["context"] == launch["context"]
        and packet_for(bundle, publisher_available=packet["credential_requirements"]["present"]) == packet,
        "private policy, delegation or stores differ from the independently approved packet",
    )
    setup.read_json(root / "approval.json")
    api = source()
    guard = resource_guard.from_descriptor(bundle["resource_guard"])
    guard.check()
    publisher = os.environ.get(PUBLISH_TOKEN, "")
    setup.require(bool(publisher), "publisher credential unavailable; no effects authorized")

    class DropFirstResponse(git_remote_transport.GitHubGitTransport):
        def _exchange(self, data: bytes | None, *, token: str, timeout: float) -> bytes:
            setup.require(
                token == publisher, "Git and dispatch credentials differ from the selected same-token identity"
            )
            return super()._exchange(data, token=token, timeout=timeout)

        def _publisher_scope(self, *, token: str, timeout: float) -> None:
            setup.require(token == publisher, "Git publisher credential changed from the selected dispatch identity")
            return super()._publisher_scope(token=token, timeout=timeout)

        def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
            intent = root / "publication-fault-intent.json"
            if not intent.exists():
                setup.write_json(intent, {"operation_id": operation["id"], "packet_digest": packet["digest"]})
            else:
                setup.require(
                    setup.read_json(intent)["operation_id"] == operation["id"],
                    "publication fault belongs to another exact operation",
                )
            super().publish(operation, repository, timeout=timeout)
            dropped = root / "publication-response-dropped.json"
            if not dropped.exists():
                setup.write_json(
                    dropped,
                    {
                        "operation_id": operation["id"],
                        "packet_digest": packet["digest"],
                        "actual_exchange_returned": True,
                    },
                )
                raise ConnectionResetError("Pilot intentionally discarded the first actual publication response.")

    transport = DropFirstResponse(
        "https://github.com/" + setup.REPOSITORY + ".git",
        str(setup.REPOSITORY_ID),
        api,
        token_environment=PUBLISH_TOKEN,
    )
    components = bundle["components"]
    setup.require(
        live_pilot_evidence.implementation_fingerprint(
            Path(launch["checkout"]),
            Path(components["handoff_policy"]["storage"]["repository"]),
            bundle["context"]["coordinator_revision"],
        )
        == bundle["implementation"],
        "executing trusted implementation differs from independently approved fixed snapshot",
    )
    prepared = worker_integration.prepare(
        Path(components["handoff_policy"]["storage"]["repository"]),
        repository_id=components["handoff_policy"]["storage"]["repository_id"],
        revision=bundle["base"],
        allowed_paths=[setup.SOURCE],
        protected_paths=setup.PROTECTED_PATHS,
        model_digest=bundle["trust"]["model"]["digest"],
    )
    setup.require(prepared.identity == bundle["worker_identity"], "prepared worker identity changed since approval")
    services = automation_controller.NativeServices(
        api, transport, prepared, guard, producer_selection, dispatch_client=GitHubIssueAdapter(publisher)
    )

    class RestartProbe(automation_controller.NativeWorkflow):
        def accept(self, phase: str, request: Doc, native_receipt: Doc, correlations: Doc) -> Doc:
            result = super().accept(phase, request, native_receipt, correlations)
            marker = root / "dispatch-controller-crash.json"
            if phase == "dispatch" and not marker.exists():
                setup.write_json(
                    marker, {"packet_digest": packet["digest"], "phase": phase, "native_receipt": native_receipt}
                )
                os.kill(os.getpid(), signal.SIGKILL)
            return result

    workflow = RestartProbe(
        contract=bundle["contract"],
        documents=bundle["documents"],
        plan=bundle["plan"],
        readiness_policy=bundle["readiness"],
        trust_policy=bundle["trust"],
        components=components,
        services=services,
    )
    delegation = bundle["delegation"]
    construction = {
        "schema_version": "0.1.0",
        "packet_digest": packet["digest"],
        "delegation_digest": planning.digest(delegation),
        "stage": "authority-may-exist",
    }
    marker = root / "authority-construction.json"
    if not os.path.lexists(marker):
        setup.write_json(marker, construction)
    else:
        setup.require(setup.read_json(marker) == construction, "authority construction marker changed")
    readers = {
        **workflow.reader_registry(),
        "authority-preflight": AuthorityPreflight(root, bundle, packet, workflow),
        "budget-exhaustion": BudgetProbe(bundle, workflow),
        "remote-precondition": RemotePrecondition(bundle, workflow),
    }
    authority = automation_authorization.Authorizer(
        Path(delegation["config_path"]),
        approved_digest=planning.digest(delegation),
        issuer=delegation["issuer"],
        approval_id=delegation["approval_id"],
        effects=workflow.effect_registry(),
        readers=readers,
    )
    return bundle, packet, workflow, authority


class AuthorityPreflight:
    """One closed whole-call read: no network is allowed in local authority construction."""

    adapter_id = "req039-authority-preflight-v1"

    def __init__(self, root: Path, bundle: Doc, packet: Doc, workflow: automation_controller.NativeWorkflow) -> None:
        self.root, self.bundle, self.packet, self.workflow = root, bundle, packet, workflow

    def inspect(self, request: Doc) -> Doc:
        selected = self.bundle["components"]["authority_preflight"]
        setup.require(
            set(request) == {"contract", "fence_reference"} and request["contract"] == selected,
            "authority preflight differs from the independently approved contract",
        )
        reference = setup.read_json(self.root / "fence-reference.json")
        setup.require(request["fence_reference"] == reference, "authority preflight selects another immutable fence")
        setup.require(
            self.workflow.configuration_digest == self.bundle["delegation"]["native_configuration_digest"],
            "authority preflight configuration pin changed",
        )
        not_aborted(self.root)
        return {
            "schema_version": "0.1.0",
            "repository": self.bundle["trust"]["repository"],
            "contract_digest": planning.digest(selected),
            "wall_time_ms": selected["wall_time_ms"],
            "native_configuration_digest": self.workflow.configuration_digest,
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        facts = self.inspect(request)
        setup.require(wall_time_ms == facts["wall_time_ms"], "authority preflight changed its reserved deadline")

        def fresh() -> Doc:
            not_aborted(self.root)
            launch = setup.read_json(self.root / "launch.json")
            same_host(launch)
            self.workflow.services.guard.check()
            receipt = setup.read_json(self.root / "approval.json")
            api = source()
            verify_approval(ApprovalReader(api), self.packet, receipt)
            try:
                fence = history.verify_fence(
                    self.workflow.services.source,
                    self.bundle["context"],
                    request["fence_reference"],
                    fence_document(self.packet, receipt),
                )
            except history.FencePending:
                return {"status": "fence_pending"}
            credential = publisher_metadata(self.bundle["context"], api)
            setup.require(
                credential == self.bundle["components"]["publisher"] and credential["scope_verified"],
                "publisher repository scope or independently approved mint metadata changed",
            )
            retained_path = self.root / "fence.json"
            if os.path.lexists(retained_path):
                setup.require(setup.read_json(retained_path) == fence, "canonical consumed-goal fence changed")
            return {
                "status": "verified",
                "fence": fence,
                "publisher": credential,
                "packet_digest": self.packet["digest"],
            }

        remaining = (utc(self.bundle["context"]["deadline_at"]) - dt.datetime.now(dt.UTC)).total_seconds()
        setup.require(remaining > 0, "authority preflight whole-unit deadline exhausted")
        observed = self.workflow.services.boundary(fresh, min(wall_time_ms / 1000, remaining))
        setup.require(
            observed.get("status") in {"verified", "fence_pending"},
            "fresh approval/fence/publisher preflight was interrupted; preserve its spent capacity",
        )
        return observed


class RemotePrecondition:
    """Observe the real published ref against the genuine old base; never release a write."""

    adapter_id = "native-remote-precondition-v1"

    def __init__(self, bundle: Doc, workflow: automation_controller.NativeWorkflow) -> None:
        self.bundle, self.workflow = bundle, workflow

    def inspect(self, request: Doc) -> Doc:
        selected = self.bundle["scope_tests"]["remote_precondition"]
        controller = self.bundle["components"]["controller"]
        retained = automation_controller.show(Path(controller["journal"]), controller["journal_id"])
        setup.require(
            retained["event"]["status"] == "completed"
            and set(request) == {"contract", "handoff_id"}
            and request["contract"] == selected
            and request["handoff_id"] == retained["event"]["correlations"]["handoff"]["handoff_id"],
            "remote precondition read differs from its approved completed native output",
        )
        return {
            "schema_version": "0.1.0",
            "repository": selected["repository"],
            "contract_digest": planning.digest(selected),
            "native_configuration_digest": self.workflow.configuration_digest,
            "wall_time_ms": selected["wall_time_ms"],
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        facts = self.inspect(request)
        setup.require(wall_time_ms == facts["wall_time_ms"], "remote precondition changed its reserved bound")

        def observe() -> Doc:
            selected = self.bundle["scope_tests"]["remote_precondition"]
            document = {**selected["request_base"], "handoff_id": request["handoff_id"]}
            proposal = remote_publication.request_from_document(document)
            authorization = remote_publication.preview(proposal)["authorization_template"]
            report = remote_publication.inspect_precondition(
                proposal, authorization=authorization, transport=self.workflow.services.transport, timeout=30
            )
            setup.require(
                remote_publication.lookup(proposal.journal, proposal.journal_id, report["operation_id"]) is None,
                "read-only precondition probe unexpectedly retained a new publication operation",
            )
            return {
                "status": "observed",
                "report": report,
                "probe_semantics": "genuine-prior-base-versus-actual-main-publication-result",
                "external_actor_mutation_demonstrated": False,
            }

        remaining = (utc(self.bundle["context"]["deadline_at"]) - dt.datetime.now(dt.UTC)).total_seconds()
        setup.require(remaining > 0, "remote precondition whole-unit deadline exhausted")
        result = self.workflow.services.boundary(observe, min(wall_time_ms / 1000, remaining))
        setup.require(result.get("status") == "observed", "remote precondition read was interrupted")
        return result


class BudgetProbe:
    adapter_id = "req039-budget-exhaustion-v1"

    def __init__(self, bundle: Doc, workflow: automation_controller.NativeWorkflow) -> None:
        self.bundle, self.workflow = bundle, workflow

    def inspect(self, request: Doc) -> Doc:
        selected = self.bundle["scope_tests"]["budget"]
        setup.require(
            set(request) == {"contract", "wall_time_ms"}
            and request["contract"] == selected
            and type(request["wall_time_ms"]) is int
            and 1 <= request["wall_time_ms"] <= 300000,
            "budget proof differs from its independently declared local read",
        )
        return {
            "schema_version": "0.1.0",
            "repository": self.bundle["trust"]["repository"],
            "contract_digest": planning.digest(selected),
            "native_configuration_digest": self.workflow.configuration_digest,
            "wall_time_ms": request["wall_time_ms"],
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        self.inspect(request)
        setup.require(wall_time_ms == request["wall_time_ms"], "budget proof changed its full reservation bound")
        return self.workflow.services.boundary(
            lambda: {
                "status": "observed",
                "state_digest": state_writer.read(Path(self.bundle["components"]["state"])).digest,
            },
            wall_time_ms / 1000,
        )


def authority_preflight(root: Path, bundle: Doc, packet: Doc, authority: automation_authorization.Authorizer) -> Doc:
    request = {
        "contract": bundle["components"]["authority_preflight"],
        "fence_reference": setup.read_json(root / "fence-reference.json"),
    }
    result = authority.call_metered(
        "authority-preflight",
        request,
        operation_id=planning.digest({"packet": packet["digest"], "purpose": "authority-preflight"}),
    )
    setup.require(
        result["status"] == "completed", "fresh authority preflight could not be established within remaining capacity"
    )
    return dict(result["receipt"]["result"])


def load_runtime(
    root: Path,
) -> tuple[Doc, Doc, automation_controller.NativeWorkflow, automation_authorization.Authorizer]:
    bundle, packet, workflow, authority = runtime_objects(root)
    result = authority_preflight(root, bundle, packet, authority)
    setup.require(result["status"] == "verified", "immutable fence upload is not yet independently visible")
    return bundle, packet, workflow, authority


def advance(root: Path) -> Doc:
    bundle, _, workflow, authority = load_runtime(root)
    selected = bundle["components"]["controller"]
    return automation_controller.step(
        Path(selected["journal"]), selected["journal_id"], workflow=workflow, authority=authority
    )


def baseline_probe(root: Path, retained: Doc) -> None:
    if (root / "baseline-observation.json").exists():
        return
    bundle, packet, workflow, authority = load_runtime(root)
    selected = bundle["components"]["baseline"]
    request = workflow.baseline_request(
        retained["event"]["correlations"], run_id=selected["run_id"], attempt=selected["attempt"]
    )
    outcome = authority.call_metered(
        "baseline-admission", request, operation_id=planning.digest({"packet": packet["digest"], "baseline": selected})
    )
    setup.require(outcome["status"] == "completed", "baseline admission could not establish its approved negative case")
    observed = outcome["receipt"]["result"]
    report = observed["report"]
    setup.require(
        observed.get("negative_established") is True
        and observed.get("producer", {}).get("conclusion") == "failure"
        and observed["pending_unchanged"]
        and report.get("admission", {}).get("status") == "admitted"
        and report.get("status") != "ready",
        "the initial exact producer did not establish an authenticated unsatisfied pending goal",
    )
    setup.write_json(root / "baseline-observation.json", outcome)


def compact_accounting(value: Doc) -> Doc:
    """Retain exact immutable prefix identities; the final export carries the full decision bodies once."""
    return {key: value[key] for key in ("schema_version", "store_id", "objective", "limits", "reserved")} | {
        "decisions": [
            {"id": item["request"]["proposal"]["id"], "digest": item["digest"], "status": item["status"]}
            for item in value["decisions"]
        ]
    }


def local_snapshot(bundle: Doc, retained: Doc) -> Doc:
    selected = bundle["delegation"]
    state = state_writer.read(Path(bundle["components"]["state"]))
    assert state.content is not None
    binding_id = retained["event"]["correlations"].get("binding_id")
    return {
        "state_b64": base64.b64encode(state.content).decode(),
        "binding": executions.get(Path(bundle["components"]["ledger"]), binding_id) if binding_id else None,
        "accounting": standing_authorization.inspect(
            Path(selected["accounting"]["path"]),
            store_id=selected["accounting"]["store_id"],
            objective_id=selected["objective"]["id"],
        ),
    }


def negative_probes(root: Path, retained: Doc) -> None:
    bundle, packet, workflow, authority = load_runtime(root)
    for case in bundle["components"]["negatives"]["cases"]:
        destination = root / ("negative-" + case + ".json")
        if os.path.lexists(destination):
            continue
        request = workflow.negative_request(retained["event"]["correlations"], case=case)
        before = local_snapshot(bundle, retained)
        report = authority.call_metered(
            "negative-admission", request, operation_id=planning.digest({"packet": packet["digest"], "case": case})
        )
        after = local_snapshot(bundle, retained)
        setup.require(report["status"] == "completed", "negative admission could not establish its declared probe")
        facts = report["receipt"]["result"]
        setup.require(
            facts["negative_established"] is True
            and facts["pending_unchanged"] is True
            and before["state_b64"] == after["state_b64"]
            and before["binding"] == after["binding"],
            "declared negative admission did not preserve exact pending native state",
        )
        setup.write_json(
            destination,
            {
                "case": case,
                "packet_digest": packet["digest"],
                "host": bundle["context"]["host"],
                "request": request,
                "report": report,
                "before": {**before, "accounting": compact_accounting(before["accounting"])},
                "after": {**after, "accounting": compact_accounting(after["accounting"])},
                "facts": {"observed_at": dt.datetime.now(dt.UTC).isoformat(), **facts},
            },
        )


def remote_precondition_probe(
    root: Path,
    bundle: Doc,
    packet: Doc,
    workflow: automation_controller.NativeWorkflow,
    authority: automation_authorization.Authorizer,
    retained: Doc,
) -> Doc:
    destination = root / "negative-divergent-ref.json"
    if os.path.lexists(destination):
        return setup.read_json(destination)
    before = local_snapshot(bundle, retained)
    request = {
        "contract": bundle["scope_tests"]["remote_precondition"],
        "handoff_id": retained["event"]["correlations"]["handoff"]["handoff_id"],
    }
    report = authority.call_metered(
        "remote-precondition",
        request,
        operation_id=planning.digest({"packet": packet["digest"], "case": "divergent-ref"}),
    )
    after = local_snapshot(bundle, retained)
    setup.require(report["status"] == "completed", "actual remote precondition negative was not established")
    facts = report["receipt"]["result"]
    native = facts["report"]
    setup.require(
        native["status"] == "conflict"
        and native["observation"]["revision"] == retained["event"]["correlations"]["handoff"]["commit"]
        and native["intent"]["request"]["expected_revision"] == bundle["base"]
        and before["state_b64"] == after["state_b64"]
        and before["binding"] == after["binding"],
        "actual published ref did not reject the genuine earlier expected base without native state changes",
    )
    result = {
        "case": "divergent-ref",
        "packet_digest": packet["digest"],
        "host": bundle["context"]["host"],
        "request": request,
        "report": report,
        "before": {**before, "accounting": compact_accounting(before["accounting"])},
        "after": {**after, "accounting": compact_accounting(after["accounting"])},
        "facts": {"observed_at": dt.datetime.now(dt.UTC).isoformat(), **facts},
    }
    setup.write_json(destination, result)
    return result


def final_scope_probes(
    root: Path,
    bundle: Doc,
    packet: Doc,
    workflow: automation_controller.NativeWorkflow,
    authority: automation_authorization.Authorizer,
    retained: Doc,
) -> dict[str, Doc]:
    """Declared local capacity/expiry/revocation cases use the original account after completion."""
    results = {}
    limit = (bundle["delegation"]["limits"]["wall_time_ms"] + 299999) // 300000 + 1
    destination = root / "negative-exhausted-objective-budget.json"
    if not os.path.lexists(destination):
        before = local_snapshot(bundle, retained)
        reports = []
        for _ in range(limit + 1):
            current = local_snapshot(bundle, retained)["accounting"]
            remaining = bundle["delegation"]["limits"]["wall_time_ms"] - current["reserved"]["wall_time_ms"]
            request = {
                "contract": bundle["scope_tests"]["budget"],
                "wall_time_ms": min(remaining, 300000) if remaining else 1,
            }
            report = authority.call_metered(
                "budget-exhaustion",
                request,
                operation_id=planning.digest({"packet": packet["digest"], "case": "exhausted-objective-budget"}),
            )
            reports.append(report)
            if remaining == 0:
                setup.require(
                    report["status"] == "denied"
                    and report["reason"].startswith("Authoritative control budget exhausted:"),
                    "zero remaining budget did not establish its exact capacity denial",
                )
                break
            setup.require(report["status"] == "completed", "declared budget-drain read failed before capacity proof")
        else:
            raise ValueError("finite original-objective budget-drain bound exhausted")
        after = local_snapshot(bundle, retained)
        setup.require(
            after["accounting"]["reserved"]["wall_time_ms"] == bundle["delegation"]["limits"]["wall_time_ms"]
            and before["state_b64"] == after["state_b64"]
            and before["binding"] == after["binding"],
            "budget probe did not establish exhausted capacity with unchanged native completion",
        )
        setup.write_json(
            destination,
            {
                "case": "exhausted-objective-budget",
                "packet_digest": packet["digest"],
                "host": bundle["context"]["host"],
                "request": request,
                "report": report,
                "before": {**before, "accounting": compact_accounting(before["accounting"])},
                "after": {**after, "accounting": compact_accounting(after["accounting"])},
                "facts": {
                    "observed_at": dt.datetime.now(dt.UTC).isoformat(),
                    "reports": reports,
                    "remaining_wall_time_ms": 0,
                },
            },
        )
    results["exhausted-objective-budget"] = setup.read_json(destination)
    for case in ("expired-delegation", "revoked-delegation"):
        destination = root / ("negative-" + case + ".json")
        if not os.path.lexists(destination):
            before = local_snapshot(bundle, retained)
            selected = bundle["delegation"]
            extra = {}
            if case == "expired-delegation":
                selected = bundle["scope_tests"]["expired_delegation"]
                active = automation_authorization.Authorizer(
                    Path(selected["config_path"]),
                    approved_digest=planning.digest(selected),
                    issuer=selected["issuer"],
                    approval_id=selected["approval_id"],
                    effects=workflow.effect_registry(),
                    readers={
                        **workflow.reader_registry(),
                        "authority-preflight": AuthorityPreflight(root, bundle, packet, workflow),
                        "budget-exhaustion": BudgetProbe(bundle, workflow),
                        "remote-precondition": RemotePrecondition(bundle, workflow),
                    },
                )
                extra["expired_delegation"] = selected
            else:
                active = authority
                extra["revocation"] = automation_revocations.revoke(
                    Path(selected["revocations"]["path"]),
                    store_id=selected["revocations"]["store_id"],
                    delegation_id=selected["id"],
                    reason="Independently approved final same-objective revocation negative after completion.",
                )
            historical = [
                item["request"]["proposal"]["effect"]
                for item in before["accounting"]["decisions"]
                if item["request"]["proposal"]["effect"].get("kind") == "dispatch"
            ]
            setup.require(bool(historical), "scope-negative proof lacks the actual retained dispatch request")
            request = historical[0]["request"]
            report = active.execute(
                "dispatch", request, effect_id=planning.digest({"packet": packet["digest"], "case": case})
            )
            after = local_snapshot(bundle, retained)
            setup.require(
                report["status"] == "denied"
                and before == after
                and (
                    (case == "expired-delegation" and report["reason"] == "Standing delegation expired.")
                    or (
                        case == "revoked-delegation"
                        and report["reason"] == "Standing delegation revoked: " + extra["revocation"]["reason"]
                    )
                ),
                "expired or revoked delegation released native work or changed capacity",
            )
            setup.write_json(
                destination,
                {
                    "case": case,
                    "packet_digest": packet["digest"],
                    "host": bundle["context"]["host"],
                    "request": request,
                    "report": report,
                    "before": {**before, "accounting": compact_accounting(before["accounting"])},
                    "after": {**after, "accounting": compact_accounting(after["accounting"])},
                    "facts": {"observed_at": dt.datetime.now(dt.UTC).isoformat(), **extra},
                },
            )
        results[case] = setup.read_json(destination)
    return results


def export_acceptance(root: Path, bundle: Doc, packet: Doc, retained: Doc) -> Doc:
    # All remaining exports are checked local journals/retained bytes; no post-budget network call.
    before = {"controller": retained, **live_pilot_evidence.export_native(bundle, retained)}
    before["accounting"] = compact_accounting(before["accounting"])
    active_bundle, active_packet, workflow, authority = runtime_objects(root)
    replay = automation_controller.step(
        Path(bundle["components"]["controller"]["journal"]),
        bundle["components"]["controller"]["journal_id"],
        workflow=workflow,
        authority=authority,
    )
    after = {"controller": replay, **live_pilot_evidence.export_native(bundle, replay)}
    after["accounting"] = compact_accounting(after["accounting"])
    setup.require(before == after, "exact completed duplicate changed native effects or accounting")
    ref_probe = remote_precondition_probe(root, active_bundle, active_packet, workflow, authority, replay)
    scope = final_scope_probes(root, active_bundle, active_packet, workflow, authority, replay)
    scope["divergent-ref"] = ref_probe
    negatives = {
        case: setup.read_json(root / ("negative-" + case + ".json"))
        for case in bundle["components"]["negatives"]["cases"]
    }
    return {
        "schema_version": "0.1.0",
        "kind": "req039-live-acceptance",
        "packet": packet,
        "approval": setup.read_json(root / "approval.json"),
        "context": bundle["context"],
        "implementation": bundle["implementation"],
        "controller": retained,
        "baseline": setup.read_json(root / "baseline-observation.json"),
        "controller_crash": setup.read_json(root / "dispatch-controller-crash.json"),
        "remote_response_drop": setup.read_json(root / "publication-response-dropped.json"),
        "fence": setup.read_json(root / "fence.json"),
        "recovery": setup.read_json(root / "recovery-observations.json"),
        "replay_checks": {"before": before, "after": after},
        "negative_cases": {**negatives, **scope},
        **live_pilot_evidence.export_native(bundle, retained),
    }


def coordinate(root: Path, checkout: Path, base: str, baseline_run: int | None) -> int:
    launch = setup.read_json(root / "launch.json")
    same_host(launch)
    not_aborted(root)
    preauthority_stage(root, launch)
    guard = resource_guard.capture(
        resource_guard.PILOT_BOUNDS, expected_cgroup="/system.slice/" + launch["unit"] + ".service"
    )
    if not os.path.lexists(root / "approval.json"):
        setup.require(
            not os.path.lexists(root / "authority-construction.json"),
            "approval receipt missing after possible authority release",
        )
        history.reject_prior_authority(source(), launch["context"])
    bundle = (
        setup.read_json(root / "bootstrap.json")
        if (root / "bootstrap.json").exists()
        else setup.bootstrap(
            root,
            checkout,
            base,
            launch["context"],
            source(),
            guard,
            baseline_run=baseline_run,
            publisher=publisher_metadata(launch["context"], source()),
        )
    )
    setup.require(
        bundle["context"] == launch["context"] and bundle["resource_guard"] == guard.descriptor,
        "bootstrap belongs to another physical coordination domain",
    )
    public = root / "public"
    if not (public / "packet.json").exists():
        packet = packet_for(bundle, publisher_available=bool(os.environ.get(PUBLISH_TOKEN)))
        setup.write_json(public / "packet.json", packet)
        setup.write_json(
            public / "approval-comment.json", {"body": approval_body(packet), "packet_digest": packet["digest"]}
        )
    packet = setup.read_json(public / "packet.json")
    reader = ApprovalReader(source())
    if bundle["components"]["publisher"]["scope_verified"] is not True:
        status(
            root,
            "blocked",
            reason="publisher_credential_or_mint_scope_unavailable",
            required_environment=PUBLISH_TOKEN,
            packet_digest=packet["digest"],
        )
        return 2
    expires = min(
        utc(bundle["context"]["created_at"]) + dt.timedelta(seconds=APPROVAL_SECONDS),
        utc(bundle["context"]["deadline_at"]),
    )
    status(root, "waiting_approval", packet_digest=packet["digest"], approval_deadline=expires.isoformat())
    while not (root / "approval.json").exists():
        not_aborted(root)
        setup.require(
            dt.datetime.now(dt.UTC) < expires, "independent approval wait expired; preserve same-host journals"
        )
        discovery_remaining = (expires - dt.datetime.now(dt.UTC)).total_seconds()
        discovery = automation_controller.bounded_native_call(
            lambda: {"receipt": find_approval(reader, packet)}, min(45, discovery_remaining)
        )
        setup.require("receipt" in discovery, "independent approval discovery was interrupted within its finite bound")
        receipt = discovery["receipt"]
        setup.require(dt.datetime.now(dt.UTC) < expires, "independent approval wait expired before authority release")
        if receipt is not None:
            not_aborted(root)
            setup.write_json(root / "approval.json", receipt)
            break
        time.sleep(5)
    expected_fence = fence_document(packet, setup.read_json(root / "approval.json"))
    if not (public / history.FENCE_MEMBER).exists():
        setup.write_json(public / history.FENCE_MEMBER, expected_fence)
    status(root, "awaiting_fence", packet_digest=packet["digest"], approval_receipt=str(public / history.FENCE_MEMBER))
    fence_deadline = min(utc(bundle["context"]["deadline_at"]), dt.datetime.now(dt.UTC) + dt.timedelta(seconds=240))
    while not (root / "fence-reference.json").exists():
        not_aborted(root)
        setup.require(
            dt.datetime.now(dt.UTC) < fence_deadline, "immutable approval fence was not retained before its deadline"
        )
        time.sleep(1)
    setup.read_json(root / "fence-reference.json")
    not_aborted(root)
    while True:
        active_bundle, active_packet, _, authority = runtime_objects(root)
        observed = authority_preflight(root, active_bundle, active_packet, authority)
        if observed["status"] == "verified":
            verified_fence = observed["fence"]
            break
        setup.require(
            observed["status"] == "fence_pending" and dt.datetime.now(dt.UTC) < fence_deadline,
            "immutable fence visibility deadline exhausted",
        )
        time.sleep(2)
    not_aborted(root)
    if not os.path.lexists(root / "fence.json"):
        setup.write_json(root / "fence.json", verified_fence)
    else:
        setup.require(setup.read_json(root / "fence.json") == verified_fence, "approved immutable fence changed")
    selected = bundle["components"]["controller"]
    while dt.datetime.now(dt.UTC) < utc(bundle["context"]["deadline_at"]):
        retained = automation_controller.show(Path(selected["journal"]), selected["journal_id"])
        event = retained["event"]
        if event["status"] in {"completed", "superseded", "paused", "cancelled"}:
            if event["status"] == "completed":
                evidence = export_acceptance(root, bundle, packet, retained)
                setup.write_json(public / "acceptance-evidence.json", evidence)
            status(
                root, event["status"], phase=event["phase"], controller_head=retained["head"], reason=event["reason"]
            )
            return 0 if event["status"] == "completed" else 2
        if event["phase"] == "execute" and event["pending"] is None:
            baseline_probe(root, retained)
        if event["phase"] == "associate" and event["pending"] is None:
            negative_probes(root, retained)
        if event["phase"] == "producer" and event["poll_count"] >= setup.MAX_PRODUCER_POLLS:
            automation_controller.pause(
                Path(selected["journal"]), selected["journal_id"], reason="Approved producer-poll capacity exhausted."
            )
            continue
        status(root, "running", phase=event["phase"], controller_head=retained["head"], packet_digest=packet["digest"])
        remaining = max(1, (utc(bundle["context"]["deadline_at"]) - dt.datetime.now(dt.UTC)).total_seconds())
        with subprocess.Popen(
            [sys.executable, "-m", "ci.req039_pilot", "step", "--root", str(root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as child:
            try:
                returncode = child.wait(timeout=min(remaining, 300))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                raise ValueError(
                    "controller child whole-call deadline exhausted; preserve uncertain native journals"
                ) from None
            child_pid = child.pid
        post = automation_controller.show(Path(selected["journal"]), selected["journal_id"])
        observations_path = root / "recovery-observations.json"
        observations = setup.read_json(observations_path) if observations_path.exists() else {"controller_restarts": []}
        if (
            observations["controller_restarts"]
            and observations["controller_restarts"][-1].get("after_restart") is None
            and returncode == 0
        ):
            observations["controller_restarts"][-1]["after_restart"] = {
                "controller": post,
                "returncode": returncode,
                "pid": child_pid,
            }
            setup.write_json(observations_path, observations, replace=True)
        if returncode == -signal.SIGKILL:
            setup.require(
                event["phase"] == "dispatch"
                and not observations["controller_restarts"]
                and setup.read_json(root / "dispatch-controller-crash.json")["packet_digest"] == packet["digest"],
                "unexpected or repeated controller death; preserve uncertain native work without another restart",
            )
            observations["controller_restarts"].append(
                {
                    "returncode": returncode,
                    "pid": child_pid,
                    "host": bundle["context"]["host"],
                    "run_attempt": bundle["context"]["run_attempt"],
                    "before": retained,
                    "after_termination": post,
                    "after_restart": None,
                    "native_dispatch_receipt": setup.read_json(root / "dispatch-controller-crash.json")[
                        "native_receipt"
                    ],
                }
            )
            setup.write_json(observations_path, observations, replace=observations_path.exists())
            setup.require(
                (root / "dispatch-controller-crash.json").exists(),
                "unexpected controller process death; preserve uncertain work",
            )
            status(
                root, "recovering", reason="declared_same_unit_controller_process_crash", packet_digest=packet["digest"]
            )
        else:
            setup.require(returncode == 0, "controller process failed; preserve exact journals and uncertain effects")
        time.sleep(1)
    status(root, "blocked", reason="whole_unit_deadline", packet_digest=packet["digest"])
    return 2


def launch(root: Path, checkout: Path, base: str, baseline_run: int | None) -> int:
    setup.private_directory(root, create=True)
    setup.private_directory(root / "public", create=True)
    context = live_context(source())
    unit = "exacly-req039-" + uuid.uuid4().hex
    found = root.stat()
    metadata = {
        "schema_version": "0.1.0",
        "unit": unit,
        "context": context,
        "base": base,
        "checkout": str(checkout),
        "storage": {"path": str(root), "device": found.st_dev, "inode": found.st_ino},
    }
    setup.write_json(root / "launch.json", metadata)
    identity = (root / "launch.json").lstat()
    setup.write_json(
        root / "preauthority-stage.json",
        {
            "schema_version": "0.1.0",
            "stage": "preauthority",
            "launch_digest": planning.digest(metadata),
            "launch_device": identity.st_dev,
            "launch_inode": identity.st_ino,
        },
    )
    command = unit_command(root, checkout, base, context, unit, baseline_run)
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    setup.require(result.returncode == 0, "coordinator unit could not be started; no effects approved")
    expires = time.monotonic() + 240
    while time.monotonic() < expires:
        if (root / "public" / "packet.json").exists() and (root / "public" / "approval-comment.json").exists():
            print(
                planning.canonical(
                    {
                        "status": "packet_ready",
                        "unit": unit,
                        "packet": str(root / "public" / "packet.json"),
                        "approval_comment": str(root / "public" / "approval-comment.json"),
                    }
                )
            )
            return 0
        state = unit_state(unit)
        setup.require(
            state in {"activating", "active"}, "coordinator ended before a complete reviewable packet was retained"
        )
        time.sleep(1)
    raise ValueError("preflight packet deadline expired; preserve partial protected setup")


def unit_state(unit: str) -> str:
    setup.require(re.fullmatch(r"exacly-req039-[0-9a-f]{32}", unit), "invalid exact unit identity")
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", unit + ".service", "--property=LoadState", "--property=ActiveState"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    values = dict(line.split("=", 1) for line in result.stdout.decode("ascii", "replace").splitlines() if "=" in line)
    if values.get("LoadState") == "not-found" and values.get("ActiveState") == "inactive":
        return "inactive"
    if result.returncode == 0 and values.get("ActiveState") in {
        "activating",
        "active",
        "deactivating",
        "inactive",
        "failed",
    }:
        return values["ActiveState"]
    return "unavailable"


def wait(root: Path) -> int:
    metadata = setup.read_json(root / "launch.json")
    same_host(metadata)
    while dt.datetime.now(dt.UTC) <= utc(metadata["context"]["deadline_at"]) + dt.timedelta(seconds=30):
        if (root / "public" / "status.json").exists():
            current = setup.read_json(root / "public" / "status.json")
            if current["status"] in {"completed", "superseded", "paused", "cancelled", "blocked", "failed", "stopped"}:
                print(planning.canonical(current))
                return 0 if current["status"] == "completed" else 2
        setup.require(
            unit_state(metadata["unit"]) in {"activating", "active"},
            "same-runner service ended; outcomes may remain uncertain",
        )
        time.sleep(5)
    raise ValueError("coordinator wait deadline exhausted; no new-runner recovery is supported")


def wait_approval(root: Path) -> int:
    metadata = setup.read_json(root / "launch.json")
    same_host(metadata)
    while dt.datetime.now(dt.UTC) <= utc(metadata["context"]["deadline_at"]):
        if (root / "public" / "status.json").exists():
            current = setup.read_json(root / "public" / "status.json")
            if current["status"] == "awaiting_fence":
                setup.require(
                    (root / "public" / history.FENCE_MEMBER).exists(), "approval receipt was not durably retained"
                )
                print(planning.canonical(current))
                return 0
            if current["status"] in {"blocked", "failed", "stopped"}:
                return 2
        setup.require(unit_state(metadata["unit"]) in {"activating", "active"}, "same-runner approval service ended")
        time.sleep(2)
    raise ValueError("same-runner approval deadline exhausted")


def reference_fence(root: Path, identity: int | None, digest: str | None) -> int:
    metadata = setup.read_json(root / "launch.json")
    same_host(metadata)
    not_aborted(root)
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = "sha256:" + digest
    setup.require(
        type(identity) is int
        and identity > 0
        and isinstance(digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
        "only an immutable artifact ID/digest reference may be supplied",
    )
    reference = {"artifact_id": identity, "digest": digest}
    path = root / "fence-reference.json"
    if path.exists():
        setup.require(setup.read_json(path) == reference, "conflicting approval-fence reference")
    else:
        setup.write_json(path, reference)
    return 0


def stop(root: Path) -> int:
    metadata = setup.read_json(root / "launch.json")
    same_host(metadata)
    observed = unit_state(metadata["unit"])
    setup.require(observed != "unavailable", "unit state is unavailable; no terminal no-authority proof is permitted")
    if observed in {"activating", "active", "deactivating"}:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", metadata["unit"] + ".service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        setup.require(result.returncode == 0, "coordinator stop unavailable; finite unit deadline remains enforced")
    setup.require(
        unit_state(metadata["unit"]) in {"inactive", "failed"}, "whole unit stop is not independently confirmed"
    )
    terminal_noauthority(root)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("launch", "coordinate", "step", "advance", "wait-approval", "wait", "stop")
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--baseline-run", type=int)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--artifact-digest")
    args = parser.parse_args(argv)
    try:
        if args.operation in {"launch", "coordinate"}:
            setup.require(isinstance(args.base, str), "explicit full base revision is required")
            if args.operation == "launch":
                return launch(args.root, args.checkout, args.base, args.baseline_run)
            return coordinate(args.root, args.checkout, args.base, args.baseline_run)
        if args.operation == "step":
            advance(args.root)
            return 0
        if args.operation == "advance":
            return reference_fence(args.root, args.artifact_id, args.artifact_digest)
        if args.operation == "wait-approval":
            return wait_approval(args.root)
        return wait(args.root) if args.operation == "wait" else stop(args.root)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as error:
        # Never render arbitrary exception/HTTP response text containing private values.
        if args.operation == "coordinate" and (args.root / "public").is_dir():
            status(
                args.root,
                "failed",
                reason="protected_preflight_or_native_operation_blocked",
                error_type=type(error).__name__,
            )
        print(
            planning.canonical({"status": "blocked", "operation": args.operation, "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
