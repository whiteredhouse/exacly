"""Bounded advisory implementation proposals from independently selected product intent.

Input pins and approval identities come from the trusted caller, never from a generated proposal.
The planner neither executes code nor grants authority to merge, publish, deploy or change criteria.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import jsonschema

from exacly import admission, planning, query
from exacly.applicability import Target
from exacly.core import bindings, is_state

Doc = dict[str, Any]
VERSION = "0.1.0"
MAX_GOALS = 64
MAX_TASKS = 512
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_PROPOSAL_BYTES = 16 * 1024 * 1024
MAX_NODES = 200_000
MAX_DEPTH = 64
TEXT = {"type": "string", "pattern": r"\S", "maxLength": 4096}
LABEL = {**TEXT, "maxLength": 256, "not": {"pattern": r"[\x00-\x1f\x7f]"}}
SHA = {"type": "string", "pattern": r"^[0-9a-f]{40}$", "minLength": 40, "maxLength": 40}
DIGEST: Doc = {**planning.DIGEST, "minLength": 71, "maxLength": 71}
DATE = {"type": "string", "format": "date", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "maxLength": 10}


def _array(items: Doc, maximum: int = MAX_GOALS, *, minimum: int = 0, unique: bool = False) -> Doc:
    return {"type": "array", "items": items, "maxItems": maximum, "minItems": minimum, "uniqueItems": unique}


REPOSITORY = planning.fields(
    {
        "id": {"type": "integer", "minimum": 1},
        "full_name": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
    }
)
TARGET = planning.fields({"context": LABEL, "model_revision": SHA, "revision": SHA})
REVIEW = planning.fields({"issuer": LABEL, "id": LABEL})
CRITERIA = _array({"type": "integer", "minimum": 0, "maximum": 4095}, 256, minimum=1, unique=True)
FINDING = planning.fields(
    {
        "id": LABEL,
        "kind": {"enum": ["observation_gap", "approved_behavior_gap", "investigated_finding"]},
        "criteria": CRITERIA,
        "reason": TEXT,
        "review": {"oneOf": [REVIEW, {"type": "null"}]},
        "evidence": _array(planning.fields({"locator": TEXT, "digest": DIGEST}), 16),
    }
)
GOAL = planning.fields(
    {
        "record": LABEL,
        "criteria": CRITERIA,
        "output_paths": _array(TEXT, 256, unique=True),
        "depends_on": _array(LABEL, unique=True),
        "findings": _array(FINDING),
        "unresolved_decisions": _array(TEXT),
    }
)
INTENT_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "id": LABEL,
        "revision": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
        "supersedes": {"oneOf": [DIGEST, {"type": "null"}]},
        "repository": REPOSITORY,
        "target": TARGET,
        "goals": _array(GOAL, minimum=1),
        "proposed_contract_changes": _array(
            planning.fields(
                {
                    "kind": {"enum": ["acceptance_criteria", "tests", "observation_bindings", "policy"]},
                    "record": LABEL,
                    "description": TEXT,
                    "content": {"type": "object"},
                }
            )
        ),
    }
)
POLICY_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "name": LABEL,
        "version": LABEL,
        "stage": {"const": "implementation-planning"},
        "issuer": LABEL,
        "approval_id": LABEL,
        "repository": REPOSITORY,
        "model": planning.fields({"namespace": LABEL, "digest": DIGEST}),
        "target": TARGET,
        "requirements": _array(LABEL, minimum=1, unique=True),
        "allowed_output_paths": _array(TEXT, 256, unique=True),
        "not_before": DATE,
        "expires_at": DATE,
        "genesis_contract_digest": DIGEST,
    }
)
CLAIMS = {
    "execution_authorized": False,
    "merge_authorized": False,
    "publication_authorized": False,
    "deployment_authorized": False,
    "contract_changes_approved": False,
    "implementation_verified": False,
    "self_authenticating": False,
}
_PROPOSAL_KEYS = {
    "schema_version",
    "kind",
    "id",
    "intent_id",
    "revision",
    "supersedes",
    "advisory",
    "as_of",
    "namespace",
    "repository",
    "target",
    "approval",
    "policy",
    "inputs",
    "observations",
    "tasks",
    "dependencies",
    "blockers",
    "proposed_contract_changes",
    "status",
    "claims",
    "digest",
}


def _copy(value: Any) -> Any:
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("implementation planning input exceeds nesting or node limits")
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    try:
        plain = planning.plain(value)
        if len(planning.canonical(plain).encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("implementation planning input exceeds the content size limit")
        return plain
    except (TypeError, RecursionError, UnicodeError) as error:
        raise ValueError("implementation planning requires bounded JSON-compatible inputs") from error


def _schema(value: Any, schema: Doc, label: str) -> Doc:
    result = _copy(value)
    try:
        jsonschema.validate(result, schema, format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "root"
        raise ValueError(f"invalid implementation {label} at {location} ({error.validator})") from error
    return dict(result)


def _path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in (".", "..", ".git") for part in path.parts)
        or "\\" in value
        or any(character in value for character in "*?[]")
        or not path.parts
        or re.match(r"^[A-Za-z]:", value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("implementation output paths must be literal relative repository files")
    return value


def _criteria(record: Doc) -> str:
    return planning.digest(
        {key: record.get(key) for key in ("statement", "acceptance_criteria", "verification_method")}
    )


def _bindings(record: Doc) -> str:
    return planning.digest({source: bindings(record, source) for source in sorted(query.bound(record, "verification"))})


def _artifact(value: Any) -> Doc:
    result = _copy(value)
    if not isinstance(result, dict) or set(result) != _PROPOSAL_KEYS:
        raise ValueError("invalid retained implementation proposal fields")
    if (
        result["schema_version"] != VERSION
        or result["kind"] != "implementation-proposal"
        or result["advisory"] is not True
        or result["claims"] != CLAIMS
        or type(result["revision"]) is not int
        or result["revision"] < 1
    ):
        raise ValueError("invalid retained advisory implementation proposal")
    digest = result.pop("digest")
    if digest != planning.digest(result):
        raise ValueError("retained implementation proposal content changed")
    result["digest"] = digest
    return result


def _acyclic(dependencies: dict[str, set[str]]) -> None:
    remaining = {key: set(value) for key, value in dependencies.items()}
    while remaining:
        ready = {key for key, values in remaining.items() if not values}
        if not ready:
            raise ValueError("implementation dependencies contain a cycle")
        remaining = {key: values - ready for key, values in remaining.items() if key not in ready}


def _observation(model: query.Model, record: str, target: Target, validity: Doc) -> Doc:
    projection = query.assess(model, record, target)
    state = projection["status"]["verification"]
    classification = (
        "satisfied" if state == "passed" else "investigate_failure" if state == "failed" else "collect_evidence"
    )
    if state != "passed" and not query.bound(model.record(record), "verification"):
        classification = "blocked_configuration"
    return {
        "record": record,
        "state": state,
        "classification": classification if validity["conforms"] else "blocked_observations",
        "native_classification": classification,
        "validation": copy.deepcopy(validity),
        "usable_as_prerequisite": validity["conforms"] and state == "passed",
        "assessment_digest": planning.digest({key: value for key, value in projection.items() if key != "as_of"}),
        "missing": projection["missing"],
        "excluded": projection["excluded"],
    }


@dataclass(frozen=True)
class _Context:
    requested: Doc
    chosen: Doc
    selected: Doc
    model: query.Model
    paths: set[str]
    goals: dict[str, Doc]
    records: Doc
    observations: dict[str, Doc]
    observation_errors: list[Doc]
    dependencies: dict[str, set[str]]
    inputs: Doc
    genesis: str


def _prepare(
    documents: Mapping[str, Any],
    intent: Any,
    planning_policy: Any,
    target: Target,
    date: dt.date,
    *,
    allow_missing_genesis: bool = False,
) -> _Context:
    if type(date) is not dt.date or not isinstance(documents, Mapping) or not isinstance(target, Target):
        raise ValueError("implementation planning requires named documents, an explicit target and date")
    selected = _schema(target.data, TARGET, "target")
    schema = copy.deepcopy(POLICY_SCHEMA)
    if allow_missing_genesis:
        schema["required"].remove("genesis_contract_digest")
    chosen = _schema(planning_policy, schema, "policy")
    requested = _schema(intent, INTENT_SCHEMA, "intent")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", chosen["repository"]["full_name"]) is None:
        raise ValueError("implementation planning requires an exact repository identity")
    if (
        requested["repository"] != chosen["repository"]
        or requested["target"] != selected
        or chosen["target"] != selected
    ):
        raise ValueError("implementation planning repository or target is outside approved scope")
    if not dt.date.fromisoformat(chosen["not_before"]) <= date < dt.date.fromisoformat(chosen["expires_at"]):
        raise ValueError("implementation planning scope is not current")
    paths = {_path(path) for path in chosen["allowed_output_paths"]}
    docs: Doc = _copy(documents)
    configs = {name: document for name, document in docs.items() if not is_state(document)}
    approved_model = query.load(configs, date, revision=selected["model_revision"])
    if approved_model.report.errors:
        raise ValueError("implementation planning requires a conforming approved model")
    # A stale positive state can fail native support validation. Project it conservatively as an
    # observation gap rather than treating absent current evidence as a defect in approved code.
    model = query.load(docs, date, revision=selected["model_revision"])
    if chosen["model"] != {"namespace": model.graph.namespace, "digest": admission.config_digest(configs)}:
        raise ValueError("implementation planning model differs from the independently approved content")
    goals = {goal["record"]: goal for goal in requested["goals"]}
    if len(goals) != len(requested["goals"]):
        raise ValueError("implementation intent contains duplicate Requirement goals")
    if set(goals) - set(chosen["requirements"]):
        raise ValueError("implementation Requirement is outside the approved planning scope")
    records: Doc = {}
    for rid in chosen["requirements"]:
        record = model.record(rid)
        if record["type"] != "Requirement" or record["lifecycle"] == "retired" or record["id"] != rid:
            raise ValueError("implementation goals require canonical, nonretired Requirement identities")
        records[rid] = record
    # Diagnostic file paths are local storage labels, not observation identities. Retained bundles
    # may reconstruct the same content under different filenames, so preserve semantic findings.
    observation_errors = sorted(
        (
            {key: value for key, value in query.finding(finding).items() if key != "document"}
            for finding in model.report.errors
        ),
        key=planning.canonical,
    )
    validity = {"conforms": not observation_errors, "findings_digest": planning.digest(observation_errors)}
    observations = {rid: _observation(model, rid, Target(selected), validity) for rid in goals}
    dependencies = {rid: set(goal["depends_on"]) for rid, goal in goals.items()}
    for rid, values in dependencies.items():
        if rid in values or values - goals.keys():
            raise ValueError("implementation dependency must identify another selected Requirement goal")
    # Runtime component relationships do not imply implementation-work ordering. This profile
    # expands only dependency edges explicitly selected in the independently approved intent.
    _acyclic(dependencies)
    inputs: Doc = {
        "model": {
            "namespace": model.graph.namespace,
            "revision": selected["model_revision"],
            "digest": admission.config_digest(configs),
        },
        "states": sorted(planning.digest(document) for document in docs.values() if is_state(document)),
        "criteria": {rid: _criteria(records[rid]) for rid in sorted(goals)},
        "bindings": {rid: _bindings(records[rid]) for rid in sorted(goals)},
        "intent_digest": planning.digest(requested),
        "policy_digest": planning.digest(chosen),
    }
    policy_scope = {key: value for key, value in chosen.items() if key != "genesis_contract_digest"}
    genesis = planning.digest(
        {
            "schema_version": VERSION,
            "intent": requested,
            "policy": policy_scope,
            "inputs": {**inputs, "policy_digest": planning.digest(policy_scope)},
            "repository": chosen["repository"],
            "target": selected,
            "observations": [observations[rid] for rid in sorted(observations)],
            "observation_validation": observation_errors,
        }
    )
    return _Context(
        requested,
        chosen,
        selected,
        model,
        paths,
        goals,
        records,
        observations,
        observation_errors,
        dependencies,
        inputs,
        genesis,
    )


def genesis_digest(
    documents: Mapping[str, Any],
    intent: Any,
    planning_policy: Any,
    target: Target,
    date: dt.date,
) -> str:
    """Fingerprint proposed initial inputs for independent review; this helper approves nothing.

    The policy may omit its genesis field while preparing that fingerprint. Only that field is
    excluded from the policy hash; model, state, observations and contract scope remain bound.
    """
    return _prepare(documents, intent, planning_policy, target, date, allow_missing_genesis=True).genesis


def create(
    documents: Mapping[str, Any],
    intent: Any,
    planning_policy: Any,
    target: Target,
    date: dt.date,
    *,
    approved_intent_digest: str,
    approved_policy_digest: str,
    issuer: str,
    approval_id: str,
    previous: Any | None = None,
) -> Doc:
    """Generate advisory criterion tasks from independently selected intent, never from missing tests.

    Approval pins are trusted caller inputs, not signatures authenticated by this pure API. Policy
    validity uses ISO dates with exclusive ``expires_at``. The pinned genesis distinguishes new
    reviewed lineages; advisory branches do not establish a globally active head or execution grant.
    """
    context = _prepare(documents, intent, planning_policy, target, date)
    requested, chosen, selected, model = context.requested, context.chosen, context.selected, context.model
    paths, goals, records = context.paths, context.goals, context.records
    observations, dependencies, inputs = context.observations, context.dependencies, context.inputs
    for pin, actual in ((approved_policy_digest, chosen), (approved_intent_digest, requested)):
        if not isinstance(pin, str) or re.fullmatch(DIGEST["pattern"], pin) is None or pin != planning.digest(actual):
            raise ValueError("implementation input does not match its independently selected approval pin")
    if chosen["issuer"] != issuer or chosen["approval_id"] != approval_id:
        raise ValueError("implementation planning approval identity differs from trusted caller selection")
    logical_id = planning.digest(
        {
            "schema_version": VERSION,
            "namespace": model.graph.namespace,
            "repository": chosen["repository"],
            "intent_id": requested["id"],
            "genesis_contract_digest": chosen["genesis_contract_digest"],
        }
    )
    if previous is None:
        if requested["revision"] != 1 or requested["supersedes"] is not None:
            raise ValueError("revised implementation intent requires its explicitly retained predecessor")
        if context.genesis != chosen["genesis_contract_digest"]:
            raise ValueError(
                "initial planning inputs changed; independently review an explicit revision or new genesis"
            )
    else:
        earlier = _artifact(previous)
        if (
            earlier["id"] != logical_id
            or earlier["intent_id"] != requested["id"]
            or requested["revision"] != earlier["revision"] + 1
            or requested["supersedes"] != earlier["digest"]
            or not isinstance(earlier["policy"], dict)
            or earlier["policy"].get("genesis_contract_digest") != chosen["genesis_contract_digest"]
        ):
            raise ValueError("implementation revision must explicitly supersede the exact preceding proposal")
    input_contract = planning.digest({"inputs": inputs, "repository": chosen["repository"], "target": selected})
    tasks: list[Doc] = []
    task_bytes = 0
    blockers: list[Doc] = []
    if context.observation_errors:
        blockers.append(
            {
                "code": "invalid_observations",
                "reason": "Native observation validation failed; positive assessments cannot satisfy prerequisites.",
                "findings": copy.deepcopy(context.observation_errors),
            }
        )
    by_record: dict[str, list[str]] = {rid: [] for rid in goals}
    for rid in sorted(goals):
        goal, record = goals[rid], records[rid]
        criteria = record.get("acceptance_criteria", [])
        if any(index >= len(criteria) for index in goal["criteria"]):
            raise ValueError("implementation goal selects a nonexistent approved criterion")
        output_paths = sorted(_path(path) for path in goal["output_paths"])
        if set(output_paths) - paths:
            raise ValueError("implementation output is outside the exact approved output scope")
        findings: set[str] = set()
        covered: set[int] = set()
        for finding in sorted(goal["findings"], key=lambda item: item["id"]):
            if finding["id"] in findings or set(finding["criteria"]) - set(goal["criteria"]):
                raise ValueError("implementation finding identity or criterion selection is ambiguous")
            findings.add(finding["id"])
            covered.update(finding["criteria"])
            if finding["kind"] == "observation_gap":
                continue
            if finding["review"] is None or not finding["evidence"]:
                raise ValueError("implementation work requires a reviewed behavior gap or investigated finding")
            if not output_paths:
                raise ValueError("implementation work requires an explicit approved output scope")
            for index in sorted(finding["criteria"]):
                criterion = {"index": index, "text": criteria[index]}
                criterion["digest"] = planning.digest({"record": rid, **criterion})
                task_id = planning.digest(
                    {"proposal": logical_id, "record": rid, "finding": finding["id"], "criterion": criterion["digest"]}
                )
                task = {
                    "id": task_id,
                    "record": rid,
                    "criterion": criterion,
                    "criteria_digest": inputs["criteria"][rid],
                    "bindings_digest": inputs["bindings"][rid],
                    "finding": copy.deepcopy(finding),
                    "input_contract": input_contract,
                    "outputs": output_paths,
                    "depends_on": [],
                    "completion": {
                        "record": rid,
                        "criterion_digest": criterion["digest"],
                        "condition": criteria[index],
                        "target": copy.deepcopy(selected),
                        "requires_trusted_observation": True,
                        "requires_independent_review": True,
                    },
                    "blockers": list(goal["unresolved_decisions"]),
                }
                task_bytes += len(planning.canonical(task).encode("utf-8"))
                if task_bytes > MAX_PROPOSAL_BYTES:
                    raise ValueError("implementation proposal exceeds the generated content size limit")
                tasks.append(task)
                by_record[rid].append(task_id)
                if len(tasks) > MAX_TASKS:
                    raise ValueError("implementation proposal exceeds the task limit")
        for index in sorted(set(goal["criteria"]) - covered):
            blockers.append(
                {
                    "record": rid,
                    "code": "uninvestigated_criterion",
                    "criterion": index,
                    "reason": "The selected criterion needs an explicit finding; missing evidence is not a code gap.",
                }
            )
        for decision in goal["unresolved_decisions"]:
            blockers.append({"record": rid, "code": "unresolved_decision", "reason": decision})
    for task in tasks:
        for dependency in sorted(dependencies[task["record"]]):
            task["depends_on"].extend(sorted(by_record[dependency]))
            if not by_record[dependency] and not observations[dependency]["usable_as_prerequisite"]:
                reason = (
                    f"Dependency {dependency} has no approved implementation task or applicable passed observation."
                )
                task["blockers"].append(reason)
                blockers.append({"record": task["record"], "code": "unresolved_dependency", "reason": reason})
    changes = []
    for change in requested["proposed_contract_changes"]:
        if change["record"] not in goals:
            raise ValueError("proposed contract change must trace to a selected Requirement")
        changes.append(
            {
                **change,
                "id": planning.digest(change),
                "review_status": "unapproved",
                "requires_independent_review": True,
            }
        )
    if changes:
        blockers.append(
            {
                "code": "unreviewed_contract_changes",
                "reason": "Generated contract changes need independent review and supersession.",
            }
        )
    result: Doc = {
        "schema_version": VERSION,
        "kind": "implementation-proposal",
        "id": logical_id,
        "intent_id": requested["id"],
        "revision": requested["revision"],
        "supersedes": requested["supersedes"],
        "advisory": True,
        "as_of": date.isoformat(),
        "namespace": model.graph.namespace,
        "repository": chosen["repository"],
        "target": selected,
        "approval": {
            "issuer": issuer,
            "id": approval_id,
            "intent_digest": approved_intent_digest,
            "policy_digest": approved_policy_digest,
        },
        "policy": {
            "name": chosen["name"],
            "version": chosen["version"],
            "digest": approved_policy_digest,
            "genesis_contract_digest": chosen["genesis_contract_digest"],
        },
        "inputs": inputs,
        "observations": [observations[rid] for rid in sorted(observations)],
        "tasks": tasks,
        "dependencies": [{"task": task["id"], "depends_on": list(task["depends_on"])} for task in tasks],
        "blockers": blockers,
        "proposed_contract_changes": changes,
        "status": "blocked"
        if blockers
        else "work_proposed"
        if tasks
        else "observation_gaps"
        if any(item["state"] != "passed" for item in observations.values())
        else "no_work",
        "claims": copy.deepcopy(CLAIMS),
    }
    result["digest"] = planning.digest(result)
    if len(planning.canonical(result).encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ValueError("implementation proposal exceeds the generated content size limit")
    _copy(result)  # Ensure every generated artifact remains readable within the public bounds.
    return result


def validate_current(
    proposal: Any,
    documents: Mapping[str, Any],
    intent: Any,
    planning_policy: Any,
    target: Target,
    date: dt.date,
    *,
    approved_intent_digest: str,
    approved_policy_digest: str,
    issuer: str,
    approval_id: str,
    previous: Any | None = None,
) -> Doc:
    """Reject stale or modified proposals before a separate execution review; grant no authority."""
    retained = _artifact(proposal)
    current = create(
        documents,
        intent,
        planning_policy,
        target,
        date,
        approved_intent_digest=approved_intent_digest,
        approved_policy_digest=approved_policy_digest,
        issuer=issuer,
        approval_id=approval_id,
        previous=previous,
    )
    old, fresh = copy.deepcopy(retained), copy.deepcopy(current)
    # The checking date may advance while all applicable inputs and observations remain identical.
    # Assessment identity excludes the clock alone; expiration still changes its applicable result.
    for value in (old, fresh):
        value.pop("digest")
        value.pop("as_of")
    if old != fresh:
        raise ValueError("implementation proposal inputs or content changed; independently review an explicit revision")
    return {
        "schema_version": VERSION,
        "operation": "check-implementation-proposal",
        "status": "blocked" if retained["status"] == "blocked" else "current",
        "proposal": {key: retained[key] for key in ("id", "revision", "digest")},
        "checked_as_of": date.isoformat(),
        "blockers": copy.deepcopy(retained["blockers"]),
        "ready_for_separate_execution_review": bool(retained["tasks"]) and not retained["blockers"],
        "claims": copy.deepcopy(CLAIMS),
    }
