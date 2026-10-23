"""Validate an Exacly product map against the specification.

Checks the document against the JSON Schema for its spec version, then runs
the checks that need the whole map: section 11's errors and warnings and the
section 7 traceability rules. The result is a validation report (section 11).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

ERROR, WARNING = "error", "warning"
EXPIRY_WARNING_DAYS = 30

# Section 6: allowed target types per relationship and source type.
ENDPOINTS: dict[str, dict[str, set[str]]] = {
    "contributes_to": {"Capability": {"Outcome"}},
    "uses_capability": {"Process": {"Capability"}},
    "supports": {"Requirement": {"Process", "Capability", "Outcome"}},
    "implements": {
        "Application": {"Requirement"},
        "Infrastructure": {"Requirement"},
        "Process": {"Requirement"},
    },
    "uses": {"Process": {"Application"}},
    "reads": {"Application": {"Information"}},
    "writes": {"Application": {"Information"}},
    "depends_on": {"Application": {"Application", "Infrastructure"}, "Infrastructure": {"Infrastructure"}},
    "evaluates": {"Measure": {"Outcome"}},
}
SAME_TYPE = {"contains", "supersedes"}
DELIVERY_EVIDENCE = {"implementation", "deployment"}
VERIFICATION_EVIDENCE = {"test", "decision"}

Doc = dict[str, Any]


@dataclass
class Finding:
    severity: str
    rule: str
    record: str | None
    message: str
    waived_by: dict[str, Any] | None = None


class Report:
    def __init__(self, doc: Any, date: dt.date) -> None:
        self.doc: Doc = doc if isinstance(doc, dict) else {}
        self.date = date
        self.findings: list[Finding] = []
        self.graph_checked = False

    def add(self, severity: str, rule: str, record: str | None, message: str) -> None:
        self.findings.append(Finding(severity, rule, record, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR and not f.waived_by]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING and not f.waived_by]

    @property
    def waived(self) -> list[Finding]:
        return [f for f in self.findings if f.waived_by]

    @property
    def result(self) -> str:
        if self.errors:
            return "does not conform"
        return "conforms with exceptions" if self.waived else "conforms"

    def as_dict(self) -> dict[str, Any]:
        model = self.doc.get("model", {})
        return {
            "result": self.result,
            "date": self.date.isoformat(),
            "spec_version": self.doc.get("exacly"),
            "namespace": model.get("namespace"),
            "scope": model.get("scope"),
            "state": self.doc.get("state"),
            "revision": self.doc.get("revision"),
            "base_revision": self.doc.get("base_revision"),
            "graph_checked": self.graph_checked,
            "errors": [asdict(f) for f in self.errors],
            "warnings": [asdict(f) for f in self.warnings],
            "exceptions_applied": [asdict(f) for f in self.waived],
        }

    def as_text(self) -> str:
        d = self.as_dict()
        scope = d["scope"] or {}
        base = f"  base {d['base_revision']}" if d["base_revision"] else ""
        lines = [
            "Exacly validation report",
            f"Model: {d['namespace']}  spec {d['spec_version']}  state {d['state']}",
            f"Revision: {d['revision'] or '(not given)'}{base}",
            f"Scope: {scope.get('covers', '(not given)')}",
            f"Date: {d['date']}",
            f"Result: {d['result']}",
        ]
        if not self.graph_checked:
            lines.append("Graph checks were skipped because the document does not match the schema.")
        sections = (("Errors", self.errors), ("Warnings", self.warnings), ("Exceptions applied", self.waived))
        for title, items in sections:
            lines.append(f"\n{title} ({len(items)})")
            for f in items:
                where = f" {f.record}" if f.record else ""
                lines.append(f"  [{f.rule}]{where}: {f.message}")
        return "\n".join(lines)


def schema_dirs() -> list[Path]:
    """Schemas bundled in the installed package, then the repository's spec/schema."""
    bundled = Path(str(resources.files("exacly_validate") / "schemas"))
    return [bundled, Path(__file__).resolve().parents[3] / "spec" / "schema"]


def schema_for(version: str) -> dict[str, Any]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.\d+", str(version))
    if not match:
        raise ValueError(f"not a spec version: {version!r}")
    for root in schema_dirs():
        iterations = sorted((root / f"{match[1]}.{match[2]}").glob("*.json"))
        if iterations:
            # The latest dated iteration is the correct one.
            schema: dict[str, Any] = json.loads(iterations[-1].read_text())
            return schema
    raise ValueError(f"no schema for spec version {version}")


def check_schema(doc: Any, report: Report) -> bool:
    try:
        schema = schema_for(doc.get("exacly", "") if isinstance(doc, dict) else "")
    except ValueError as error:
        report.add(ERROR, "schema", None, str(error))
        return False
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    for e in errors:
        where = "/" + "/".join(str(p) for p in e.absolute_path)
        report.add(ERROR, "schema", None, f"{where}: {e.message}")
    return not errors


@dataclass(frozen=True)
class Edge:
    source: str
    type: str
    target: str
    label: str


class Graph:
    """The map's records and resolved relationships, indexed once. The document is not changed."""

    def __init__(self, doc: Doc, report: Report) -> None:
        self.namespace: str = doc["model"]["namespace"]
        self.records: dict[str, Doc] = {}
        for r in doc["records"]:
            if r["id"] in self.records:
                report.add(ERROR, "11.duplicate-id", r["id"], "ID is used by more than one record")
            self.records.setdefault(r["id"], r)
        self.edges: list[Edge] = []
        self._out: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._in: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._resolve(doc["relationships"], report)

    def _resolve(self, relationships: list[Doc], report: Report) -> None:
        rel_ids: set[str] = set()
        seen: set[tuple[str, str, str]] = set()
        for rel in relationships:
            label = f"{rel['source']} --{rel['type']}--> {rel['target']}"
            if "id" in rel:
                if rel["id"] in rel_ids or rel["id"] in self.records:
                    report.add(ERROR, "11.duplicate-id", rel["id"], "relationship ID is already used")
                rel_ids.add(rel["id"])
            source, target = self.local(rel["source"]), self.local(rel["target"])
            missing = [
                ref
                for ref, local in ((rel["source"], source), (rel["target"], target))
                if local is not None and local not in self.records
            ]
            for ref in missing:
                report.add(ERROR, "11.unresolved", rel["source"], f"{label}: {ref} is not a record in this model")
            key = (source or rel["source"], rel["type"], target or rel["target"])
            if key in seen:
                report.add(ERROR, "11.duplicate-edge", key[0], f"duplicate relationship {label}")
            seen.add(key)
            if source is None or target is None or missing:
                continue  # references another model, or nothing: no local edge
            edge = Edge(source, rel["type"], target, label)
            self.edges.append(edge)
            self._out[(source, edge.type)].append(target)
            self._in[(target, edge.type)].append(source)

    def local(self, ref: str) -> str | None:
        """The local record ID a reference names, or None for another model's record."""
        if ref in self.records:
            return ref
        prefix, sep, rest = ref.partition(":")
        if sep:
            return rest if prefix == self.namespace else None
        return ref

    def type_of(self, ref: str) -> str | None:
        record = self.records.get(ref)
        return record["type"] if record else None

    def out(self, source: str, rel: str) -> list[str]:
        return self._out.get((source, rel), [])

    def into(self, target: str, rel: str) -> list[str]:
        return self._in.get((target, rel), [])

    def of_type(self, record_type: str) -> list[str]:
        return [rid for rid, r in self.records.items() if r["type"] == record_type and r["lifecycle"] != "retired"]


def check_structure(g: Graph, doc: Doc, report: Report) -> None:
    for e in g.edges:
        source_type, target_type = g.type_of(e.source) or "", g.type_of(e.target) or ""
        if ":" in e.type or ":" in source_type or ":" in target_type:
            continue  # extension vocabulary: its endpoint rules live in the extension
        if e.type in SAME_TYPE:
            if source_type != target_type:
                report.add(ERROR, "11.endpoint", e.source, f"{e.label}: {e.type} must link records of the same type")
            if e.source == e.target:
                report.add(ERROR, "11.cycle", e.source, f"{e.label}: a record cannot {e.type} itself")
            continue
        if target_type not in ENDPOINTS.get(e.type, {}).get(source_type, set()):
            message = f"{e.label}: {source_type} --{e.type}--> {target_type} is not allowed"
            report.add(ERROR, "11.endpoint", e.source, message)
    for field, items in (("record", doc.get("exceptions", [])), ("subject", doc.get("assessments", []))):
        for item in items:
            local = g.local(item[field])
            if local is not None and local not in g.records:
                report.add(ERROR, "11.unresolved", item[field], f"{field} {item[field]} is not a record in this model")
    for rel in sorted(SAME_TYPE):
        for cycle in cycles(g, rel):
            report.add(ERROR, "11.cycle", cycle[0], f"{rel} cycle: {' -> '.join([*cycle, cycle[0]])}")
    for cycle in cycles(g, "depends_on"):
        message = f"dependency cycle for review: {' -> '.join([*cycle, cycle[0]])}"
        report.add(WARNING, "11.dependency-cycle", cycle[0], message)


def cycles(g: Graph, rel: str) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan)."""
    nodes = sorted({n for e in g.edges if e.type == rel for n in (e.source, e.target)})
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in g.out(v, rel):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for v in nodes:
        if v not in index:
            visit(v)
    return found


def justified_applications(g: Graph) -> set[str]:
    """Rule 7.5: implements a Requirement or used by a Process, or depended on by a justified Application."""
    apps = g.of_type("Application")
    justified = {a for a in apps if g.out(a, "implements") or g.into(a, "uses")}
    while more := {a for a in apps if a not in justified and any(s in justified for s in g.into(a, "depends_on"))}:
        justified |= more
    return justified


def reachable_infrastructure(g: Graph, apps: set[str]) -> set[str]:
    """Rule 7.6: Infrastructure reachable through depends_on from the given Applications."""
    reached: set[str] = set()
    frontier = [t for a in apps for t in g.out(a, "depends_on") if g.type_of(t) == "Infrastructure"]
    while frontier:
        i = frontier.pop()
        if i not in reached:
            reached.add(i)
            frontier += [t for t in g.out(i, "depends_on") if g.type_of(t) == "Infrastructure"]
    return reached


def check_traceability(g: Graph, doc: Doc, report: Report) -> None:
    """Section 7. Gaps in active records are errors; gaps in proposed records are warnings."""
    outcomes = set(g.of_type("Outcome"))
    capabilities = {c for c in g.of_type("Capability") if any(t in outcomes for t in g.out(c, "contributes_to"))}
    processes = {p for p in g.of_type("Process") if any(c in capabilities for c in g.out(p, "uses_capability"))}
    business = outcomes | capabilities | processes
    requirements = {q for q in g.of_type("Requirement") if any(t in business for t in g.out(q, "supports"))}
    apps = justified_applications(g)
    infrastructure = reachable_infrastructure(g, apps)

    for rid, r in g.records.items():
        if r["lifecycle"] == "retired":
            continue
        gap = traceability_gap(g, rid, r["type"], capabilities, requirements, apps, infrastructure)
        if gap:
            rule, message = gap
            if r["lifecycle"] == "active":
                report.add(ERROR, rule, rid, message)
            else:
                report.add(WARNING, rule, rid, f"proposed record is incomplete: {message}")

    check_claims(g, doc, report)

    for e in g.edges:
        source, target = g.records[e.source], g.records[e.target]
        if e.type != "supersedes" and source["lifecycle"] == "active" and target["lifecycle"] == "retired":
            report.add(ERROR, "7.retired-target", e.source, f"active {e.type} relationship to retired {e.target}")


def traceability_gap(
    g: Graph,
    rid: str,
    record_type: str,
    capabilities: set[str],
    requirements: set[str],
    apps: set[str],
    infrastructure: set[str],
) -> tuple[str, str] | None:
    """The section 7 rule a record breaks, with the reason, or None."""
    if record_type == "Outcome" and not any(g.type_of(m) == "Measure" for m in g.into(rid, "evaluates")):
        return "7.1", "no Measure evaluates this Outcome"
    if record_type == "Capability" and rid not in capabilities:
        return "7.2", "does not contribute_to an Outcome"
    if record_type == "Process" and not any(g.type_of(c) == "Capability" for c in g.out(rid, "uses_capability")):
        return "7.3", "does not use_capability a Capability"
    if record_type == "Requirement" and not g.out(rid, "supports"):
        return "7.4", "has no supports relationship"
    if record_type == "Requirement" and rid not in requirements:
        return "7.4", "has no business path to an Outcome through supports, uses_capability and contributes_to"
    if record_type == "Application" and rid not in apps:
        return "7.5", "implements no Requirement, is used by no Process, and no justified Application depends on it"
    if (
        record_type == "Infrastructure"
        and rid not in infrastructure
        and not any(q in requirements for q in g.out(rid, "implements"))
    ):
        return (
            "7.6",
            "is not reachable through depends_on from a justified Application and implements no justified Requirement",
        )
    readers = g.into(rid, "reads") + g.into(rid, "writes")
    if record_type == "Information" and not any(g.type_of(a) == "Application" for a in readers):
        return "7.7", "no Application reads or writes it"
    return None


def evidence_kinds(assessments: list[Doc]) -> set[str]:
    return {ev["kind"] for a in assessments for ev in a["evidence"]}


def check_claims(g: Graph, doc: Doc, report: Report) -> None:
    """Rule 7.8: delivered and verified requirement claims need an implementer and evidence."""
    by_subject: dict[str, list[Doc]] = defaultdict(list)
    for a in doc.get("assessments", []):
        by_subject[g.local(a["subject"]) or a["subject"]].append(a)
    for rid, assessments in by_subject.items():
        if g.type_of(rid) != "Requirement":
            continue
        delivered = [a for a in assessments if a["dimension"] == "delivery" and a["state"] == "released"]
        verified = [a for a in assessments if a["dimension"] == "verification" and a["state"] == "passed"]
        if not (delivered or verified):
            continue
        if not g.into(rid, "implements"):
            report.add(ERROR, "7.8", rid, "claimed delivered or verified, but nothing implements it")
        if not evidence_kinds(delivered) & DELIVERY_EVIDENCE:
            message = (
                "claimed delivered or verified without implementation or deployment evidence"
                " on a released delivery assessment"
            )
            report.add(ERROR, "7.8", rid, message)
        if verified and not evidence_kinds(verified) & VERIFICATION_EVIDENCE:
            report.add(ERROR, "7.8", rid, "claimed verified without test or decision evidence")


def check_hygiene(g: Graph, doc: Doc, report: Report) -> None:
    interval = dt.timedelta(days=doc["model"]["review_interval_days"])
    for rid, r in g.records.items():
        if r["lifecycle"] == "active":
            reviewed = r["last_reviewed_at"]
            if reviewed is None:
                report.add(WARNING, "11.review-overdue", rid, "active record has never been reviewed")
            elif report.date - dt.date.fromisoformat(reviewed) > interval:
                message = f"last reviewed {reviewed}, beyond the {interval.days}-day interval"
                report.add(WARNING, "11.review-overdue", rid, message)
        if r["type"] == "Measure" and r.get("baseline") == "unknown" and r["lifecycle"] != "retired":
            report.add(
                WARNING, "11.unknown-baseline", rid, f"baseline unknown; {r.get('baseline_owner')} to collect it"
            )


def apply_exceptions(doc: Doc, report: Report) -> None:
    """A valid exception waives the error it names (rule and record); an expired one does not.

    Exceptions name rules by the codes in this report, such as 7.2 or 7.retired-target.
    """
    for exc in doc.get("exceptions", []):
        expires = dt.date.fromisoformat(exc["expires_at"])
        if expires < report.date:
            message = f"exception to {exc['rule']} expired {exc['expires_at']} and no longer applies"
            report.add(WARNING, "11.exception-expired", exc["record"], message)
            continue
        targets = [
            f
            for f in report.findings
            if f.rule == exc["rule"] and f.record == exc["record"] and f.severity == ERROR and not f.waived_by
        ]
        for f in targets:
            f.waived_by = exc
        if not targets:
            report.add(WARNING, "11.exception-unused", exc["record"], f"exception to {exc['rule']} matches no finding")
        elif (expires - report.date).days <= EXPIRY_WARNING_DAYS:
            message = f"exception to {exc['rule']} expires {exc['expires_at']}"
            report.add(WARNING, "11.exception-expiring", exc["record"], message)


def validate(doc: Any, date: dt.date) -> Report:
    """Validate a parsed product map as of `date`. The document is not modified."""
    report = Report(doc, date)
    if not check_schema(doc, report):
        return report
    report.graph_checked = True
    g = Graph(doc, report)
    check_structure(g, doc, report)
    check_traceability(g, doc, report)
    check_hygiene(g, doc, report)
    apply_exceptions(doc, report)
    return report
