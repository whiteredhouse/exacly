"""A narrow exact GitHub merge profile; these validators release no delivery authority.

The profile uses GitHub's generated merge commit and atomic updateRefs. Ordinary PR merge APIs,
forks, merge queues, bypass actors and weaker server protections cannot implement this profile.
Protected-contract fingerprints are computed from reviewed before/after Git blob inventories.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any

from exacly import delivery_automation as delivery
from exacly import delivery_key, delivery_profile, planning

Doc = dict[str, Any]
PROFILE = "github-atomic-merge-v1"
VERSION = "0.1.0"
ZERO = "0" * 40
CATEGORIES = ("model", "criteria", "bindings", "observer")
FIELDS = planning.fields
TEXT = delivery.TEXT
SHA = delivery.SHA
DIGEST = delivery.DIGEST
POSITIVE = delivery.POSITIVE
LIMIT = FIELDS(
    {
        "wall_time_ms": {"type": "integer", "minimum": 1, "maximum": 120000},
        "read_calls": {"type": "integer", "minimum": 1, "maximum": 128},
        "write_calls": {"type": "integer", "minimum": 0, "maximum": 2},
    }
)
CONFIG_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "repository": FIELDS({"id": POSITIVE, "full_name": TEXT, "node_id": TEXT}),
        "token_environment": TEXT,
        "actor": FIELDS(
            {
                "app_id": POSITIVE,
                "installation_id": POSITIVE,
                "token_digest": DIGEST,
                "identity_approval_digest": DIGEST,
            }
        ),
        "key": FIELDS({"path": TEXT, "id": DIGEST}),
        "call_limits": FIELDS({name: LIMIT for name in ("inspect", "invoke", "reconcile")}),
    }
)
CHECK = FIELDS({"name": TEXT, "app_id": POSITIVE})
POLICY_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "enforcement": {"const": "atomic-ref-leases-and-current-server-protection"},
        "policy_stability": {"const": "trusted-administrators-preserve-approved-policy-during-call"},
        "server_passing_conclusions": {"const": ["success", "neutral", "skipped"]},
        "base_ref": TEXT,
        "protection_digest": DIGEST,
        "minimum_approvals": {"type": "integer", "minimum": 1, "maximum": 6},
        "required_checks": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True, "items": CHECK},
        "contract": delivery.CONTRACT,
    }
)
FILE = FIELDS({"path": TEXT, "before": SHA, "after": SHA})
PAYLOAD_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "head_ref": TEXT,
        "merge_revision": SHA,
        "merge_tree": SHA,
        "marker_ref": TEXT,
        "operation_profile_digest": DIGEST,
        "contract_files": FIELDS(
            {category: {"type": "array", "minItems": 1, "maxItems": 64, "items": FILE} for category in CATEGORIES}
        ),
    }
)


def require(value: Any, reason: str) -> None:
    if not value:
        raise ValueError(reason)


def branch(value: str) -> None:
    require(bool(re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value)), "invalid merge branch ref")
    require(
        not any(item in value for item in ("..", "//", "@{", ".lock")) and not value.endswith(("/", ".")),
        "unsafe merge branch ref",
    )


def validate_configuration(value: Any) -> Doc:
    config = delivery._schema(value, CONFIG_SCHEMA)
    delivery._repository(config["repository"])
    selector = config["token_environment"]
    require(selector == "EXACLY_DELIVERY_MERGE_TOKEN", "merge credentials require the fixed delivery-merge selector")
    path = Path(config["key"]["path"])
    require(
        path.is_absolute() and str(path) == config["key"]["path"] and ".." not in path.parts,
        "merge key needs a canonical absolute path",
    )
    for method, limits in config["call_limits"].items():
        require(limits["write_calls"] == (2 if method == "invoke" else 0), "merge IO ceilings differ from the profile")
        require(
            limits["read_calls"] >= (32 if method == "invoke" else 16),
            "merge profile read ceiling cannot cover its complete bounded protocol",
        )
    return config


def validate_policy(value: Any) -> Doc:
    policy = delivery._schema(value, POLICY_SCHEMA)
    branch(policy["base_ref"])
    require(
        len({entry["name"] for entry in policy["required_checks"]}) == len(policy["required_checks"]),
        "merge check contexts must be unambiguous",
    )
    return policy


def _profile(configuration: Any, policy: Any, *, operation: bool) -> Doc:
    """Fingerprint selected configuration and actual installed implementation, without credentials."""
    config, approved = validate_configuration(configuration), validate_policy(policy)
    required = (
        "automatic_merge.py",
        "github_merge.py",
        "delivery_key.py",
        "delivery_automation.py",
        "delivery_engine.py",
        "delivery_store.py",
        "delivery_runtime.py",
    )
    if operation:
        config["actor"] = {
            key: value
            for key, value in config["actor"].items()
            if key not in ("token_digest", "identity_approval_digest")
        }
    return {"configuration": config, "policy": approved, **delivery_profile.implementation(required=required)}


def configuration_digest(configuration: Any, policy: Any) -> str:
    """Pin actual selected credentials as well as the native enforcement implementation."""
    return delivery.digest(_profile(configuration, policy, operation=False))


def operation_profile_digest(configuration: Any, policy: Any) -> str:
    """Keep exact operation semantics stable across separately approved read-only token rotation."""
    return delivery.digest(_profile(configuration, policy, operation=True))


def validate_payload(request: Any, payload: Any, configuration: Any, policy: Any) -> Doc:
    request = delivery.validate_request(request)
    config, approved = validate_configuration(configuration), validate_policy(policy)
    native = delivery._schema(payload, PAYLOAD_SCHEMA)
    require(
        native["operation_profile_digest"] == operation_profile_digest(config, approved),
        "native merge operation profile changed",
    )
    require(request["stage"] == "merge", "verification and other delivery stages cannot authorize merge")
    subject = request["subject"]
    repository = {key: config["repository"][key] for key in ("id", "full_name")}
    require(subject["repository"] == subject["head_repository"] == repository, "fork or changed merge repository")
    require(subject["method"] == "merge", "this exact merge profile excludes squash and rebase")
    require(subject["base_ref"] == approved["base_ref"], "merge destination differs from approved policy")
    branch(native["head_ref"])
    require(native["head_ref"] != subject["base_ref"], "merge head and base must be different refs")
    require(
        native["merge_revision"] not in (subject["base_revision"], subject["head_revision"]),
        "a separate provider-generated merge commit is required",
    )
    expected_marker = "refs/tags/exacly-delivery-operations/" + request["operation_key"].removeprefix("sha256:")
    require(native["marker_ref"] == expected_marker, "merge marker must identify exactly this delivery operation")
    for category, entries in native["contract_files"].items():
        paths = []
        for entry in entries:
            path = PurePosixPath(entry["path"])
            require(
                not path.is_absolute()
                and str(path) == entry["path"]
                and ".." not in path.parts
                and "\\" not in entry["path"]
                and entry["path"] not in (".", ""),
                "unsafe protected Git path",
            )
            paths.append(entry["path"])
        require(len(paths) == len(set(paths)), "duplicate protected contract path")
        expected = delivery.digest(sorted(entries, key=lambda entry: entry["path"]))
        require(
            expected == approved["contract"][category], "protected contract inventory is not independently approved"
        )
    require(delivery.digest(native) == request["native_payload_digest"], "merge native payload digest changed")
    return native


def marker(request: Doc, payload: Doc, configuration: Doc, policy: Doc, *, lock_timeout: float) -> Doc:
    """Derive a distinct signed tag without a provider write or a circular approval digest."""
    native = validate_payload(request, payload, configuration, policy)
    subject, key = request["subject"], configuration["key"]
    scope = {
        "schema_version": VERSION,
        "stage": "merge",
        "key_id": key["id"],
        "operation_key": request["operation_key"],
        "request_digest": delivery.digest(request),
        "native_payload_digest": delivery.digest(native),
        "operation_profile_digest": native["operation_profile_digest"],
        "policy_digest": delivery.digest(policy),
        "repository_id": subject["repository"]["id"],
        "base_revision": subject["base_revision"],
        "head_revision": subject["head_revision"],
        "head_ref": native["head_ref"],
        "base_ref": subject["base_ref"],
        "merge_revision": native["merge_revision"],
        "merge_tree": native["merge_tree"],
        "marker_ref": native["marker_ref"],
    }
    scope["proof"] = delivery_key.sign(scope, Path(key["path"]), key["id"], lock_timeout=lock_timeout)
    message = planning.canonical(scope) + "\n"
    tag = native["marker_ref"].removeprefix("refs/tags/")
    content = (
        f"object {native['merge_revision']}\ntype commit\ntag {tag}\n"
        "tagger Exacly delivery <delivery@exacly.invalid> 0 +0000\n\n" + message
    ).encode()
    oid = hashlib.sha1(b"tag " + str(len(content)).encode() + b"\0" + content).hexdigest()
    return {
        "ref": native["marker_ref"],
        "object": oid,
        "message": message,
        "scope": scope,
        "tag": tag,
        "commit": native["merge_revision"],
    }


def ref_updates(request: Doc, payload: Doc, signed_marker: Doc) -> list[Doc]:
    subject = request["subject"]
    return [
        {
            "name": subject["base_ref"],
            "beforeOid": subject["base_revision"],
            "afterOid": payload["merge_revision"],
            "force": False,
        },
        {
            "name": payload["head_ref"],
            "beforeOid": subject["head_revision"],
            "afterOid": subject["head_revision"],
            "force": False,
        },
        {"name": signed_marker["ref"], "beforeOid": ZERO, "afterOid": signed_marker["object"], "force": False},
    ]


def protection_snapshot(value: Any) -> Doc:
    """Normalize only documented enforcement fields; unknown rulesets are rejected separately."""
    require(isinstance(value, dict), "unavailable server branch protection")
    checks, reviews = value.get("required_status_checks"), value.get("required_pull_request_reviews")
    require(isinstance(checks, dict) and isinstance(reviews, dict), "required server checks/reviews are missing")
    allowances = reviews.get("bypass_pull_request_allowances", {})
    require(
        isinstance(allowances, dict)
        and set(allowances) <= {"users", "teams", "apps"}
        and all(
            isinstance(allowances.get(key, []), list) and not allowances.get(key, [])
            for key in ("users", "teams", "apps")
        ),
        "review bypass actors are unsupported",
    )
    result = {
        "checks": checks.get("checks"),
        "strict": checks.get("strict"),
        "approvals": reviews.get("required_approving_review_count"),
        "last_push_approval": reviews.get("require_last_push_approval"),
        "dismiss_stale_reviews": reviews.get("dismiss_stale_reviews"),
        "code_owners": reviews.get("require_code_owner_reviews"),
    }
    require(
        type(result["dismiss_stale_reviews"]) is bool and type(result["code_owners"]) is bool,
        "unknown required-review protection capability",
    )
    require(
        isinstance(result["checks"], list)
        and bool(result["checks"])
        and all(
            isinstance(item, dict)
            and isinstance(item.get("context"), str)
            and item["context"].strip()
            and type(item.get("app_id")) is int
            and item["app_id"] > 0
            for item in result["checks"]
        ),
        "server checks require explicit known App identities",
    )
    result["checks"] = [{key: item[key] for key in ("context", "app_id")} for item in result["checks"]]
    for key in (
        "enforce_admins",
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "required_conversation_resolution",
        "lock_branch",
        "required_signatures",
    ):
        selected = value.get(key)
        require(
            isinstance(selected, dict) and type(selected.get("enabled")) is bool, "unknown server protection capability"
        )
        result[key] = selected["enabled"]
    require(
        result["enforce_admins"] is True and result["strict"] is True and result["last_push_approval"] is True,
        "server does not enforce current independent reviews and strict checks",
    )
    require(
        all(
            result[key] is False
            for key in (
                "required_linear_history",
                "allow_force_pushes",
                "allow_deletions",
                "lock_branch",
                "required_signatures",
            )
        ),
        "unsupported server protection",
    )
    return result


def protection_facts(value: Any) -> Doc:
    """Retain the selected enforcement fields, never a raw provider protection response."""
    snapshot = protection_snapshot(value)
    return {
        "required_status_checks": {"strict": snapshot["strict"], "checks": snapshot["checks"]},
        "required_pull_request_reviews": {
            "required_approving_review_count": snapshot["approvals"],
            "require_last_push_approval": snapshot["last_push_approval"],
            "dismiss_stale_reviews": snapshot["dismiss_stale_reviews"],
            "require_code_owner_reviews": snapshot["code_owners"],
            "bypass_pull_request_allowances": {key: [] for key in ("users", "teams", "apps")},
        },
        **{
            key: {"enabled": value}
            for key, value in snapshot.items()
            if key
            not in ("checks", "strict", "approvals", "last_push_approval", "dismiss_stale_reviews", "code_owners")
        },
    }


PR_FIELDS = {
    "number",
    "repository_id",
    "head_repository_id",
    "head_ref",
    "base_ref",
    "head_revision",
    "base_revision",
    "merge_revision",
    "state",
    "draft",
    "merged",
}
REVIEW_FIELDS = {
    "reviewDecision",
    "mergeStateStatus",
    "mergeable",
    "isMergeQueueEnabled",
    "isInMergeQueue",
    "isCrossRepository",
    "headRefOid",
    "baseRefOid",
    "potentialMergeCommit",
}


def _ready_shape(facts: Any) -> None:
    require(
        isinstance(facts, dict)
        and set(facts)
        == {
            "repository",
            "installation_repository_ids",
            "protection",
            "rules",
            "pull_request",
            "review",
            "merge_commit",
            "check_runs",
            "contract_files",
            "refs",
        },
        "unexpected retained provider preflight fields",
    )
    require(
        isinstance(facts["repository"], dict)
        and set(facts["repository"]) == {"id", "full_name", "node_id", "admin", "maintain", "push"},
        "unallowlisted retained repository fields",
    )
    require(
        isinstance(facts["merge_commit"], dict)
        and set(facts["merge_commit"]) == {"sha", "tree", "parents"}
        and isinstance(facts["refs"], dict)
        and set(facts["refs"]) == {"base", "head", "marker"},
        "unallowlisted retained Git fields",
    )
    inventory = facts["contract_files"]
    require(
        isinstance(inventory, dict)
        and set(inventory) == set(CATEGORIES)
        and all(
            isinstance(entries, list)
            and all(isinstance(item, dict) and set(item) == {"path", "before", "after"} for item in entries)
            for entries in inventory.values()
        ),
        "unallowlisted retained protected source fields",
    )
    require(
        facts["protection"] == protection_facts(facts["protection"]), "unallowlisted retained server protection fields"
    )
    review = facts["review"]
    require(
        isinstance(review, dict)
        and set(review) == REVIEW_FIELDS
        and isinstance(review["potentialMergeCommit"], dict)
        and set(review["potentialMergeCommit"]) == {"oid"},
        "unallowlisted retained review fields",
    )
    require(
        isinstance(facts["pull_request"], dict) and set(facts["pull_request"]) == PR_FIELDS,
        "unallowlisted retained PR fields",
    )
    require(
        isinstance(facts["check_runs"], list)
        and all(
            isinstance(item, dict) and set(item) == {"name", "app_id", "head_sha", "status", "conclusion"}
            for item in facts["check_runs"]
        ),
        "unallowlisted retained check fields",
    )
    require(facts["rules"] == [], "unsupported retained effective rules")


def validate_actor_facts(configuration: Doc, facts: Any) -> None:
    """Require the actual current token's narrow repository role and installation scope."""
    require(isinstance(facts, dict), "merge actor facts are missing")
    config = configuration
    repo = facts.get("repository", {})
    require(
        isinstance(repo, dict)
        and type(repo.get("id")) is int
        and all(type(repo.get(key)) is bool for key in ("admin", "maintain", "push")),
        "invalid provider repository identity or permissions",
    )
    require(
        repo
        == {
            "id": config["repository"]["id"],
            "full_name": config["repository"]["full_name"],
            "node_id": config["repository"]["node_id"],
            "admin": False,
            "maintain": False,
            "push": True,
        },
        "unknown, broad or bypass-capable merge actor",
    )
    installation_ids = facts.get("installation_repository_ids")
    require(
        isinstance(installation_ids, list)
        and all(type(item) is int for item in installation_ids)
        and installation_ids == [repo["id"]],
        "merge token is not single-repository scoped",
    )


def validate_ready_facts(request: Doc, payload: Doc, configuration: Doc, policy: Doc, facts: Any) -> None:
    """Check bounded provider facts; this does not make their reads an atomic snapshot."""
    require(isinstance(facts, dict), "merge provider facts are missing")
    _ready_shape(facts)
    validate_actor_facts(configuration, facts)
    subject, repo = request["subject"], facts["repository"]
    protection = protection_snapshot(facts.get("protection"))
    require(delivery.digest(protection) == policy["protection_digest"], "server protection changed")
    require(facts.get("rules") == [], "unsupported effective repository ruleset")
    require(
        type(protection["approvals"]) is int and protection["approvals"] >= policy["minimum_approvals"],
        "server requires fewer approvals than approved delivery policy",
    )
    protected_checks = protection["checks"]
    require(
        isinstance(protected_checks, list)
        and all(
            check in protected_checks
            for check in [{"context": entry["name"], "app_id": entry["app_id"]} for entry in policy["required_checks"]]
        ),
        "server check policy is weaker than the selected delivery policy",
    )
    pr = facts.get("pull_request", {})
    require(
        isinstance(pr, dict)
        and all(type(pr.get(key)) is int for key in ("number", "repository_id", "head_repository_id")),
        "invalid provider PR identity",
    )
    require(
        pr.get("number") == subject["pull_request"]
        and pr.get("repository_id") == repo["id"]
        and pr.get("head_repository_id") == repo["id"]
        and pr.get("head_ref") == payload["head_ref"]
        and pr.get("base_ref") == subject["base_ref"]
        and pr.get("head_revision") == subject["head_revision"]
        and pr.get("base_revision") == subject["base_revision"]
        and pr.get("merge_revision") == payload["merge_revision"]
        and pr.get("state") == "open"
        and pr.get("draft") is False
        and pr.get("merged") is False,
        "PR identity, head, base or state changed",
    )
    graph = facts.get("review", {})
    require(
        isinstance(graph, dict) and isinstance(graph.get("potentialMergeCommit"), dict),
        "current GitHub mergeability facts are malformed",
    )
    require(
        graph.get("reviewDecision") == "APPROVED"
        and graph.get("mergeStateStatus") == "CLEAN"
        and graph.get("mergeable") == "MERGEABLE"
        and graph.get("isMergeQueueEnabled") is False
        and graph.get("isInMergeQueue") is False
        and graph.get("isCrossRepository") is False
        and graph.get("headRefOid") == subject["head_revision"]
        and graph.get("baseRefOid") == subject["base_revision"]
        and graph.get("potentialMergeCommit", {}).get("oid") == payload["merge_revision"],
        "current independent reviews, checks or supported merge path are missing",
    )
    commit = facts.get("merge_commit", {})
    require(
        commit
        == {
            "sha": payload["merge_revision"],
            "tree": payload["merge_tree"],
            "parents": [subject["base_revision"], subject["head_revision"]],
        },
        "provider-generated merge parents or tree changed",
    )
    require(
        facts.get("refs") == {"base": subject["base_revision"], "head": subject["head_revision"], "marker": None},
        "merge refs or operation marker changed",
    )
    runs = facts.get("check_runs")
    require(
        isinstance(runs, list) and all(isinstance(entry, dict) and type(entry.get("app_id")) is int for entry in runs),
        "required check runs are unavailable",
    )
    for expected in policy["required_checks"]:
        selected = [
            entry
            for entry in runs
            if entry.get("name") == expected["name"] and entry.get("app_id") == expected["app_id"]
        ]
        require(
            len(selected) == 1
            and selected[0].get("head_sha") == payload["merge_revision"]
            and selected[0].get("status") == "completed"
            and selected[0].get("conclusion") == "success",
            "required current merge check is missing, ambiguous or unsuccessful",
        )
    require(
        facts.get("contract_files") == payload["contract_files"],
        "protected criteria/bindings/observer Git facts changed",
    )


def provenance(
    method: str,
    request: Doc,
    payload: Doc,
    configuration: Doc,
    policy: Doc,
    *,
    facts: Doc,
    write_started: bool,
    marker_object: str | None = None,
) -> Doc:
    return {
        "schema_version": VERSION,
        "adapter_id": PROFILE,
        "method": method,
        "request_digest": delivery.digest(request),
        "payload_digest": delivery.digest(payload),
        "configuration_digest": configuration_digest(configuration, policy),
        "operation_profile_digest": payload["operation_profile_digest"],
        "policy_digest": delivery.digest(policy),
        "write_started": write_started,
        "marker_object": marker_object,
        "facts": facts,
    }


def validate_provenance(method: str, request: Doc, payload: Doc, value: Any, *, configuration: Doc, policy: Doc) -> Doc:
    native = validate_payload(request, payload, configuration, policy)
    selected = delivery._document(value)
    expected = provenance(
        method,
        request,
        native,
        configuration,
        policy,
        facts=selected.get("facts", {}),
        write_started=selected.get("write_started", False),
        marker_object=selected.get("marker_object"),
    )
    require(selected == expected and method in ("inspect", "invoke", "reconcile"), "merge provenance identity changed")
    require(type(selected["write_started"]) is bool and isinstance(selected["facts"], dict), "invalid merge provenance")
    if method != "invoke":
        require(selected["write_started"] is False, "read-only delivery method claims mutation")
    marker_object = selected["marker_object"]
    require(
        marker_object is None or (isinstance(marker_object, str) and re.fullmatch(r"[0-9a-f]{40}", marker_object)),
        "invalid merge marker object",
    )
    facts = selected["facts"]
    if "repository" in facts:
        validate_ready_facts(request, native, configuration, policy, facts)
    elif facts.get("marker_verified") is True:
        _validate_observed(request, native, configuration, policy, facts, marker_object)
    elif facts.get("blocked") is True:
        require(facts == {"blocked": True} and method == "inspect", "invalid blocked inspection provenance")
    elif facts.get("provider_unconfirmed") is True:
        require(
            set(facts) <= {"preflight", "provider_unconfirmed", "preparation_object"}, "unexpected uncertain provenance"
        )
        if facts.get("preflight"):
            _ready_shape(facts["preflight"])
        require(isinstance(facts.get("preflight", {}), dict), "malformed unavailable preflight facts")
        observed = facts.get("preparation_object")
        require(
            observed is None or (isinstance(observed, str) and re.fullmatch(r"[0-9a-f]{40}", observed)),
            "malformed observed marker preparation identity",
        )
    elif facts.get("marker_verified") is False:
        require(method in ("invoke", "reconcile") and marker_object is not None, "invalid uncertain marker discovery")
        require(
            set(facts)
            == {"pull_request", "marker_ref", "marker_verified", "merge_commit", "preflight", "marker", "actor"}
            and isinstance(facts["pull_request"], dict)
            and set(facts["pull_request"]) == PR_FIELDS,
            "unallowlisted uncertain marker discovery fields",
        )
        validate_actor_facts(configuration, facts["actor"])
        require(
            set(facts["actor"]) == {"repository", "installation_repository_ids"},
            "unallowlisted discovered actor fields",
        )
        if facts["preflight"] is not None:
            _ready_shape(facts["preflight"])
    else:
        raise ValueError("unsupported native merge provenance")
    return selected


def _validate_observed(
    request: Doc, native: Doc, configuration: Doc, policy: Doc, facts: Doc, marker_object: str | None
) -> None:
    require(
        set(facts) == {"pull_request", "marker_ref", "marker_verified", "merge_commit", "preflight", "marker", "actor"},
        "unexpected merge receipt provenance",
    )
    validate_actor_facts(configuration, facts["actor"])
    require(
        set(facts["actor"]) == {"repository", "installation_repository_ids"}, "unallowlisted discovered actor fields"
    )
    marker_value = facts["marker"]
    require(
        isinstance(marker_value, dict) and set(marker_value) == {"ref", "object", "scope", "tag", "commit"},
        "malformed retained merge marker",
    )
    require(
        marker_value["ref"] == native["marker_ref"]
        and marker_value["object"] == marker_object == facts["marker_ref"]
        and marker_value["commit"] == native["merge_revision"]
        and marker_value["tag"] == native["marker_ref"].removeprefix("refs/tags/"),
        "retained marker identity changed",
    )
    scope = marker_value["scope"]
    require(
        isinstance(scope, dict)
        and set(scope)
        == {
            "schema_version",
            "stage",
            "key_id",
            "operation_key",
            "request_digest",
            "native_payload_digest",
            "operation_profile_digest",
            "policy_digest",
            "repository_id",
            "base_revision",
            "head_revision",
            "head_ref",
            "base_ref",
            "merge_revision",
            "merge_tree",
            "marker_ref",
            "proof",
        }
        and scope.get("schema_version") == VERSION
        and scope.get("operation_key") == request["operation_key"]
        and scope.get("request_digest") == delivery.digest(request)
        and scope.get("native_payload_digest") == delivery.digest(native)
        and scope.get("operation_profile_digest") == native["operation_profile_digest"]
        and scope.get("policy_digest") == delivery.digest(policy)
        and scope.get("repository_id") == request["subject"]["repository"]["id"]
        and scope.get("base_revision") == request["subject"]["base_revision"]
        and scope.get("head_revision") == request["subject"]["head_revision"]
        and scope.get("base_ref") == request["subject"]["base_ref"]
        and all(scope.get(key) == native[key] for key in ("head_ref", "merge_revision", "merge_tree", "marker_ref"))
        and scope.get("key_id") == configuration["key"]["id"]
        and scope.get("stage") == "merge"
        and isinstance(scope.get("proof"), str)
        and re.fullmatch(r"[0-9a-f]{64}", scope["proof"]),
        "retained merge marker scope changed",
    )
    content = (
        f"object {native['merge_revision']}\ntype commit\ntag {marker_value['tag']}\n"
        "tagger Exacly delivery <delivery@exacly.invalid> 0 +0000\n\n" + planning.canonical(scope) + "\n"
    ).encode()
    require(
        hashlib.sha1(b"tag " + str(len(content)).encode() + b"\0" + content).hexdigest() == marker_object,
        "retained merge marker bytes changed",
    )
    subject = request["subject"]
    require(
        facts["merge_commit"]
        == {
            "sha": native["merge_revision"],
            "tree": native["merge_tree"],
            "parents": [subject["base_revision"], subject["head_revision"]],
        },
        "retained merged commit differs from approval",
    )
    pr = facts["pull_request"]
    require(
        isinstance(pr, dict)
        and all(type(pr.get(key)) is int for key in ("number", "repository_id", "head_repository_id"))
        and pr.get("merged") is True
        and pr.get("merge_revision") == native["merge_revision"]
        and pr.get("number") == subject["pull_request"]
        and pr.get("head_revision") == subject["head_revision"]
        and pr.get("repository_id") == subject["repository"]["id"]
        and pr.get("head_repository_id") == subject["repository"]["id"]
        and pr.get("head_ref") == native["head_ref"]
        and pr.get("base_ref") == subject["base_ref"],
        "retained PR merged receipt differs from approved subject",
    )
    if facts["preflight"] is not None:
        validate_ready_facts(request, native, configuration, policy, facts["preflight"])


def validate_result(method: str, request: Doc, payload: Doc, value: Any, *, configuration: Doc, policy: Doc) -> Doc:
    """Tie readiness and the actual effect back to native proof, not merely its digest."""
    result = delivery._document(value)
    expected_keys = (
        {"ready", "reason", "provenance"} if method == "inspect" else {"outcome", "effect", "reason", "provenance"}
    )
    require(set(result) == expected_keys and isinstance(result["reason"], str), "invalid native merge result")
    proof = validate_provenance(
        method, request, payload, result["provenance"], configuration=configuration, policy=policy
    )
    facts = proof["facts"]
    if method == "inspect":
        require(type(result["ready"]) is bool, "invalid native merge readiness")
        if result["ready"]:
            validate_ready_facts(request, payload, configuration, policy, facts)
        else:
            require(facts == {"blocked": True}, "blocked inspection must not claim a ready snapshot")
    else:
        require(result["outcome"] in ("applied", "rejected", "uncertain"), "invalid native merge outcome")
        if result["outcome"] == "applied":
            require(facts.get("marker_verified") is True, "merge effect lacks authenticated marker and PR receipt")
            _validate_observed(request, payload, configuration, policy, facts, proof["marker_object"])
            subject = request["subject"]
            require(
                result["effect"]
                == {
                    "repository": subject["repository"],
                    "pull_request": subject["pull_request"],
                    "head_revision": subject["head_revision"],
                    "base_revision": subject["base_revision"],
                    "method": "merge",
                    "commit": payload["merge_revision"],
                    "remote_receipt_digest": delivery.digest(facts),
                },
                "merge effect differs from actual native result",
            )
        else:
            require(
                result["effect"] is None and facts.get("marker_verified") is not True,
                "nonterminal merge result claims an established effect",
            )
            if result["outcome"] == "rejected":
                require(
                    method == "invoke" and proof["write_started"] is False,
                    "an uncertain provider write cannot be called rejected",
                )
    return result
