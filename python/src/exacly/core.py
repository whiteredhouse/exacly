"""Validate an Exacly model against the specification.

A 0.1 model is one product map. A 0.2 model is config documents, which people
author, plus state documents, which tooling writes (section 9). Each document
is checked against the JSON Schema for its spec version and kind, then the
checks that need the whole model run: section 11's errors and warnings and the
section 7 traceability rules. The result is a validation report (section 11).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from collections.abc import Mapping
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
# From 0.2: Infrastructure stores Information, in a role (section 6).
ENDPOINTS_0_2 = {**ENDPOINTS, "stores": {"Infrastructure": {"Information"}}}
SAME_TYPE = {"contains", "supersedes"}
PARTS = ("records", "relationships", "exceptions")  # The parts of config that several documents may share.
DELIVERY_EVIDENCE = {"implementation", "deployment"}
VERIFICATION_EVIDENCE = {"test", "decision"}
# The binding fields that tell a record's bindings of one source apart, where the source's observer defines them.
BINDING_IDENTITY = {"tests": ("workflow", "job")}

Doc = dict[str, Any]


@dataclass
class Finding:
    severity: str
    rule: str
    record: str | None
    message: str
    waived_by: dict[str, Any] | None = None
    document: str | None = None


class Report:
    """Findings about a model. `doc` carries the metadata the report names: version, model boundary, revision."""

    def __init__(self, doc: Any, date: dt.date, documents: list[str] | None = None) -> None:
        self.doc: Doc = doc if isinstance(doc, dict) else {}
        self.date = date
        self.documents = documents or []
        self.states: list[dict[str, Any]] = []
        self.findings: list[Finding] = []
        self.graph_checked = False

    def add(self, severity: str, rule: str, record: str | None, message: str, document: str | None = None) -> None:
        self.findings.append(Finding(severity, rule, record, message, document=document))

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
        model = self.doc.get("model")
        model = model if isinstance(model, dict) else {}
        return {
            "result": self.result,
            "date": self.date.isoformat(),
            "spec_version": self.doc.get("exacly"),
            "namespace": model.get("namespace"),
            "scope": model.get("scope"),
            "state": self.doc.get("state"),
            "revision": self.doc.get("revision"),
            "base_revision": self.doc.get("base_revision"),
            "documents": self.documents,
            "states": self.states,
            "graph_checked": self.graph_checked,
            "errors": [asdict(f) for f in self.errors],
            "warnings": [asdict(f) for f in self.warnings],
            "exceptions_applied": [asdict(f) for f in self.waived],
        }

    def as_text(self) -> str:
        d = self.as_dict()
        covers = d["scope"].get("covers") if isinstance(d["scope"], dict) else None
        base = f"  base {d['base_revision']}" if d["base_revision"] else ""
        state = f"  state {d['state']}" if d["state"] else ""
        lines = [
            "Exacly validation report",
            f"Model: {d['namespace']}  spec {d['spec_version']}{state}",
            f"Revision: {d['revision'] or '(not given)'}{base}",
            f"Scope: {covers or '(not given)'}",
        ]
        if len(self.documents) > 1:
            lines.append(f"Documents: {', '.join(self.documents)}")
        for s in self.states:
            lines.append(
                f"State: {s['context']}, serial {s['serial']}, config {s['config_revision']} ({s['document']})"
            )
        lines += [f"Date: {d['date']}", f"Result: {d['result']}"]
        if not self.graph_checked:
            lines.append("Graph checks were skipped because the document does not match the schema.")
        sections = (("Errors", self.errors), ("Warnings", self.warnings), ("Exceptions applied", self.waived))
        for title, items in sections:
            lines.append(f"\n{title} ({len(items)})")
            for f in items:
                where = f" {f.record}" if f.record else ""
                where += f" in {f.document}" if f.document and len(self.documents) > 1 else ""
                lines.append(f"  [{f.rule}]{where}: {f.message}")
        return "\n".join(lines)


def schema_dirs() -> list[Path]:
    """Schemas bundled in the installed package, then the repository's spec/schema."""
    bundled = Path(str(resources.files("exacly") / "schemas"))
    return [bundled, Path(__file__).resolve().parents[3] / "spec" / "schema"]


def minor_version(version: Any) -> str | None:
    """'0.2' for '0.2.1', or None when it is not a spec version."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.\d+", str(version))
    return f"{match[1]}.{match[2]}" if match else None


def version_of(doc: Any) -> Any:
    return doc.get("exacly") if isinstance(doc, dict) else None


def is_state(doc: Any) -> bool:
    """A 0.2 state document, told from config by the fields only state has."""
    return isinstance(doc, dict) and any(k in doc for k in ("config_revision", "lineage", "serial", "context"))


def schema_for(version: str, kind: str | None = None) -> dict[str, Any]:
    """The schema for a spec version. From 0.2, each document kind ("config" or "state") has its own."""
    minor = minor_version(version)
    if not minor:
        raise ValueError(f"not a spec version: {version!r}")
    folder = f"{minor}/{kind}" if kind else minor
    for root in schema_dirs():
        iterations = sorted((root / folder).glob("*.json"))
        if iterations:
            # The latest dated iteration is the correct one.
            schema: dict[str, Any] = json.loads(iterations[-1].read_text())
            return schema
    raise ValueError(f"no {kind + ' ' if kind else ''}schema for spec version {version}")


def check_schema(doc: Any, report: Report, kind: str | None = None, document: str | None = None) -> bool:
    try:
        schema = schema_for(version_of(doc) or "", kind)
    except ValueError as error:
        report.add(ERROR, "schema", None, str(error), document)
        return False
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    for e in errors:
        where = "/" + "/".join(str(p) for p in e.absolute_path)
        report.add(ERROR, "schema", None, f"{where}: {e.message}", document)
    return not errors


@dataclass(frozen=True)
class Edge:
    source: str
    type: str
    target: str
    label: str
    role: str | None = None
    document: str | None = None


class Graph:
    """The map's records and resolved relationships, indexed once. The document is not changed.

    `sources` maps each record, relationship and exception (by object identity) to the document it came from,
    when the model spans several documents.
    """

    def __init__(self, doc: Doc, report: Report, sources: dict[int, str] | None = None) -> None:
        self.namespace: str = doc["model"]["namespace"]
        self.sources = sources or {}
        self.records: dict[str, Doc] = {}
        for r in doc["records"]:
            if r["id"] in self.records:
                message = "ID is used by more than one record"
                report.add(ERROR, "11.duplicate-id", r["id"], message, self.document_of(r))
            self.records.setdefault(r["id"], r)
        self.edges: list[Edge] = []
        self.relationship_ids: set[str] = set()
        self.keys: set[tuple[str, str, str]] = set()
        self._out: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._in: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._resolve(doc["relationships"], report)

    def _resolve(self, relationships: list[Doc], report: Report) -> None:
        rel_ids, seen = self.relationship_ids, self.keys
        for rel in relationships:
            label = f"{rel['source']} --{rel['type']}--> {rel['target']}"
            if "id" in rel:
                if rel["id"] in rel_ids or rel["id"] in self.records:
                    message = "relationship ID is already used"
                    report.add(ERROR, "11.duplicate-id", rel["id"], message, self.document_of(rel))
                rel_ids.add(rel["id"])
            source, target = self.local(rel["source"]), self.local(rel["target"])
            missing = [
                ref
                for ref, local in ((rel["source"], source), (rel["target"], target))
                if local is not None and local not in self.records
            ]
            for ref in missing:
                message = f"{label}: {ref} is not a record in this model"
                report.add(ERROR, "11.unresolved", source or rel["source"], message, self.document_of(rel))
            key = self.key(rel)
            if key in seen:
                report.add(ERROR, "11.duplicate-edge", key[0], f"duplicate relationship {label}", self.document_of(rel))
            seen.add(key)
            if source is None or target is None or missing:
                continue  # references another model, or nothing: no local edge
            edge = Edge(source, rel["type"], target, label, rel.get("role"), self.document_of(rel))
            self.edges.append(edge)
            self._out[(source, edge.type)].append(target)
            self._in[(target, edge.type)].append(source)

    def document_of(self, item: Doc) -> str | None:
        return self.sources.get(id(item))

    def key(self, rel: Doc) -> tuple[str, str, str]:
        """A relationship's identity as (source, type, target), with references made local where they can be."""
        return (self.local(rel["source"]) or rel["source"], rel["type"], self.local(rel["target"]) or rel["target"])

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


def check_structure(g: Graph, doc: Doc, report: Report, endpoints: dict[str, dict[str, set[str]]] = ENDPOINTS) -> None:
    for e in g.edges:
        source_type, target_type = g.type_of(e.source) or "", g.type_of(e.target) or ""
        if ":" in e.type or ":" in source_type or ":" in target_type:
            continue  # extension vocabulary: its endpoint rules live in the extension
        if e.type in SAME_TYPE:
            if source_type != target_type:
                message = f"{e.label}: {e.type} must link records of the same type"
                report.add(ERROR, "11.endpoint", e.source, message, e.document)
            continue
        if target_type not in endpoints.get(e.type, {}).get(source_type, set()):
            message = f"{e.label}: {source_type} --{e.type}--> {target_type} is not allowed"
            report.add(ERROR, "11.endpoint", e.source, message, e.document)
    for field, items in (("record", doc.get("exceptions", [])), ("subject", doc.get("assessments", []))):
        for item in items:
            local = g.local(item[field])
            if local is not None and local not in g.records:
                message = f"{field} {item[field]} is not a record in this model"
                report.add(ERROR, "11.unresolved", local, message, g.document_of(item))
    for rel in sorted(SAME_TYPE):
        for cycle in cycles(g, rel):
            report.add(ERROR, "11.cycle", cycle[0], f"{rel} cycle: {' -> '.join([*cycle, cycle[0]])}")
    for cycle in cycles(g, "depends_on"):
        message = f"dependency cycle for review: {' -> '.join([*cycle, cycle[0]])}"
        report.add(WARNING, "11.dependency-cycle", cycle[0], message)


def cycles(g: Graph, rel: str) -> list[list[str]]:
    """Strongly connected components that contain a cycle, including a record linked to itself (Tarjan)."""
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
            if len(component) > 1 or v in g.out(v, rel):
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


def check_traceability(g: Graph, assessments: list[Doc], report: Report) -> None:
    """Section 7. Gaps in active records are errors; gaps in proposed records are warnings."""
    outcomes = set(g.of_type("Outcome"))
    # Rule 7.1 excludes retired Measures from 0.2; keep the 0.1 link rule unchanged.
    measures = {
        rid
        for rid, r in g.records.items()
        if r["type"] == "Measure" and (minor_version(version_of(report.doc)) == "0.1" or r["lifecycle"] != "retired")
    }
    capabilities = {c for c in g.of_type("Capability") if any(t in outcomes for t in g.out(c, "contributes_to"))}
    processes = {p for p in g.of_type("Process") if any(c in capabilities for c in g.out(p, "uses_capability"))}
    business = outcomes | capabilities | processes
    requirements = {q for q in g.of_type("Requirement") if any(t in business for t in g.out(q, "supports"))}
    apps = justified_applications(g)
    infrastructure = reachable_infrastructure(g, apps)

    for rid, r in g.records.items():
        if r["lifecycle"] == "retired":
            continue
        gap = traceability_gap(g, rid, r["type"], measures, capabilities, requirements, apps, infrastructure)
        if gap:
            rule, message = gap
            if r["lifecycle"] == "active":
                report.add(ERROR, rule, rid, message)
            else:
                report.add(WARNING, rule, rid, f"proposed record is incomplete: {message}")

    check_claims(g, assessments, report)

    for e in g.edges:
        source, target = g.records[e.source], g.records[e.target]
        if e.type != "supersedes" and source["lifecycle"] == "active" and target["lifecycle"] == "retired":
            message = f"active {e.type} relationship to retired {e.target}"
            report.add(ERROR, "7.retired-target", e.source, message, e.document)


def traceability_gap(
    g: Graph,
    rid: str,
    record_type: str,
    measures: set[str],
    capabilities: set[str],
    requirements: set[str],
    apps: set[str],
    infrastructure: set[str],
) -> tuple[str, str] | None:
    """The section 7 rule a record breaks, with the reason, or None."""
    if record_type == "Outcome" and not any(m in measures for m in g.into(rid, "evaluates")):
        return "7.1", "no eligible Measure evaluates this Outcome"
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


def bindings(record: Doc, source: str) -> list[Doc]:
    """A record's bindings for one source: a binding, or a list of them (section 5)."""
    found = record.get("observe", {}).get(source)
    return [] if not found else found if isinstance(found, list) else [found]


def identity(source: str, binding: Doc) -> Doc:
    """What identifies a binding among its record's bindings of the same source: the fields its observer defines, such
    as a tests binding's workflow and job, or its whole content for a source whose observer defines none,
    such as runtime, measurement or an extension source (section 5)."""
    if source not in BINDING_IDENTITY:
        return dict(binding)
    return {key: str(binding[key]) for key in BINDING_IDENTITY[source] if binding.get(key)}


def binding_words(binding: Doc) -> str:
    """A binding's identifying values as words, such as ".github/workflows/ci.yml test"."""
    return " ".join(
        value if isinstance(value, str) else ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        for value in binding.values()
    )


def check_bindings(g: Graph, report: Report) -> None:
    """Section 5: a record's bindings of one source must be told apart, by their identifying fields where the
    source defines them, otherwise by their whole content."""
    for rid, r in g.records.items():
        for source in r.get("observe", {}):
            keys = [identity(source, b) for b in bindings(r, source)]
            repeated = [key for i, key in enumerate(keys) if key in keys[:i]]
            for key in repeated:
                named = ", ".join(f"{k} {v}" for k, v in key.items()) or "no identifying fields"
                report.add(ERROR, "11.duplicate-binding", rid, f"two {source} bindings with the same {named}")


def check_extensions(g: Graph, model: Doc, assessments: list[Doc], report: Report) -> None:
    """A declared extension's bindings and the fields it writes on assessments, against the schemas it ships (section
    5: an extension defines its fields)."""
    from exacly import manual_qa  # The extension builds on this module.

    if manual_qa.EXTENSION not in (model.get("extensions") or []):
        return
    for rid, r in g.records.items():
        for binding in bindings(r, manual_qa.SOURCE):
            for problem in manual_qa.problems(binding, manual_qa.BINDING_SCHEMA):
                report.add(ERROR, "11.extension-binding", rid, f"{manual_qa.SOURCE} binding {problem}")
    for a in assessments:
        if a.get("source") != manual_qa.SOURCE:
            continue
        for problem in manual_qa.problems(a, manual_qa.ASSESSMENT_SCHEMA):
            message = f"{manual_qa.SOURCE} assessment in {a['context']} {problem}"
            report.add(ERROR, "11.extension-assessment", g.local(a["subject"]) or a["subject"], message, a["document"])


def check_storage(g: Graph, report: Report) -> None:
    """Rule 7.9: active Information SHOULD have exactly one system of record, so a gap is a warning."""
    for rid in g.of_type("Information"):
        count = sum(1 for e in g.edges if e.type == "stores" and e.target == rid and e.role == "system_of_record")
        if count == 1:
            continue
        message = "no Infrastructure stores it as system of record" if not count else f"{count} systems of record"
        if g.records[rid]["lifecycle"] != "active":
            message = f"proposed record is incomplete: {message}"
        report.add(WARNING, "7.9", rid, message)


def expired(evidence: Doc, date: dt.date) -> bool:
    """After its expiry date a reference no longer supports its claim (section 8)."""
    return "expires_at" in evidence and dt.date.fromisoformat(evidence["expires_at"]) < date


def supported(assessment: Doc, kinds: set[str], date: dt.date) -> bool:
    """Whether the assessment carries unexpired evidence of one of these kinds that applies to its own context.

    Each claim stands on its own evidence: assessments do not carry forward to another revision or environment.
    """
    return any(
        ev["kind"] in kinds and ev["applies_to"] == assessment["context"] and not expired(ev, date)
        for ev in assessment["evidence"]
    )


def check_claims(g: Graph, assessments: list[Doc], report: Report) -> None:
    """Rule 7.8: delivered and verified requirement claims need an implementer and evidence."""
    by_subject: dict[str, list[Doc]] = defaultdict(list)
    for a in assessments:
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
        for a in delivered:
            if not supported(a, DELIVERY_EVIDENCE, report.date):
                message = f"claimed released in {a['context']} without implementation or deployment evidence for it"
                report.add(ERROR, "7.8", rid, message, a.get("document"))
        for a in verified:
            if not supported(a, VERIFICATION_EVIDENCE, report.date):
                message = f"claimed verified in {a['context']} without test or decision evidence for it"
                report.add(ERROR, "7.8", rid, message, a.get("document"))
        if verified and not any(supported(a, DELIVERY_EVIDENCE, report.date) for a in delivered):
            message = "claimed verified, but no released delivery assessment has implementation or deployment evidence"
            report.add(ERROR, "7.8", rid, message, verified[0].get("document"))


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


def apply_exceptions(g: Graph, doc: Doc, report: Report) -> None:
    """A valid exception waives the error it names (rule and record); an expired one does not.

    Exceptions name rules by the codes in this report, such as 7.2 or 7.retired-target.
    """
    for exc in doc.get("exceptions", []):
        record = g.local(exc["record"]) or exc["record"]
        expires = dt.date.fromisoformat(exc["expires_at"])
        if expires < report.date:
            message = f"exception to {exc['rule']} expired {exc['expires_at']} and no longer applies"
            report.add(WARNING, "11.exception-expired", record, message, g.document_of(exc))
            continue
        targets = [
            f
            for f in report.findings
            if f.rule == exc["rule"] and f.record == record and f.severity == ERROR and not f.waived_by
        ]
        for f in targets:
            f.waived_by = exc
        if not targets:
            message = f"exception to {exc['rule']} matches no finding"
            report.add(WARNING, "11.exception-unused", record, message, g.document_of(exc))
        elif (expires - report.date).days <= EXPIRY_WARNING_DAYS:
            message = f"exception to {exc['rule']} expires {exc['expires_at']}"
            report.add(WARNING, "11.exception-expiring", record, message, g.document_of(exc))


GIT_HASH = re.compile(r"[0-9a-f]{7,64}")


def revisions_match(a: str, b: str) -> bool:
    """Equal, or both Git commit hashes and one abbreviates the other. Other revision names compare exactly,
    so release-1 does not match release-10."""
    short, full = sorted((a, b), key=len)
    if short == full:
        return True
    return bool(GIT_HASH.fullmatch(short) and GIT_HASH.fullmatch(full)) and full.startswith(short)


def check_states(g: Graph, states: dict[str, Doc], revision: str | None, report: Report) -> list[Doc]:
    """Section 9 and 11 checks on state documents. Returns the assessments of the states that were read,
    each with its record as `subject`, for the section 7 rules."""
    assessments: list[Doc] = []
    contexts: set[str] = set()
    for name, state in states.items():
        if state["namespace"] != g.namespace:
            message = f"state is for model {state['namespace']}, not {g.namespace}, so it was not read"
            report.add(ERROR, "11.state-namespace", None, message, name)
            continue
        if state["context"] in contexts:
            message = f"another state document is already for context {state['context']}, so it was not read"
            report.add(ERROR, "11.state-context", None, message, name)
            continue
        contexts.add(state["context"])
        report.states.append(
            {
                "document": name,
                "context": state["context"],
                "serial": state["serial"],
                "config_revision": state["config_revision"],
            }
        )
        if revision and not revisions_match(state["config_revision"], revision):
            message = f"computed against config {state['config_revision']}, not {revision}"
            report.add(WARNING, "11.state-revision", None, message, name)

        for ref, observed in state["records"].items():
            rid = g.local(ref)
            if rid is None or rid not in g.records:
                report.add(
                    ERROR, "11.unresolved", ref, f"state observes {ref}, which is not a record in this model", name
                )
                continue
            for a in observed.get("assessments", []):
                assessments.append({**a, "subject": rid, "document": name})
                check_expiry(a["evidence"], rid, name, report)
            check_expiry(observed.get("evidence", []), rid, name, report)
        for rel in state.get("relationships", []):
            if "id" in rel:
                known, label, record = rel["id"] in g.relationship_ids, rel["id"], rel["id"]
            else:
                key = g.key(rel)
                known, label, record = key in g.keys, f"{rel['source']} --{rel['type']}--> {rel['target']}", key[0]
            if not known:
                report.add(
                    ERROR,
                    "11.unresolved",
                    record,
                    f"state observes {label}, which is not a relationship in this model",
                    name,
                )
                continue
            check_expiry(rel["evidence"], record, name, report)
        for work in state.get("pending", []):
            rid = g.local(work["record"])
            if rid is None or rid not in g.records:
                message = f"pending work {work['task']} is for {work['record']}, which is not a record in this model"
                report.add(ERROR, "11.unresolved", work["record"], message, name)
    return assessments


def check_expiry(evidence: list[Doc], record: str, document: str, report: Report) -> None:
    for ev in evidence:
        if expired(ev, report.date):
            message = f"evidence {ev['locator']} expired {ev['expires_at']} and no longer supports its claim"
            report.add(WARNING, "11.evidence-expired", record, message, document)


def validate(doc: Any, date: dt.date) -> Report:
    """Validate one parsed document as of `date`: a 0.1 product map, or 0.2 config in a single document.
    The document is not modified."""
    if minor_version(version_of(doc)) == "0.1":
        return validate_map(doc, date)
    return validate_documents({"document": doc}, date)


def validate_map(doc: Any, date: dt.date) -> Report:
    """Validate a 0.1 product map as of `date`."""
    report = Report(doc, date)
    if not check_schema(doc, report):
        return report
    report.graph_checked = True
    g = Graph(doc, report)
    check_structure(g, doc, report)
    check_traceability(g, doc.get("assessments", []), report)
    check_hygiene(g, doc, report)
    apply_exceptions(g, doc, report)
    return report


def validate_documents(documents: Mapping[str, Any], date: dt.date, revision: str | None = None) -> Report:
    """Validate a model given as named documents, as of `date`: 0.2 config and state documents, or one 0.1 map.

    `revision` is the config revision being validated, such as a Git commit; state computed against another
    revision is reported. No document is modified.
    """
    names = sorted(documents)
    if len(names) == 1 and minor_version(version_of(documents[names[0]])) == "0.1":
        return validate_map(documents[names[0]], date)
    configs = {n: documents[n] for n in names if not is_state(documents[n])}
    states = {n: documents[n] for n in names if is_state(documents[n])}
    boundaries = [n for n, d in configs.items() if isinstance(d, dict) and "model" in d]
    version = version_of(configs[boundaries[0]]) if boundaries else next(map(version_of, configs.values()), None)
    model = configs[boundaries[0]].get("model") if boundaries else None
    report = Report({"exacly": version, "model": model, "revision": revision}, date, names)

    valid = True
    for name in names:
        doc = documents[name]
        if minor_version(version_of(doc)) == "0.1":
            report.add(
                ERROR, "schema", None, "a 0.1 product map is one document; it cannot be combined with others", name
            )
            valid = False
        elif not check_schema(doc, report, "state" if name in states else "config", name):
            valid = False
        elif version_of(doc) != version:
            message = f"declares spec version {version_of(doc)}, but the model declares {version}"
            report.add(ERROR, "11.version-mismatch", None, message, name)
            valid = False
    if not boundaries:
        report.add(ERROR, "11.model-boundary", None, "no config document declares the model boundary")
    if len(boundaries) > 1:
        for name in boundaries:
            message = f"declares the model boundary, as {len(boundaries) - 1} other config document(s) also do"
            report.add(ERROR, "11.model-boundary", None, message, name)
    if not valid or len(boundaries) != 1:
        return report

    report.graph_checked = True
    merged: Doc = {"model": model, "records": [], "relationships": [], "exceptions": []}
    origin: dict[str, str] = {}
    sources = {id(item): name for name, doc in configs.items() for part in PARTS for item in doc.get(part, [])}
    for name, doc in configs.items():
        for part in PARTS:
            merged[part] += doc.get(part, [])
        for r in doc.get("records", []):
            origin.setdefault(r["id"], name)
            check_expiry(r["evidence"], r["id"], name, report)
        for rel in doc.get("relationships", []):
            check_expiry(rel.get("evidence", []), rel["source"], name, report)
    g = Graph(merged, report, sources)
    check_structure(g, merged, report, ENDPOINTS_0_2)
    assessments = check_states(g, states, revision, report)
    check_traceability(g, assessments, report)
    check_storage(g, report)
    check_bindings(g, report)
    check_extensions(g, merged["model"], assessments, report)
    check_hygiene(g, merged, report)
    apply_exceptions(g, merged, report)
    for f in report.findings:
        if f.document is None and f.record in origin:
            f.document = origin[f.record]
    return report
