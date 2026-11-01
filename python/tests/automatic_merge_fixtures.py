"""Controlled GitHub HTTP contract fixtures; none of these responses establish live acceptance."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

import pytest
from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import delivery_key
from exacly import execution_store as store
from exacly.github_merge import READ_QUERY, UPDATE_QUERY, GitHubMergeAdapter
from test_delivery_contracts import approval as common_approval
from test_delivery_contracts import identify
from test_delivery_contracts import intent as common_intent

Doc = dict[str, Any]
TOKEN = "controlled-delivery-token"
TOKEN_ENVIRONMENT = "EXACLY_DELIVERY_MERGE_TOKEN"
REPOSITORY = {"id": 123, "full_name": "owner/product"}
NODE_ID = "R_controlled_repository"
BASE, HEAD, MERGE, TREE = (character * 40 for character in "abcd")
PREFIX = "/repos/owner/product"
BASE_REF, HEAD_REF = "refs/heads/main", "refs/heads/approved-change"


class Response(io.BytesIO):
    def __init__(self, value: Any, *, raw: bytes | None = None) -> None:
        encoded = json.dumps(value).encode() if raw is None else raw
        super().__init__(encoded)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(encoded))


def protection() -> Doc:
    return {
        "required_status_checks": {"strict": True, "checks": [{"context": "verify-exact-merge", "app_id": 15368}]},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "require_last_push_approval": True,
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
        **{
            key: {"enabled": value}
            for key, value in {
                "enforce_admins": True,
                "required_linear_history": False,
                "allow_force_pushes": False,
                "allow_deletions": False,
                "required_conversation_resolution": True,
                "lock_branch": False,
                "required_signatures": False,
            }.items()
        },
    }


class ScriptedGitHub:
    """Translate actual urllib requests and enforce a controlled atomic three-ref transaction."""

    def __init__(self) -> None:
        self.expected_token = TOKEN
        self.calls: list[Doc] = []
        self.overrides: dict[tuple[str, str], Any] = {}
        self.before_update: Callable[[ScriptedGitHub, Doc], None] | None = None
        self.after_update: Callable[[ScriptedGitHub], None] | None = None
        self.before_request: Callable[[ScriptedGitHub, str, str, Any], None] | None = None
        self.repository: Doc = {
            **REPOSITORY,
            "node_id": NODE_ID,
            "permissions": {"admin": False, "maintain": False, "push": True},
        }
        self.protection = protection()
        self.rules: list[Doc] = []
        self.installation_repository_ids = [REPOSITORY["id"]]
        self.pull_request: Doc = {
            "number": 42,
            "head": {"sha": HEAD, "ref": HEAD_REF.removeprefix("refs/heads/"), "repo": copy.deepcopy(REPOSITORY)},
            "base": {"sha": BASE, "ref": BASE_REF.removeprefix("refs/heads/"), "repo": copy.deepcopy(REPOSITORY)},
            "merge_commit_sha": MERGE,
            "state": "open",
            "draft": False,
            "merged": False,
        }
        self.graph: Doc = {
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "isMergeQueueEnabled": False,
            "isInMergeQueue": False,
            "isCrossRepository": False,
            "headRefOid": HEAD,
            "baseRefOid": BASE,
            "potentialMergeCommit": {"oid": MERGE},
        }
        self.commit: Doc = {"sha": MERGE, "tree": {"sha": TREE}, "parents": [{"sha": BASE}, {"sha": HEAD}]}
        self.check_runs: list[Doc] = [
            {
                "name": "verify-exact-merge",
                "app": {"id": 15368},
                "head_sha": MERGE,
                "status": "completed",
                "conclusion": "success",
            }
        ]
        entries = [
            {"path": category + ".yaml", "sha": "e" * 40, "type": "blob", "mode": "100644"}
            for category in merge.CATEGORIES
        ]
        self.trees = {BASE: copy.deepcopy(entries), HEAD: copy.deepcopy(entries)}
        self.refs: dict[str, str] = {BASE_REF: BASE, HEAD_REF: HEAD}
        self.tags: dict[str, Doc] = {}
        self.mutations: list[Doc] = []

    @property
    def writes(self) -> list[Doc]:
        return [
            call
            for call in self.calls
            if call["method"] == "POST" and not (call["path"] == "/graphql" and call["body"]["query"] == READ_QUERY)
        ]

    def _absent(self, path: str) -> None:
        raise HTTPError("https://api.github.com" + path, 404, "controlled absent object", Message(), io.BytesIO())

    def _atomic(self, body: Doc) -> Doc:
        supplied = body["variables"]["input"]
        assert supplied["repositoryId"] == NODE_ID
        if self.before_update is not None:
            self.before_update(self, supplied)
        changes = supplied["refUpdates"]
        assert len(changes) == 3 and len({change["name"] for change in changes}) == 3
        assert all(change["force"] is False for change in changes)
        allowed = (
            self.graph["reviewDecision"] == "APPROVED"
            and self.graph["mergeStateStatus"] == "CLEAN"
            and all(
                run["status"] == "completed" and run["conclusion"] in ("success", "neutral", "skipped")
                for run in self.check_runs
            )
        )
        matches = all(self.refs.get(change["name"], merge.ZERO) == change["beforeOid"] for change in changes)
        if not allowed or not matches:
            return {"errors": [{"type": "UNPROCESSABLE", "message": "controlled atomic rejection"}]}
        self.refs.update({change["name"]: change["afterOid"] for change in changes})
        self.pull_request.update(state="closed", merged=True, merge_commit_sha=MERGE)
        self.mutations.append(copy.deepcopy(supplied))
        if self.after_update is not None:
            self.after_update(self)
        return {"data": {"updateRefs": {"clientMutationId": supplied["clientMutationId"]}}}

    def respond(self, method: str, path: str, body: Any) -> Any:
        if self.before_request is not None:
            self.before_request(self, method, path, body)
        override = self.overrides.get((method, path))
        if isinstance(override, Exception):
            raise override
        if (method, path) in self.overrides:
            return copy.deepcopy(override)
        if method == "GET" and path == PREFIX:
            return copy.deepcopy(self.repository)
        if method == "GET" and path == "/installation/repositories?per_page=100":
            return {
                "total_count": len(self.installation_repository_ids),
                "repositories": [{"id": identifier} for identifier in self.installation_repository_ids],
            }
        if method == "GET" and path == PREFIX + "/branches/main/protection":
            result = copy.deepcopy(self.protection)
            result.pop("required_signatures")
            return result
        if method == "GET" and path == PREFIX + "/branches/main/protection/required_signatures":
            return copy.deepcopy(self.protection["required_signatures"])
        if method == "GET" and path == PREFIX + "/rules/branches/main":
            return copy.deepcopy(self.rules)
        if method == "GET" and path == PREFIX + "/pulls/42":
            return copy.deepcopy(self.pull_request)
        if method == "GET" and path == PREFIX + "/git/commits/" + MERGE:
            return copy.deepcopy(self.commit)
        if method == "GET" and path == PREFIX + "/commits/" + MERGE + "/check-runs?filter=latest&per_page=100":
            return {"total_count": len(self.check_runs), "check_runs": copy.deepcopy(self.check_runs)}
        if method == "GET" and path.startswith(PREFIX + "/git/trees/"):
            revision = path.removeprefix(PREFIX + "/git/trees/").split("?", 1)[0]
            if revision not in self.trees:
                self._absent(path)
            return {"truncated": False, "tree": copy.deepcopy(self.trees[revision])}
        if method == "GET" and path.startswith(PREFIX + "/git/ref/"):
            reference = "refs/" + path.removeprefix(PREFIX + "/git/ref/")
            if reference not in self.refs:
                self._absent(path)
            return {
                "ref": reference,
                "object": {"sha": self.refs[reference], "type": "tag" if "/tags/" in path else "commit"},
            }
        if method == "GET" and path.startswith(PREFIX + "/git/tags/"):
            oid = path.rsplit("/", 1)[1]
            if oid not in self.tags:
                self._absent(path)
            return copy.deepcopy(self.tags[oid])
        if method == "POST" and path == PREFIX + "/git/tags":
            assert body["type"] == "commit" and body["tagger"]["date"] == "1970-01-01T00:00:00Z"
            raw = (
                f"object {body['object']}\ntype commit\ntag {body['tag']}\n"
                f"tagger {body['tagger']['name']} <{body['tagger']['email']}> 0 +0000\n\n" + body["message"]
            ).encode()
            oid = hashlib.sha1(b"tag " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            tag = {
                "sha": oid,
                "tag": body["tag"],
                "message": body["message"],
                "object": {"sha": body["object"], "type": body["type"]},
            }
            self.tags[oid] = tag
            return copy.deepcopy(tag)
        if method == "POST" and path == "/graphql":
            assert body["query"] in (READ_QUERY, UPDATE_QUERY)
            if body["query"] == UPDATE_QUERY:
                return self._atomic(body)
            assert body["variables"] == {"owner": "owner", "name": "product", "number": 42}
            return {
                "data": {"repository": {"id": NODE_ID, "databaseId": 123, "pullRequest": copy.deepcopy(self.graph)}}
            }
        raise AssertionError((method, path))

    def open(self, request: Request, *, timeout: float) -> Response:
        assert timeout > 0
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "api.github.com" and not parsed.fragment
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        encoded = request.data
        assert encoded is None or isinstance(encoded, bytes)
        body = json.loads(encoded) if encoded is not None else None
        method = request.get_method()
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["authorization"] == "Bearer " + self.expected_token
        assert headers["x-github-api-version"] == "2026-03-10"
        self.calls.append({"method": method, "path": path, "body": copy.deepcopy(body), "headers": headers})
        return Response(self.respond(method, path, body))


@dataclass
class MergeCase:
    request: Doc
    payload: Doc
    configuration: Doc
    policy: Doc
    opener: ScriptedGitHub
    adapter: GitHubMergeAdapter
    authorization: Doc
    intent: Doc
    pin: delivery.ApprovalPin

    def rebind(self) -> None:
        self.adapter = GitHubMergeAdapter(self.configuration, self.policy, opener=self.opener)
        self.payload["operation_profile_digest"] = self.adapter.operation_profile_digest
        self.request["native_payload_digest"] = delivery.digest(self.payload)
        self.authorization = common_approval(self.request)
        self.authorization.update(
            policy_digest=delivery.digest(self.policy),
            adapter={"id": self.adapter.adapter_id, "configuration_digest": self.adapter.configuration_digest},
            contract=copy.deepcopy(self.policy["contract"]),
            limits={"operations": 3, "wall_time_ms": 120000, "reads": 128, "rollbacks": 0},
            not_before="2020-01-01T00:00:00Z",
            expires_at="2100-01-01T00:00:00Z",
        )
        self.intent = common_intent(self.request, self.authorization)
        self.intent["cost"].update(wall_time_ms=120000, reads=64)
        identify(self.intent)
        self.pin = delivery.ApprovalPin(
            self.authorization["issuer"], self.authorization["approval_id"], delivery.digest(self.authorization)
        )

    def inspect(self) -> Doc:
        return self.adapter.inspect(self.request, self.payload, deadline=store.Deadline(30))

    def invoke(self) -> Doc:
        return self.adapter.invoke(self.intent, self.payload, self.authorization, deadline=store.Deadline(30))

    def reconcile(self) -> Doc:
        return self.adapter.reconcile(self.intent, self.payload, deadline=store.Deadline(30))


def make_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MergeCase:
    directory = tmp_path / "merge-private-key"
    directory.mkdir(mode=0o700)
    path = directory / "merge.key"
    key_id = delivery_key.initialize(path)
    configuration = {
        "schema_version": merge.VERSION,
        "profile": merge.PROFILE,
        "repository": {**REPOSITORY, "node_id": NODE_ID},
        "token_environment": TOKEN_ENVIRONMENT,
        "actor": {
            "app_id": 456,
            "installation_id": 789,
            "token_digest": "sha256:" + hashlib.sha256(TOKEN.encode()).hexdigest(),
            "identity_approval_digest": delivery.digest("independently approved App/mint metadata"),
        },
        "key": {"path": str(path), "id": key_id},
        "call_limits": {
            method: {"wall_time_ms": 120000, "read_calls": 64, "write_calls": 2 if method == "invoke" else 0}
            for method in ("inspect", "invoke", "reconcile")
        },
    }
    opener = ScriptedGitHub()
    contracts = {
        category: [{"path": category + ".yaml", "before": "e" * 40, "after": "e" * 40}] for category in merge.CATEGORIES
    }
    policy = {
        "schema_version": merge.VERSION,
        "profile": merge.PROFILE,
        "enforcement": "atomic-ref-leases-and-current-server-protection",
        "policy_stability": "trusted-administrators-preserve-approved-policy-during-call",
        "server_passing_conclusions": ["success", "neutral", "skipped"],
        "base_ref": BASE_REF,
        "protection_digest": delivery.digest(merge.protection_snapshot(opener.protection)),
        "minimum_approvals": 1,
        "required_checks": [{"name": "verify-exact-merge", "app_id": 15368}],
        "contract": {category: delivery.digest(entries) for category, entries in contracts.items()},
    }
    operation_key = delivery.digest("one exact approved merge")
    payload = {
        "schema_version": merge.VERSION,
        "profile": merge.PROFILE,
        "head_ref": HEAD_REF,
        "merge_revision": MERGE,
        "merge_tree": TREE,
        "marker_ref": "refs/tags/exacly-delivery-operations/" + operation_key.removeprefix("sha256:"),
        "contract_files": contracts,
    }
    request = {
        "schema_version": delivery.VERSION,
        "profile": delivery.PROFILE,
        "kind": "delivery-request",
        "objective": {"id": delivery.digest("delivery objective"), "original_goal": delivery.digest("unchanged goal")},
        "operation_key": operation_key,
        "stage": "merge",
        "subject": {
            "repository": copy.deepcopy(REPOSITORY),
            "head_repository": copy.deepcopy(REPOSITORY),
            "pull_request": 42,
            "head_revision": HEAD,
            "base_revision": BASE,
            "base_ref": BASE_REF,
            "method": "merge",
        },
        "native_payload_digest": delivery.digest(payload),
    }
    monkeypatch.setenv(TOKEN_ENVIRONMENT, TOKEN)
    result = MergeCase(
        request,
        payload,
        configuration,
        policy,
        opener,
        GitHubMergeAdapter(configuration, policy),
        {},
        {},
        delivery.ApprovalPin("unused", "unused", delivery.digest("unused")),
    )
    result.rebind()
    return result


@pytest.fixture
def merge_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MergeCase:
    return make_case(tmp_path, monkeypatch)
