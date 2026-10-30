"""Read-only readiness policies, evaluated separately from model conformance."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

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


JUNIT_PREFIX = "exacly.gate/v1"
JUNIT_MAX_BYTES = 8 * 1024 * 1024
JUNIT_MAX_CHECKS = 4096
JUNIT_MAX_IDENTITY_BYTES = 1024
JUNIT_MAX_DIAGNOSTIC_BYTES = 4096
JUNIT_MAX_REFERENCES = 4
_JUNIT_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_JUNIT_RESULTS = {"satisfied", "not_satisfied", "unknown"}


def _junit_identity(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("JUnit identity components must be nonempty strings")
    if len(value.encode("utf-8", errors="surrogatepass")) > JUNIT_MAX_IDENTITY_BYTES:
        raise ValueError("JUnit identity component exceeds the supported size")
    # Encode the original identity, including XML-invalid characters, rather than merging distinct
    # identities through display sanitization. Wildcards and separators also become literal selectors.
    return quote(value, safe="", encoding="utf-8", errors="surrogatepass")


def _junit_reference(value: Any) -> str | None:
    if not isinstance(value, str) or any(
        ord(char) <= 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme:
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
            # Validate ports as well; urlsplit otherwise defers malformed port errors.
            _ = parsed.port
            reference = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        else:
            decoded = unquote(parsed.path, errors="strict")
            if (
                parsed.netloc
                or not decoded
                or decoded.startswith(("/", "\\"))
                or "\\" in decoded
                or ".." in decoded.split("/")
                or any(ord(char) <= 32 or 0x7F <= ord(char) <= 0x9F for char in decoded)
            ):
                return None
            reference = parsed.path
        if len(reference.encode("utf-8", errors="surrogatepass")) > JUNIT_MAX_IDENTITY_BYTES:
            return None
        return reference
    except ValueError:
        return None


def _junit_text(value: str, limit: int = JUNIT_MAX_DIAGNOSTIC_BYTES) -> str:
    """Bound display text and redact URL credentials/query strings, preserving valid XML 1.0."""
    value = _JUNIT_URL.sub(lambda found: _junit_reference(found.group()) or "[redacted reference]", value)
    prefix = value[:limit]
    safe = "".join(
        char
        if ord(char) in {9, 10, 13}
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
        else "\ufffd"
        for char in prefix
    )
    content = safe.encode("utf-8")
    if len(prefix) != len(value) or len(content) > limit:
        marker = "\n[truncated]"
        safe = content[: limit - len(marker)].decode("utf-8", errors="ignore") + marker
    return safe


def _junit_digest(value: Any) -> str:
    try:
        content = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("JUnit requires a JSON-compatible native readiness report") from error
    return "sha256:" + hashlib.sha256(content.encode("ascii")).hexdigest()


def _junit_property(properties: ET.Element, name: str, value: str) -> None:
    ET.SubElement(properties, "property", {"name": name, "value": _junit_text(value)})


def as_junit(report: Doc, *, namespace: str, report_id: str) -> str:
    """Render one native gate evaluation; blocking readiness is never exported as skipped tests.

    The explicit logical report ID distinguishes target slots without putting changing revisions in
    case IDs. This report describes an evaluation, not a fresh product test or authenticated evidence.
    """
    if not isinstance(report, dict):
        raise ValueError("JUnit requires a native readiness report")
    selected_policy, target = report.get("policy"), report.get("target")
    if not isinstance(selected_policy, dict) or not isinstance(target, dict):
        raise ValueError("JUnit requires native policy and target metadata")
    parts = [_junit_identity(value) for value in (namespace, selected_policy.get("name"), report_id)]
    classname = "/".join([JUNIT_PREFIX, *parts])
    _junit_identity(selected_policy.get("version"))
    _junit_identity(report.get("as_of"))
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks or len(checks) > JUNIT_MAX_CHECKS:
        raise ValueError("JUnit requires a nonempty bounded set of readiness prerequisites")
    identities: set[str] = set()
    cases = []
    results = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("JUnit requires native readiness prerequisite objects")
        name = "/".join(_junit_identity(check.get(key)) for key in ("record", "dimension"))
        identity = f"{classname}/{name}"
        result = check.get("result")
        if (
            identity in identities
            or check.get("dimension") not in {"verification", "delivery"}
            or not isinstance(result, str)
            or result not in _JUNIT_RESULTS
            or not isinstance(check.get("reason"), str)
            or not isinstance(check.get("state"), str)
        ):
            raise ValueError("JUnit prerequisite identity or readiness result is invalid")
        identities.add(identity)
        cases.append((check, name, identity))
        results.append(result)
    expected = "not_satisfied" if "not_satisfied" in results else "unknown" if "unknown" in results else "satisfied"
    if report.get("result") != expected:
        raise ValueError("JUnit native report has inconsistent aggregate readiness")
    counts = {"tests": str(len(cases)), "failures": str(sum(value != "satisfied" for value in results))}
    counts.update(errors="0", skipped="0")
    root = ET.Element("testsuites", counts)
    suite = ET.SubElement(root, "testsuite", {"name": classname, **counts})
    properties = ET.SubElement(suite, "properties")
    metadata = {
        "exacly.report.kind": "readiness-evaluation",
        "exacly.namespace": namespace,
        "exacly.report.id": report_id,
        "exacly.policy.name": selected_policy["name"],
        "exacly.policy.version": selected_policy["version"],
        "exacly.as_of": report["as_of"],
        "exacly.readiness.result": report["result"],
        "exacly.target.digest": _junit_digest(target),
        "exacly.native_report.digest": _junit_digest(report),
    }
    # Arbitrary build fields and raw report payloads can contain service credentials. Keep their
    # complete content identity as a digest and expose only the standard target coordinates.
    for key in ("context", "model_revision", "revision", "environment", "platform", "release"):
        if key in target:
            _junit_identity(target[key])
            metadata[f"exacly.target.{key}"] = target[key]
    for key, value in metadata.items():
        _junit_property(properties, key, value)
    findings = report.get("findings", {})
    if not isinstance(findings, dict):
        raise ValueError("JUnit requires native validation findings")
    messages = []
    for severity in ("errors", "warnings"):
        values = findings.get(severity, [])
        if not isinstance(values, list):
            raise ValueError("JUnit requires native validation findings")
        for finding in values[:8]:
            if not isinstance(finding, dict):
                raise ValueError("JUnit requires native validation finding objects")
            messages.append(_junit_text(f"{severity}: {finding.get('rule', '')}: {finding.get('message', '')}", 512))
        if len(values) > 8:
            messages.append(f"{severity}: additional findings omitted")
    if messages:
        ET.SubElement(suite, "system-out").text = _junit_text("\n".join(messages))
    size = len(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    if size > JUNIT_MAX_BYTES:
        raise ValueError("JUnit report exceeds the supported size")
    for check, name, identity in cases:
        case = ET.Element("testcase", {"classname": classname, "name": name})
        case_properties = ET.SubElement(case, "properties")
        ET.SubElement(case_properties, "property", {"name": "exacly-case", "value": identity})
        for key, value in {
            "exacly.record": check["record"],
            "exacly.dimension": check["dimension"],
            "exacly.state": check["state"],
            "exacly.readiness.result": check["result"],
        }.items():
            _junit_property(case_properties, key, value)
        evidence = check.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("JUnit requires native evidence references")
        references = []
        for entry in evidence:
            reference = _junit_reference(entry.get("locator")) if isinstance(entry, dict) else None
            if reference is not None and reference not in references:
                references.append(reference)
                if len(references) == JUNIT_MAX_REFERENCES:
                    break
        for number, reference in enumerate(references, start=1):
            _junit_property(case_properties, f"exacly.evidence.{number}", reference)
        excluded = check.get("excluded", [])
        if not isinstance(excluded, list):
            raise ValueError("JUnit requires native excluded observations")
        excluded_references: dict[str, str] = {}
        for exclusion in excluded:
            if len(excluded_references) == JUNIT_MAX_REFERENCES:
                break
            assessment = exclusion.get("assessment") if isinstance(exclusion, dict) else None
            if not isinstance(assessment, dict):
                continue
            evidence = assessment.get("evidence", [])
            reason = exclusion.get("reason")
            if not isinstance(evidence, list) or not isinstance(reason, str):
                raise ValueError("JUnit requires native excluded evidence and reasons")
            for entry in evidence:
                reference = _junit_reference(entry.get("locator")) if isinstance(entry, dict) else None
                if reference is not None and reference not in excluded_references:
                    excluded_references[reference] = reason
                    if len(excluded_references) == JUNIT_MAX_REFERENCES:
                        break
        for number, (reference, reason) in enumerate(excluded_references.items(), start=1):
            _junit_property(case_properties, f"exacly.excluded_evidence.{number}", reference)
            _junit_property(case_properties, f"exacly.excluded_evidence.{number}.reason", _junit_text(reason, 512))
        if check["result"] != "satisfied":
            failure = ET.SubElement(
                case,
                "failure",
                {
                    "type": f"exacly.readiness.{check['result']}",
                    "message": _junit_text(check["reason"], 512),
                },
            )
            failure.text = _junit_text(check["reason"])
        size += len(ET.tostring(case, encoding="utf-8"))
        if size > JUNIT_MAX_BYTES:
            raise ValueError("JUnit report exceeds the supported size")
        # Cases precede suite diagnostics for conventional JUnit readers.
        suite.insert(len(suite) - bool(messages), case)
    content: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(content) > JUNIT_MAX_BYTES:
        raise ValueError("JUnit report exceeds the supported size")
    return content.decode("utf-8")
