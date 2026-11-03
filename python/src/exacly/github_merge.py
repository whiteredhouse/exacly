"""Concrete bounded GitHub updateRefs adapter for separately authorized exact PR merges.

The fixed API endpoint and query text are installed code. JSON data cannot load a provider,
choose a credential helper, bypass protection or turn an uncertain write into another request.
The bootstrap token/App description is independently approved, not an API attestation of issuer.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPException
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from exacly import automatic_merge as merge
from exacly import delivery_automation as delivery
from exacly import delivery_key
from exacly import execution_store as store
from exacly.github_evidence import NoRedirect, json_object, no_constant, read_bounded

Doc = dict[str, Any]
API = "https://api.github.com"
READ_QUERY = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
id databaseId pullRequest(number:$number){reviewDecision mergeStateStatus mergeable isMergeQueueEnabled
isInMergeQueue isCrossRepository headRefOid baseRefOid potentialMergeCommit{oid}}}}"""
UNSTABLE_QUERY = """query($owner:String!,$name:String!,$number:Int!,$base:String!){repository(owner:$owner,name:$name){
id databaseId nameWithOwner ref(qualifiedName:$base){prefix name target{oid} branchProtectionRule{
id pattern requiresDeployments requiredDeploymentEnvironments requiresConversationResolution restrictsPushes}}
pullRequest(number:$number){number headRefOid baseRefOid potentialMergeCommit{oid}
reviewThreads(first:100){totalCount pageInfo{hasNextPage} nodes{id isResolved}}}}}"""
MERGE_RECEIPT_QUERY = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
id databaseId pullRequest(number:$number){number state isDraft merged headRefName baseRefName
headRefOid baseRefOid headRepository{databaseId} mergeCommit{oid}}}}"""
UPDATE_QUERY = "mutation($input:UpdateRefsInput!){updateRefs(input:$input){clientMutationId}}"


class _InspectionStage(StrEnum):
    UNKNOWN = "unknown"
    KEY = "key"
    CREDENTIAL = "credential"
    REPOSITORY = "repository"
    INSTALLATION_SCOPE = "installation_scope"
    PROTECTION = "protection"
    SIGNATURES = "signatures"
    RULES = "rules"
    PULL_REQUEST = "pull_request"
    REVIEW = "review"
    UNSTABLE_CAPABILITY = "unstable_capability"
    LEGACY_HEAD_STATUS = "legacy_head_status"
    LEGACY_MERGE_STATUS = "legacy_merge_status"
    LEGACY_STATUS_FACTS = "legacy_status_facts"
    BRANCH_CONDITIONS = "branch_conditions"
    ANCESTRY = "ancestry"
    REQUIRED_CONDITION_FACTS = "required_condition_facts"
    CHECKS = "checks"
    MERGE_COMMIT = "merge_commit"
    CONTRACT_TREES = "contract_trees"
    REFERENCES = "references"
    READY_FACTS = "ready_facts"


class _ProviderFailure(StrEnum):
    HTTP = "http_error"
    TRANSPORT = "transport_error"
    RESPONSE = "invalid_response"


class ProviderUnavailable(ValueError):
    """A provider result cannot establish the requested fact; diagnostics exclude remote bodies."""

    def __init__(self, code: _ProviderFailure, *, http_status: int | None = None) -> None:
        super().__init__("GitHub delivery request is unavailable")
        self.code, self.http_status = code, http_status


@dataclass
class _InspectionProgress:
    """Per-call diagnostic context; it never holds provider content or authorizes an effect."""

    stage: _InspectionStage = _InspectionStage.KEY

    def blocked_reason(self, error: Exception) -> str:
        status = None
        if type(error) is merge.ReadinessFailure and type(error.check) is merge.ReadinessCheck:
            code = error.check.value
        elif isinstance(error, ProviderUnavailable) and isinstance(error.code, _ProviderFailure):
            code = error.code.value
            if type(error.http_status) is int and 100 <= error.http_status <= 599:
                status = error.http_status
        elif isinstance(error, store.StoreError):
            code = "deadline_or_storage"
        elif isinstance(error, OSError):
            code = "local_io_error"
        elif isinstance(error, (KeyError, TypeError, AttributeError)):
            code = "malformed_facts"
        else:
            code = "validation_failed"
        suffix = "" if status is None else f"; http_status={status}"
        stage = self.stage if isinstance(self.stage, _InspectionStage) else _InspectionStage.UNKNOWN
        return f"GitHub merge readiness blocked: stage={stage.value}; code={code}{suffix}."


def _credential_now() -> dt.datetime:
    """Fresh credentials use the real clock, never a timestamp selected by input JSON."""
    return dt.datetime.now(dt.UTC)


class _Session:
    def __init__(
        self,
        configuration: Doc,
        method: str,
        deadline: store.Deadline,
        opener: Any,
        *,
        progress: _InspectionProgress | None = None,
    ) -> None:
        self.progress = progress if progress is not None else _InspectionProgress()
        self.configuration, self.deadline, self.opener = merge.validate_configuration(configuration), deadline, opener
        self.limits = self.configuration["call_limits"][method]
        self.reads = self.writes = 0
        token = os.environ.get(self.configuration["token_environment"], "")
        merge.require(
            token
            and token.isascii()
            and not any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token),
            "selected delivery merge credential is unavailable",
        )
        merge.require(
            "sha256:" + hashlib.sha256(token.encode()).hexdigest() == self.configuration["actor"]["token_digest"],
            "delivery credential differs from independently approved mint identity",
        )
        merge.validate_credential_time(self.configuration, now=_credential_now())
        self.token = token

    def request(self, method: str, path: str, body: Doc | None = None, *, optional: bool = False) -> Any:
        self.deadline.check()
        merge.validate_credential_time(self.configuration, now=_credential_now())
        mutation = method == "POST" and path != "/graphql-read"
        if mutation:
            self.writes += 1
            merge.require(self.writes <= self.limits["write_calls"], "delivery write ceiling exhausted")
        else:
            self.reads += 1
            merge.require(self.reads <= self.limits["read_calls"], "delivery read ceiling exhausted")
        endpoint = "/graphql" if path == "/graphql-read" else path
        merge.require(
            endpoint == "/graphql"
            or endpoint.startswith("/repos/")
            or endpoint == "/installation/repositories?per_page=100",
            "unsupported installed provider path",
        )
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(API + endpoint, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.deadline.remaining()) as response:
                deadline = self.deadline

                class BoundedResponse:
                    headers = response.headers

                    def read(self, size: int) -> bytes:
                        deadline.check()
                        content = response.read(size)
                        deadline.check()
                        return cast(bytes, content)

                content = read_bounded(BoundedResponse(), 8 * 1024 * 1024)
            self.deadline.check()
            return json.loads(content, object_pairs_hook=json_object, parse_constant=no_constant)
        except HTTPError as error:
            if optional and error.code == 404:
                return None
            raise ProviderUnavailable(_ProviderFailure.HTTP, http_status=error.code) from None
        except store.StoreError:
            raise
        except (URLError, HTTPException, OSError):
            raise ProviderUnavailable(_ProviderFailure.TRANSPORT) from None
        except ValueError:
            raise ProviderUnavailable(_ProviderFailure.RESPONSE) from None


def _object(value: Any) -> Doc:
    merge.require(isinstance(value, dict), "GitHub returned an invalid delivery object")
    return cast(Doc, value)


class GitHubMergeAdapter:
    """Fixed trusted concrete adapter; constructing/validating it performs no credential or key IO."""

    def __init__(self, configuration: Doc, policy: Doc, *, opener: Any = None) -> None:
        self.configuration = merge.validate_configuration(configuration)
        self.policy = merge.validate_policy(policy)
        self._opener = build_opener(NoRedirect()) if opener is None else opener

    @property
    def adapter_id(self) -> str:
        return merge.PROFILE

    @property
    def configuration_digest(self) -> str:
        return merge.configuration_digest(self.configuration, self.policy)

    @property
    def operation_profile_digest(self) -> str:
        return merge.operation_profile_digest(self.configuration, self.policy)

    @property
    def call_limits(self) -> Doc:
        return copy.deepcopy(self.configuration["call_limits"])

    @property
    def approval_context(self) -> Doc:
        return {"policy_digest": delivery.digest(self.policy), "contract": copy.deepcopy(self.policy["contract"])}

    def validate_payload(self, request: Doc, payload: Doc) -> Doc:
        return merge.validate_payload(request, payload, self.configuration, self.policy)

    def validate_provenance(self, method: str, request: Doc, payload: Doc, provenance: Doc) -> Doc:
        return merge.validate_provenance(
            method, request, payload, provenance, configuration=self.configuration, policy=self.policy
        )

    def validate_result(self, method: str, request: Doc, payload: Doc, result: Doc) -> Doc:
        return merge.validate_result(
            method, request, payload, result, configuration=self.configuration, policy=self.policy
        )

    def _session(
        self, method: str, deadline: store.Deadline, *, progress: _InspectionProgress | None = None
    ) -> _Session:
        deadline.check()
        key = self.configuration["key"]
        delivery_key.validate_key(Path(key["path"]), key["id"], lock_timeout=deadline.remaining())
        if progress is not None:
            progress.stage = _InspectionStage.CREDENTIAL
        return _Session(self.configuration, method, deadline, self._opener, progress=progress)

    @property
    def _prefix(self) -> str:
        return "/repos/" + str(self.configuration["repository"]["full_name"])

    def _ref(self, session: _Session, name: str) -> str | None:
        session.progress.stage = _InspectionStage.REFERENCES
        value = session.request(
            "GET", self._prefix + "/git/ref/" + quote(name.removeprefix("refs/"), safe="/"), optional=True
        )
        if value is None:
            return None
        obj = _object(value)
        merge.require(obj.get("ref") == name, "GitHub reference identity changed")
        return str(_object(obj.get("object")).get("sha"))

    @staticmethod
    def _pr(value: Any) -> Doc:
        pr = _object(value)
        head, base = _object(pr.get("head")), _object(pr.get("base"))
        return {
            "number": pr.get("number"),
            "repository_id": _object(base.get("repo")).get("id"),
            "head_repository_id": _object(head.get("repo")).get("id"),
            "head_ref": "refs/heads/" + str(head.get("ref")),
            "base_ref": "refs/heads/" + str(base.get("ref")),
            "head_revision": head.get("sha"),
            "base_revision": base.get("sha"),
            "merge_revision": pr.get("merge_commit_sha"),
            "state": pr.get("state"),
            "draft": pr.get("draft"),
            "merged": pr.get("merged"),
        }

    def _merge_receipt(self, session: _Session, request: Doc) -> Doc:
        """Observe the actual merged commit; a potential merge is never a receipt."""
        session.progress.stage = _InspectionStage.PULL_REQUEST
        owner, name = self.configuration["repository"]["full_name"].split("/")
        graph = _object(
            session.request(
                "POST",
                "/graphql-read",
                {
                    "query": MERGE_RECEIPT_QUERY,
                    "variables": {"owner": owner, "name": name, "number": request["subject"]["pull_request"]},
                },
            )
        )
        merge.require(not graph.get("errors"), "GitHub actual merge receipt is unavailable")
        repository = _object(_object(graph.get("data")).get("repository"))
        merge.require(
            type(repository.get("databaseId")) is int
            and repository.get("databaseId") == self.configuration["repository"]["id"]
            and repository.get("id") == self.configuration["repository"]["node_id"],
            "GraphQL repository identity changed",
        )
        pr = _object(repository.get("pullRequest"))
        merge.require(
            type(pr.get("number")) is int and pr.get("number") == request["subject"]["pull_request"],
            "GraphQL pull-request identity changed",
        )
        state, merged, draft = pr.get("state"), pr.get("merged"), pr.get("isDraft")
        merge.require(
            state in ("OPEN", "CLOSED", "MERGED") and type(merged) is bool and type(draft) is bool,
            "GitHub actual merge receipt state is malformed",
        )
        merge.require((state == "MERGED") == merged, "GitHub actual merge receipt state is contradictory")
        merge.require(
            all(isinstance(pr.get(key), str) and pr[key] for key in ("headRefName", "baseRefName")),
            "GitHub actual merge receipt reference names are unavailable",
        )
        head_repository = _object(pr.get("headRepository"))
        commit = pr.get("mergeCommit")
        return {
            "number": pr["number"],
            "repository_id": repository["databaseId"],
            "head_repository_id": head_repository.get("databaseId"),
            "head_ref": "refs/heads/" + pr["headRefName"],
            "base_ref": "refs/heads/" + pr["baseRefName"],
            "head_revision": pr.get("headRefOid"),
            "base_revision": pr.get("baseRefOid"),
            "merge_revision": None if commit is None else _object(commit).get("oid"),
            "state": "open" if state == "OPEN" else "closed",
            "draft": draft,
            "merged": merged,
        }

    def _commit(self, session: _Session, revision: str) -> Doc:
        session.progress.stage = _InspectionStage.MERGE_COMMIT
        value = _object(session.request("GET", self._prefix + "/git/commits/" + revision))
        parents = value.get("parents")
        merge.require(isinstance(parents, list), "GitHub commit parents are unavailable")
        return {
            "sha": value.get("sha"),
            "tree": _object(value.get("tree")).get("sha"),
            "parents": [_object(parent).get("sha") for parent in cast(list[Any], parents)],
        }

    def _contracts(self, session: _Session, request: Doc, payload: Doc) -> Doc:
        session.progress.stage = _InspectionStage.CONTRACT_TREES
        trees = {}
        for phase, key in (("before", "base_revision"), ("after", "head_revision")):
            value = _object(
                session.request("GET", self._prefix + "/git/trees/" + request["subject"][key] + "?recursive=1")
            )
            entries = value.get("tree")
            merge.require(
                value.get("truncated") is False and isinstance(entries, list) and len(entries) <= 100000,
                "protected source tree is incomplete",
            )
            found = {}
            for entry in cast(list[Any], entries):
                item = _object(entry)
                path = item.get("path")
                merge.require(isinstance(path, str) and path not in found, "duplicate or invalid Git tree entry")
                found[path] = item
            trees[phase] = found
        result: Doc = {}
        for category, files in payload["contract_files"].items():
            result[category] = []
            for entry in files:
                identities: Doc = {"path": entry["path"]}
                for phase in ("before", "after"):
                    actual = trees[phase].get(entry["path"], {})
                    merge.require(
                        actual.get("type") == "blob" and actual.get("mode") in ("100644", "100755"),
                        "protected source file is missing or not ordinary Git content",
                    )
                    identities[phase] = actual.get("sha")
                result[category].append(identities)
        return result

    def _actor(self, session: _Session) -> Doc:
        session.progress.stage = _InspectionStage.REPOSITORY
        repo = _object(session.request("GET", self._prefix))
        raw_permissions = repo.get("permissions")
        if session.configuration["actor"].get("credential_kind") == merge.INSTALLATION_GRANT_KIND:
            permissions = {} if raw_permissions is None else _object(raw_permissions)
        else:
            permissions = _object(raw_permissions)
        session.progress.stage = _InspectionStage.INSTALLATION_SCOPE
        scope = _object(session.request("GET", "/installation/repositories?per_page=100"))
        repositories = scope.get("repositories")
        merge.require(
            type(scope.get("total_count")) is int
            and scope.get("total_count") == 1
            and isinstance(repositories, list)
            and len(repositories) == 1,
            "delivery merge requires a single-repository installation token",
        )
        return {
            "repository": {
                "id": repo.get("id"),
                "full_name": repo.get("full_name"),
                "node_id": repo.get("node_id"),
                **{name: permissions.get(name) for name in ("admin", "maintain", "push")},
            },
            "installation_repository_ids": [_object(entry).get("id") for entry in cast(list[Any], repositories)],
        }

    def _unstable_conditions(self, session: _Session, request: Doc, payload: Doc) -> Doc:
        session.progress.stage = _InspectionStage.UNSTABLE_CAPABILITY
        merge._validate_unstable_capability(session.configuration)
        subject = request["subject"]
        legacy: Doc = {}
        for scope, revision in (("head", subject["head_revision"]), ("merge", payload["merge_revision"])):
            session.progress.stage = (
                _InspectionStage.LEGACY_HEAD_STATUS if scope == "head" else _InspectionStage.LEGACY_MERGE_STATUS
            )
            value = _object(session.request("GET", self._prefix + "/commits/" + revision + "/status?per_page=100"))
            statuses = value.get("statuses")
            merge.require(
                value.get("sha") == revision
                and isinstance(statuses, list)
                and type(value.get("total_count")) is int
                and value["total_count"] == len(statuses) < 100,
                "legacy status inventory is incomplete or for another commit",
            )
            legacy[scope] = {
                "sha": value["sha"],
                "total_count": value["total_count"],
                "statuses": [
                    {"context": _object(item).get("context"), "state": _object(item).get("state")}
                    for item in cast(list[Any], statuses)
                ],
            }
        session.progress.stage = _InspectionStage.LEGACY_STATUS_FACTS
        merge._legacy_statuses_shape(legacy)
        owner, name = self.configuration["repository"]["full_name"].split("/")
        session.progress.stage = _InspectionStage.BRANCH_CONDITIONS
        graph = _object(
            session.request(
                "POST",
                "/graphql-read",
                {
                    "query": UNSTABLE_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "number": subject["pull_request"],
                        "base": subject["base_ref"],
                    },
                },
            )
        )
        merge.require(not graph.get("errors"), "additional required branch conditions are unavailable")
        repo = _object(_object(graph.get("data")).get("repository"))
        base = _object(repo.get("ref"))
        prefix, base_name = base.get("prefix"), base.get("name")
        merge.require(isinstance(prefix, str) and isinstance(base_name, str), "protected base reference is unavailable")
        policy = _object(base.get("branchProtectionRule"))
        pr = _object(repo.get("pullRequest"))
        threads = _object(pr.get("reviewThreads"))
        nodes = threads.get("nodes")
        merge.require(isinstance(nodes, list), "review thread inventory is unavailable")
        session.progress.stage = _InspectionStage.ANCESTRY
        comparison = _object(
            session.request(
                "GET",
                self._prefix
                + "/compare/"
                + subject["base_revision"]
                + "..."
                + subject["head_revision"]
                + "?per_page=100",
            )
        )
        commits = comparison.get("commits")
        merge.require(
            isinstance(commits, list)
            and type(comparison.get("total_commits")) is int
            and type(comparison.get("ahead_by")) is int
            and 0 < comparison["total_commits"] == comparison["ahead_by"] == len(commits) < 100,
            "head ancestry comparison is incomplete",
        )
        revisions = [_object(item).get("sha") for item in cast(list[Any], commits)]
        merge.require(
            all(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) for revision in revisions)
            and len(set(revisions)) == len(revisions)
            and revisions[-1] == subject["head_revision"],
            "head ancestry comparison identities changed",
        )
        conditions = {
            "repository": {
                "id": repo.get("databaseId"),
                "full_name": repo.get("nameWithOwner"),
                "node_id": repo.get("id"),
            },
            "base_ref": {
                "name": cast(str, prefix) + cast(str, base_name),
                "revision": _object(base.get("target")).get("oid"),
            },
            "pull_request": {
                "number": pr.get("number"),
                "head_revision": pr.get("headRefOid"),
                "base_revision": pr.get("baseRefOid"),
                "merge_revision": _object(pr.get("potentialMergeCommit")).get("oid"),
            },
            "branch_policy": {
                "id": policy.get("id"),
                "pattern": policy.get("pattern"),
                "requires_deployments": policy.get("requiresDeployments"),
                "required_deployment_environments": policy.get("requiredDeploymentEnvironments"),
                "requires_conversation_resolution": policy.get("requiresConversationResolution"),
                "restricts_pushes": policy.get("restrictsPushes"),
            },
            "review_threads": {
                "total_count": threads.get("totalCount"),
                "has_next_page": _object(threads.get("pageInfo")).get("hasNextPage"),
                "threads": [
                    {"id": _object(item).get("id"), "is_resolved": _object(item).get("isResolved")}
                    for item in cast(list[Any], nodes)
                ],
            },
            "ancestry": {
                "base_revision": _object(comparison.get("base_commit")).get("sha"),
                "head_revision": revisions[-1],
                "merge_base_revision": _object(comparison.get("merge_base_commit")).get("sha"),
                "status": comparison.get("status"),
                "ahead_by": comparison.get("ahead_by"),
                "behind_by": comparison.get("behind_by"),
            },
        }
        session.progress.stage = _InspectionStage.REQUIRED_CONDITION_FACTS
        merge._required_conditions_shape(conditions)
        return {"legacy_statuses": legacy, "required_conditions": conditions}

    def _facts(self, session: _Session, request: Doc, payload: Doc) -> Doc:
        actor = self._actor(session)
        base_name = quote(request["subject"]["base_ref"].removeprefix("refs/heads/"), safe="")
        session.progress.stage = _InspectionStage.PROTECTION
        protection = session.request("GET", self._prefix + "/branches/" + base_name + "/protection")
        protection = _object(protection)
        session.progress.stage = _InspectionStage.SIGNATURES
        protection["required_signatures"] = session.request(
            "GET", self._prefix + "/branches/" + base_name + "/protection/required_signatures"
        )
        session.progress.stage = _InspectionStage.PROTECTION
        protection = merge.protection_facts(protection)
        session.progress.stage = _InspectionStage.RULES
        rules = session.request("GET", self._prefix + "/rules/branches/" + base_name)
        merge.require(rules == [], "unsupported effective repository ruleset")
        session.progress.stage = _InspectionStage.PULL_REQUEST
        pr = self._pr(session.request("GET", self._prefix + "/pulls/" + str(request["subject"]["pull_request"])))
        owner, name = self.configuration["repository"]["full_name"].split("/")
        session.progress.stage = _InspectionStage.REVIEW
        graph = _object(
            session.request(
                "POST",
                "/graphql-read",
                {
                    "query": READ_QUERY,
                    "variables": {"owner": owner, "name": name, "number": request["subject"]["pull_request"]},
                },
            )
        )
        merge.require(not graph.get("errors"), "GitHub current review decision is unavailable")
        graph_repo = _object(_object(graph.get("data")).get("repository"))
        merge.require(
            type(graph_repo.get("databaseId")) is int
            and graph_repo.get("id") == self.configuration["repository"]["node_id"]
            and graph_repo.get("databaseId") == self.configuration["repository"]["id"],
            "GraphQL repository identity changed",
        )
        review = _object(graph_repo.get("pullRequest"))
        review = {key: review.get(key) for key in merge.REVIEW_FIELDS}
        review["potentialMergeCommit"] = {"oid": _object(review["potentialMergeCommit"]).get("oid")}
        # API 2026-03-10 omits REST merge_commit_sha. The fixed GraphQL read
        # observes the candidate; a non-null legacy observation may not disagree.
        candidate = review["potentialMergeCommit"]["oid"]
        merge.require(
            pr["merge_revision"] is None
            or (isinstance(pr["merge_revision"], str) and pr["merge_revision"] == candidate),
            "REST and GraphQL merge candidate observations differ",
        )
        pr["merge_revision"] = candidate
        extra = self._unstable_conditions(session, request, payload) if review["mergeStateStatus"] == "UNSTABLE" else {}
        session.progress.stage = _InspectionStage.CHECKS
        checks = _object(
            session.request(
                "GET", self._prefix + "/commits/" + payload["merge_revision"] + "/check-runs?filter=latest&per_page=100"
            )
        )
        runs = checks.get("check_runs")
        merge.require(
            isinstance(runs, list)
            and type(checks.get("total_count")) is int
            and checks.get("total_count") == len(runs)
            and len(runs) < 100,
            "required check inventory is incomplete",
        )
        selected_runs = []
        for item in cast(list[Any], runs):
            run = _object(item)
            selected_runs.append(
                {
                    "name": run.get("name"),
                    "app_id": _object(run.get("app")).get("id"),
                    "head_sha": run.get("head_sha"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                }
            )
        return {
            **actor,
            **extra,
            "protection": protection,
            "rules": rules,
            "pull_request": pr,
            "review": review,
            "merge_commit": self._commit(session, payload["merge_revision"]),
            "check_runs": selected_runs,
            "contract_files": self._contracts(session, request, payload),
            "refs": {
                "base": self._ref(session, request["subject"]["base_ref"]),
                "head": self._ref(session, payload["head_ref"]),
                "marker": self._ref(session, payload["marker_ref"]),
            },
        }

    def _proof(
        self, method: str, request: Doc, payload: Doc, facts: Doc, *, started: bool, marker_object: str | None = None
    ) -> Doc:
        return merge.provenance(
            method,
            request,
            payload,
            self.configuration,
            self.policy,
            facts=facts,
            write_started=started,
            marker_object=marker_object,
        )

    def inspect(self, request: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
        payload = self.validate_payload(request, payload)
        progress = _InspectionProgress()
        try:
            facts = self._facts(self._session("inspect", deadline, progress=progress), request, payload)
            progress.stage = _InspectionStage.READY_FACTS
            merge.validate_ready_facts(request, payload, self.configuration, self.policy, facts)
            return {
                "ready": True,
                "reason": "Exact protected GitHub merge is currently ready; reads are not atomic.",
                "provenance": self._proof("inspect", request, payload, facts, started=False),
            }
        except (ValueError, store.StoreError, OSError, KeyError, TypeError, AttributeError) as error:
            return {
                "ready": False,
                "reason": progress.blocked_reason(error),
                "provenance": self._proof("inspect", request, payload, {"blocked": True}, started=False),
            }

    def _applied(
        self,
        session: _Session,
        intent: Doc,
        payload: Doc,
        signed: Doc,
        *,
        method: str,
        started: bool,
        actor: Doc,
        preflight: Doc | None = None,
    ) -> Doc:
        request, subject = intent["request"], intent["request"]["subject"]
        pr = self._merge_receipt(session, request)
        marker_ref = self._ref(session, signed["ref"])
        remote_tag = session.request("GET", self._prefix + "/git/tags/" + signed["object"], optional=True)
        commit = self._commit(session, payload["merge_revision"])
        target = remote_tag.get("object") if isinstance(remote_tag, dict) else None
        matched = (
            all(type(pr.get(key)) is int for key in ("number", "repository_id", "head_repository_id"))
            and marker_ref == signed["object"]
            and isinstance(remote_tag, dict)
            and remote_tag.get("sha") == signed["object"]
            and remote_tag.get("tag") == signed["tag"]
            and remote_tag.get("message") == signed["message"]
            and isinstance(target, dict)
            and {key: target.get(key) for key in ("sha", "type")}
            == {"sha": payload["merge_revision"], "type": "commit"}
            and pr["merged"] is True
            and pr["merge_revision"] == payload["merge_revision"]
            and pr["number"] == subject["pull_request"]
            and pr["repository_id"] == subject["repository"]["id"]
            and pr["head_repository_id"] == subject["repository"]["id"]
            and pr["head_revision"] == subject["head_revision"]
            and pr["head_ref"] == payload["head_ref"]
            and pr["base_ref"] == subject["base_ref"]
            and commit
            == {
                "sha": payload["merge_revision"],
                "tree": payload["merge_tree"],
                "parents": [subject["base_revision"], subject["head_revision"]],
            }
        )
        facts = {
            "pull_request": pr,
            "marker_ref": marker_ref,
            "marker_verified": bool(matched),
            "merge_commit": commit,
            "preflight": preflight,
            "marker": {key: value for key, value in signed.items() if key != "message"},
            "actor": actor,
        }
        proof = self._proof(method, request, payload, facts, started=started, marker_object=signed["object"])
        if not matched:
            return {
                "outcome": "uncertain",
                "effect": None,
                "reason": "Exact GitHub merge and authenticated marker are not jointly established; do not resend.",
                "provenance": proof,
            }
        effect = {
            "repository": copy.deepcopy(subject["repository"]),
            "pull_request": subject["pull_request"],
            "head_revision": subject["head_revision"],
            "base_revision": subject["base_revision"],
            "method": "merge",
            "commit": payload["merge_revision"],
            "remote_receipt_digest": delivery.digest(facts),
        }
        return {
            "outcome": "applied",
            "effect": effect,
            "reason": "Exact merge and PR receipt retained; fresh verification remains separate.",
            "provenance": proof,
        }

    @staticmethod
    def _invoke_capacity(session: _Session, facts: Doc) -> None:
        if facts["review"]["mergeStateStatus"] == "UNSTABLE":
            merge.require(
                session.limits["read_calls"] >= 40,
                "approved read ceiling cannot cover the complete UNSTABLE merge protocol",
            )

    def invoke(self, intent: Doc, payload: Doc, authorization: Doc, *, deadline: store.Deadline) -> Doc:
        delivery.validate_intent(intent, approval=authorization)
        request = delivery.validate_request(intent["request"])
        payload = self.validate_payload(request, payload)
        merge.require(
            authorization["policy_digest"] == delivery.digest(self.policy)
            and authorization["contract"] == self.policy["contract"]
            and authorization["adapter"] == {"id": self.adapter_id, "configuration_digest": self.configuration_digest},
            "merge approval is not for the current native policy/configuration",
        )
        started, signed, facts = False, None, {}
        preparation_object = None
        try:
            session = self._session("invoke", deadline)
            facts = self._facts(session, request, payload)
            merge.validate_ready_facts(request, payload, self.configuration, self.policy, facts)
            self._invoke_capacity(session, facts)
            signed = merge.marker(request, payload, self.configuration, self.policy, lock_timeout=deadline.remaining())
            # A single logical stage has one bounded object-preparation request and one atomic
            # reference request. The engine retained may_have_started before releasing this call.
            self._current_authority(intent, authorization)
            started = True
            tag = _object(
                session.request(
                    "POST",
                    self._prefix + "/git/tags",
                    {
                        "tag": signed["tag"],
                        "message": signed["message"],
                        "object": signed["commit"],
                        "type": "commit",
                        "tagger": {
                            "name": "Exacly delivery",
                            "email": "delivery@exacly.invalid",
                            "date": "1970-01-01T00:00:00Z",
                        },
                    },
                )
            )
            if isinstance(tag.get("sha"), str) and re.fullmatch(r"[0-9a-f]{40}", tag["sha"]):
                preparation_object = tag["sha"]
            merge.require(tag.get("sha") == signed["object"], "prepared marker object differs from approved content")
            facts = self._facts(session, request, payload)
            merge.validate_ready_facts(request, payload, self.configuration, self.policy, facts)
            self._invoke_capacity(session, facts)
            delivery_key.validate_key(
                Path(self.configuration["key"]["path"]),
                self.configuration["key"]["id"],
                lock_timeout=deadline.remaining(),
            )
            self._current_authority(intent, authorization)
            result = _object(
                session.request(
                    "POST",
                    "/graphql",
                    {
                        "query": UPDATE_QUERY,
                        "variables": {
                            "input": {
                                "repositoryId": self.configuration["repository"]["node_id"],
                                "clientMutationId": request["operation_key"],
                                "refUpdates": merge.ref_updates(request, payload, signed),
                            }
                        },
                    },
                )
            )
            # Error classification is deliberately conservative. An undocumented GraphQL error
            # code is not proof of a server-side rejection or permission for a replacement send.
            merge.require(
                not result.get("errors")
                and _object(_object(result.get("data")).get("updateRefs")).get("clientMutationId")
                == request["operation_key"],
                "atomic merge was not acknowledged",
            )
            return self._applied(
                session,
                intent,
                payload,
                signed,
                method="invoke",
                started=True,
                preflight=facts,
                actor={name: facts[name] for name in ("repository", "installation_repository_ids")},
            )
        except (ValueError, store.StoreError, OSError, KeyError, TypeError, AttributeError):
            return {
                "outcome": "uncertain" if started else "rejected",
                "effect": None,
                "reason": (
                    "A provider write may have started; reconcile without a replacement send."
                    if started
                    else "Current merge preconditions rejected before provider writes."
                ),
                "provenance": self._proof(
                    "invoke",
                    request,
                    payload,
                    {"preflight": facts, "provider_unconfirmed": True, "preparation_object": preparation_object},
                    started=started,
                    marker_object=None if signed is None else signed["object"],
                ),
            }

    def _current_authority(self, intent: Doc, authorization: Doc) -> None:
        delivery.validate_intent(intent, approval=authorization)
        merge.require(
            authorization["adapter"] == {"id": self.adapter_id, "configuration_digest": self.configuration_digest}
            and authorization["policy_digest"] == delivery.digest(self.policy)
            and authorization["contract"] == self.policy["contract"],
            "selected native merge configuration changed before provider write",
        )
        # Fingerprinting is fallible local work too. Use the actual clock after that work,
        # immediately before dispatch; a pre-fingerprint timestamp must not extend a grant.
        now = dt.datetime.now(dt.UTC)
        merge.require(
            delivery._timestamp(authorization["not_before"]) <= now < delivery._timestamp(authorization["expires_at"]),
            "merge approval expired before provider write",
        )

    def reconcile(self, intent: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
        request = delivery.validate_request(intent["request"])
        payload = self.validate_payload(request, payload)
        signed = None
        try:
            session = self._session("reconcile", deadline)
            actor = self._actor(session)
            merge.validate_actor_facts(self.configuration, actor)
            signed = merge.marker(request, payload, self.configuration, self.policy, lock_timeout=deadline.remaining())
            return self._applied(session, intent, payload, signed, method="reconcile", started=False, actor=actor)
        except (ValueError, store.StoreError, OSError, KeyError, TypeError, AttributeError):
            return {
                "outcome": "uncertain",
                "effect": None,
                "reason": "Provider merge history cannot be established; absence never authorizes another send.",
                "provenance": self._proof(
                    "reconcile",
                    request,
                    payload,
                    {"provider_unconfirmed": True},
                    started=False,
                    marker_object=None if signed is None else signed["object"],
                ),
            }
