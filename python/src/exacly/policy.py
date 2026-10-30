"""Read-only readiness policies, evaluated separately from model conformance."""

from __future__ import annotations

import copy
import json
from typing import Any

import jsonschema

from exacly import query
from exacly.applicability import TEXT, Target

Doc = dict[str, Any]
SCHEMA: Doc = {
    "type": "object",
    "required": ["name", "version", "requirements"],
    "additionalProperties": False,
    "properties": {
        "name": TEXT,
        "version": TEXT,
        "requirements": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "object",
                "required": ["record", "dimensions"],
                "additionalProperties": False,
                "properties": {
                    "record": TEXT,
                    "dimensions": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"enum": ["verification", "delivery"]},
                    },
                },
            },
        },
    },
}
POSITIVE = {"verification": "passed", "delivery": "released"}
NEGATIVE = {"verification": {"failed"}, "delivery": {"withdrawn", "planned", "in_progress"}}


def validate_policy(model: query.Model, policy: Any) -> Doc:
    """Validate a readiness policy and all model references without evaluating observations.

    The returned snapshot can be independently reviewed and retained. Validation neither approves
    the policy nor establishes readiness, and performs no source reads or filesystem operations.
    """
    try:
        jsonschema.validate(policy, SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid policy: {error.message}") from error
    requested: set[tuple[str, str]] = set()
    for entry in policy["requirements"]:
        try:
            record = model.record(entry["record"])
        except query.QueryError as error:
            raise ValueError(f"invalid policy: {error}") from error
        if record["type"] != "Requirement":
            raise ValueError(f"invalid policy: {entry['record']} is not a Requirement")
        for dimension in entry["dimensions"]:
            key = record["id"], dimension
            if key in requested:
                raise ValueError(f"invalid policy: duplicate check {key[0]} {dimension}")
            requested.add(key)
    return copy.deepcopy(policy)


def evaluate(model: query.Model, policy: Any, target: Target) -> Doc:
    """Evaluate a validated policy against selected evidence without observing or changing anything.

    Negative results dominate unknown prerequisites. Invalid models cannot establish a satisfied policy;
    invalid policy documents raise ValueError rather than becoming a readiness decision.
    """
    chosen = validate_policy(model, policy)
    requested = {
        (model.record(entry["record"])["id"], dimension)
        for entry in chosen["requirements"]
        for dimension in entry["dimensions"]
    }
    checks = []
    for rid, dimension in sorted(requested):
        assessment = query.assess(model, rid, target)
        applicable = [a for a in assessment["applicable"] if a["dimension"] == dimension]
        excluded = [e for e in assessment["excluded"] if e["assessment"]["dimension"] == dimension]
        missing = [m for m in assessment["missing"] if m["dimension"] == dimension]
        state = assessment["status"][dimension]
        if model.report.errors:
            result, reason = "unknown", "the model does not conform; see validation findings"
        elif state in NEGATIVE[dimension]:
            result = "not_satisfied"
            reason = "; ".join(a.get("reason") or a["state"] for a in applicable if a["state"] in NEGATIVE[dimension])
        elif state == POSITIVE[dimension] and not missing:
            result, reason = "satisfied", f"applicable evidence establishes {state}"
        else:
            result = "unknown"
            reasons = [e["reason"] for e in excluded]
            reasons += [f"missing {m['source']} result" for m in missing]
            reason = "; ".join(dict.fromkeys(reasons)) or f"{dimension} is {state} for the selected target"
        checks.append(
            {
                "record": rid,
                "dimension": dimension,
                "state": state,
                "result": result,
                "reason": reason,
                "evidence": [e for a in applicable for e in a["evidence"]],
                "assessments": applicable,
                "excluded": excluded,
                "missing": missing,
            }
        )
    outcomes = {c["result"] for c in checks}
    result = "not_satisfied" if "not_satisfied" in outcomes else "unknown" if "unknown" in outcomes else "satisfied"
    return {
        "policy": {"name": chosen["name"], "version": chosen["version"]},
        "target": target.data,
        "as_of": model.report.date.isoformat(),
        "result": result,
        "checks": checks,
        "findings": {
            "errors": [query.finding(f) for f in model.report.errors],
            "warnings": [query.finding(f) for f in model.report.warnings],
        },
    }


def as_text(report: Doc) -> str:
    """An explainable decision, with its policy version and selected target."""
    lines = [
        f"Policy: {report['policy']['name']} version {report['policy']['version']}",
        f"Target: {json.dumps(report['target'], sort_keys=True)}",
        f"As of: {report['as_of']}",
        f"Result: {report['result']}",
    ]
    lines += [f"  {c['record']} {c['dimension']}: {c['result']} — {c['reason']}" for c in report["checks"]]
    lines += [f"  Validation {f['rule']}: {f['message']}" for f in report["findings"]["errors"]]
    return "\n".join(lines)
