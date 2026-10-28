"""Read-only work proposals for explicit Requirement verification goals.

Plans describe supplied observations; they neither authenticate those observations nor authorize execution.
The versioned artifact and its human rendering share one result from the readiness evaluator.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from collections.abc import Mapping
from importlib import metadata
from typing import Any

from exacly import policy, query
from exacly.applicability import TARGET_SCHEMA, TEXT, Target
from exacly.core import bindings, is_state, minor_version, version_of

Doc = dict[str, Any]
VERSION = "0.1.0"
DIGEST = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
OBJECTS = {"type": "array", "items": {"type": "object"}}
CLASSIFICATIONS = ["satisfied", "collect_evidence", "investigate_failure", "blocked_configuration", "blocked_model"]


def fields(properties: Doc, optional: tuple[str, ...] = ()) -> Doc:
    """A closed object in the public artifact schema."""
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key not in optional],
        "additionalProperties": False,
    }


COMPLETION = fields(
    {
        "record": TEXT,
        "dimension": {"const": "verification"},
        "required_state": {"const": "passed"},
        "target": TARGET_SCHEMA,
        "criteria_digest": DIGEST,
        "bindings_digest": DIGEST,
        "requires_trusted_observation": {"const": True},
    }
)
SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Exacly advisory verification plan 0.1.0",
    **fields(
        {
            "schema_version": {"const": VERSION},
            "digest": DIGEST,
            "advisory": {"const": True},
            "tool": fields({"name": {"const": "exacly"}, "version": TEXT}),
            "namespace": TEXT,
            "target": TARGET_SCHEMA,
            "policy": fields({"name": TEXT, "version": TEXT, "digest": DIGEST}),
            "as_of": {"type": "string", "format": "date"},
            "status": {"enum": ["satisfied", "work_proposed", "blocked"]},
            "readiness": {"enum": ["satisfied", "not_satisfied", "unknown"]},
            "inputs": fields(
                {
                    "config": fields({"revision": TEXT, "digest": DIGEST}),
                    "states": {
                        "type": "array",
                        "items": fields(
                            {
                                "namespace": TEXT,
                                "context": TEXT,
                                "config_revision": TEXT,
                                "lineage": TEXT,
                                "serial": {"type": "integer", "minimum": 0},
                                "digest": DIGEST,
                            }
                        ),
                    },
                }
            ),
            "checks": {
                "type": "array",
                "items": fields(
                    {
                        "record": TEXT,
                        "dimension": {"const": "verification"},
                        "state": {"enum": ["passed", "failed", "inconclusive", "not_checked"]},
                        "result": {"enum": ["satisfied", "not_satisfied", "unknown"]},
                        "reason": TEXT,
                        "evidence": OBJECTS,
                        "assessments": OBJECTS,
                        "excluded": OBJECTS,
                        "missing": OBJECTS,
                        "classification": {"enum": CLASSIFICATIONS},
                        "action_id": DIGEST,
                    },
                    optional=("action_id",),
                ),
            },
            "actions": {
                "type": "array",
                "items": fields(
                    {
                        "id": DIGEST,
                        "kind": {"enum": ["collect_evidence", "investigate_failure", "blocked_configuration"]},
                        "record": TEXT,
                        "dimension": {"const": "verification"},
                        "reason": TEXT,
                        "criteria_digest": DIGEST,
                        "bindings_digest": DIGEST,
                        "completion": COMPLETION,
                    }
                ),
            },
            "findings": fields({"errors": OBJECTS, "warnings": OBJECTS}),
        }
    ),
}


def plain(value: Any) -> Any:
    """Copy JSON-compatible inputs, converting YAML date metadata without allowing ambiguous hashes."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("plan inputs cannot contain nonfinite numbers")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("plan inputs require string object keys")
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    raise ValueError(f"plan input is not JSON-compatible: {type(value).__name__}")


def canonical(value: Any) -> str:
    """Canonical JSON for this artifact version; authored array order remains significant."""
    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def classification(check: Doc, record: Doc, blocked: bool) -> str:
    if blocked:
        return "blocked_model"
    if check["result"] == "satisfied":
        return "satisfied"
    if check["result"] == "not_satisfied":
        return "investigate_failure"
    if not query.bound(record, "verification"):
        return "blocked_configuration"
    return "collect_evidence"


def proposal(check: Doc, record: Doc, namespace: str, selection: Doc, chosen_policy: Doc) -> Doc:
    """Identify the unresolved goal independently of observation refreshes, reasons and evaluation dates."""
    criteria = digest({key: record.get(key) for key in ("statement", "acceptance_criteria", "verification_method")})
    bound = digest({source: bindings(record, source) for source in sorted(query.bound(record, "verification"))})
    identity = {
        "schema_version": VERSION,
        "namespace": namespace,
        "target": selection,
        "policy": chosen_policy,
        "record": check["record"],
        "dimension": "verification",
        "kind": check["classification"],
        "criteria_digest": criteria,
        "bindings_digest": bound,
    }
    reason = check["reason"]
    if check["classification"] == "blocked_configuration":
        reason += "; no verification observation binding is configured"
    return {
        "id": digest(identity),
        "kind": check["classification"],
        "record": check["record"],
        "dimension": "verification",
        "reason": reason,
        "criteria_digest": criteria,
        "bindings_digest": bound,
        "completion": {
            "record": check["record"],
            "dimension": "verification",
            "required_state": "passed",
            "target": selection,
            "criteria_digest": criteria,
            "bindings_digest": bound,
            "requires_trusted_observation": True,
        },
    }


def create(documents: Mapping[str, Any], policy_doc: Any, target: Target, date: dt.date) -> Doc:
    """Propose verification work for explicit policy goals without changing or authenticating inputs.

    A schema-valid but nonconforming model produces a blocked plan with no work proposals. Unreadable
    models and invalid policies or targets raise QueryError or ValueError. A plan is never an apply contract.
    """
    if type(date) is not dt.date:
        raise ValueError("planning requires an explicit evaluation date")
    if not isinstance(documents, Mapping):
        raise ValueError("planning requires named config and state documents")
    selected = Target(copy.deepcopy(target.data))
    model = query.load(documents, date, revision=selected.data["model_revision"])
    if minor_version(version_of(model.report.doc)) != "0.2":
        raise ValueError("planning requires 0.2 config and state documents")
    report = policy.evaluate(model, policy_doc, selected)
    if any(check["dimension"] != "verification" for check in report["checks"]):
        raise ValueError("planning supports verification-only policies; delivery planning is not available")
    if any(model.record(check["record"])["lifecycle"] == "retired" for check in report["checks"]):
        raise ValueError("planning cannot propose work for retired Requirements")
    docs = plain(documents)
    selection = plain(selected.data)
    chosen_policy = {**report["policy"], "digest": digest(policy_doc)}
    blocked = bool(model.report.errors)
    checks, actions = [], []
    for check in report["checks"]:
        record = model.record(check["record"])
        check = {**check, "classification": classification(check, record, blocked)}
        if check["classification"] not in {"satisfied", "blocked_model"}:
            action = proposal(check, record, model.graph.namespace, selection, chosen_policy)
            actions.append(action)
            check["action_id"] = action["id"]
        checks.append(check)
    config = sorted((doc for doc in docs.values() if not is_state(doc)), key=canonical)
    states = sorted(
        (
            {
                **{key: doc[key] for key in ("namespace", "context", "config_revision", "lineage", "serial")},
                "digest": digest(doc),
            }
            for doc in docs.values()
            if is_state(doc)
        ),
        key=canonical,
    )
    result: Doc = {
        "schema_version": VERSION,
        "advisory": True,
        "tool": {"name": "exacly", "version": metadata.version("exacly")},
        "namespace": model.graph.namespace,
        "target": selection,
        "policy": chosen_policy,
        "as_of": date.isoformat(),
        "status": "blocked" if blocked else "work_proposed" if actions else "satisfied",
        "readiness": report["result"],
        "inputs": {"config": {"revision": selection["model_revision"], "digest": digest(config)}, "states": states},
        "checks": checks,
        "actions": actions,
        "findings": report["findings"],
    }
    result = plain(result)
    result["digest"] = digest(result)
    return result


def as_text(plan: Doc) -> str:
    """Render the saved result for review without loading or evaluating inputs again."""
    lines = [
        f"Exacly verification plan {plan['schema_version']}",
        f"Plan: {plan['digest']}",
        f"Model: {plan['namespace']}",
        f"Policy: {plan['policy']['name']} version {plan['policy']['version']}",
        f"Target: {canonical(plan['target'])}",
        f"As of: {plan['as_of']}",
        f"Status: {plan['status']}",
        f"Readiness: {plan['readiness']}",
        "Advisory only: no execution, evidence authentication or automatic completion.",
        "",
        f"Checks ({len(plan['checks'])})",
    ]
    lines += [f"  {check['record']}: {check['classification']} — {check['reason']}" for check in plan["checks"]]
    lines += ["", f"Actions ({len(plan['actions'])})"]
    for action in plan["actions"]:
        completion = action["completion"]
        lines += [
            f"  {action['id']} {action['record']}: {action['kind']}",
            f"    Reason: {action['reason']}",
            f"    Completion: {completion['dimension']} {completion['required_state']} for the selected target",
            f"    Criteria: {completion['criteria_digest']}; bindings: {completion['bindings_digest']}",
            "    Completion requires a trusted observation path.",
        ]
    for severity in ("errors", "warnings"):
        lines += ["", f"Validation {severity} ({len(plan['findings'][severity])})"]
        lines += [f"  {finding['rule']}: {finding['message']}" for finding in plan["findings"][severity]]
    return "\n".join(lines)
