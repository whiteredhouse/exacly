"""Validate an Exacly product map against the specification.

Checks the document against the JSON Schema for its spec version, then runs
the checks that need the whole map: section 11's errors and warnings and the
section 7 traceability rules. Prints a validation report (section 11) and
exits 1 when the map does not conform.

Usage:
    python validator/exacly_validate.py MAP.yaml [--date YYYY-MM-DD] [--json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import jsonschema
import yaml

SPEC = Path(__file__).resolve().parent.parent / "spec"

ERROR, WARNING = "error", "warning"
EXPIRY_WARNING_DAYS = 30

# Section 6: allowed (source type, target types) per relationship.
ENDPOINTS: dict[str, list[tuple[str, set[str]]]] = {
    "contributes_to": [("Capability", {"Outcome"})],
    "uses_capability": [("Process", {"Capability"})],
    "supports": [("Requirement", {"Process", "Capability", "Outcome"})],
    "implements": [
        ("Application", {"Requirement"}),
        ("Infrastructure", {"Requirement"}),
        ("Process", {"Requirement"}),
    ],
    "uses": [("Process", {"Application"})],
    "reads": [("Application", {"Information"})],
    "writes": [("Application", {"Information"})],
    "depends_on": [("Application", {"Application", "Infrastructure"}), ("Infrastructure", {"Infrastructure"})],
    "evaluates": [("Measure", {"Outcome"})],
}
SAME_TYPE = {"contains", "supersedes"}
DELIVERY_EVIDENCE = {"implementation", "deployment"}
VERIFICATION_EVIDENCE = {"test", "decision"}


@dataclass
class Finding:
    severity: str
    rule: str
    record: str | None
    message: str
    waived_by: dict | None = None


class Report:
    def __init__(self, doc: dict, date: dt.date) -> None:
        self.doc, self.date = doc, date
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

    def as_dict(self) -> dict:
        model = self.doc.get("model", {}) if isinstance(self.doc, dict) else {}
        return {
            "result": self.result,
            "date": self.date.isoformat(),
            "spec_version": self.doc.get("exacly") if isinstance(self.doc, dict) else None,
            "namespace": model.get("namespace"),
            "scope": model.get("scope"),
            "state": self.doc.get("state") if isinstance(self.doc, dict) else None,
            "revision": self.doc.get("revision") if isinstance(self.doc, dict) else None,
            "base_revision": self.doc.get("base_revision") if isinstance(self.doc, dict) else None,
            "graph_checked": self.graph_checked,
            "errors": [asdict(f) for f in self.errors],
            "warnings": [asdict(f) for f in self.warnings],
            "exceptions_applied": [asdict(f) for f in self.waived],
        }

    def as_text(self) -> str:
        d = self.as_dict()
        scope = d["scope"] or {}
        lines = [
            "Exacly validation report",
            f"Model: {d['namespace']}  spec {d['spec_version']}  state {d['state']}",
            f"Revision: {d['revision'] or '(not given)'}"
            + (f"  base {d['base_revision']}" if d["base_revision"] else ""),
            f"Scope: {scope.get('covers', '(not given)')}",
            f"Date: {d['date']}",
            f"Result: {d['result']}",
        ]
        if not self.graph_checked:
            lines.append("Graph checks were skipped because the document does not match the schema.")
        for title, items in (("Errors", self.errors), ("Warnings", self.warnings), ("Exceptions applied", self.waived)):
            lines.append(f"\n{title} ({len(items)})")
            for f in items:
                where = f" {f.record}" if f.record else ""
                lines.append(f"  [{f.rule}]{where}: {f.message}")
        return "\n".join(lines)


def schema_for(version: str) -> dict:
    match = re.fullmatch(r"(\d+)\.(\d+)\.\d+", str(version))
    if not match:
        raise ValueError(f"not a spec version: {version!r}")
    iterations = sorted((SPEC / "schema" / f"{match[1]}.{match[2]}").glob("*.json"))
    if not iterations:
        raise ValueError(f"no schema for spec version {version}")
    return json.loads(iterations[-1].read_text())  # the latest dated iteration is the correct one


def check_schema(doc: dict, report: Report) -> bool:
    try:
        schema = schema_for(doc.get("exacly", "") if isinstance(doc, dict) else "")
    except ValueError as error:
        report.add(ERROR, "schema", None, str(error))
        return False
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    for e in errors:
        where = "/" + "/".join(map(str, e.absolute_path))
        report.add(ERROR, "schema", None, f"{where}: {e.message}")
    return not errors


class Graph:
    def __init__(self, doc: dict, report: Report) -> None:
        self.namespace = doc["model"]["namespace"]
        self.records: dict[str, dict] = {}
        for r in doc["records"]:
            if r["id"] in self.records:
                report.add(ERROR, "11.duplicate-id", r["id"], "ID is used by more than one record")
            self.records.setdefault(r["id"], r)
        self.edges: list[dict] = doc["relationships"]

    def local(self, ref: str) -> str | None:
        """The local record ID a reference names, or None for another model's record."""
        if ref in self.records:
            return ref
        prefix, sep, rest = ref.partition(":")
        if sep:
            return rest if prefix == self.namespace else None
        return ref

    def type_of(self, ref: str) -> str | None:
        r = self.records.get(ref)
        return r["type"] if r else None

    def live(self, ref: str) -> bool:
        r = self.records.get(ref)
        return bool(r) and r["lifecycle"] != "retired"

    def out(self, source: str, rel: str) -> list[str]:
        return [e["target"] for e in self.edges if e["source"] == source and e["type"] == rel]

    def into(self, target: str, rel: str) -> list[str]:
        return [e["source"] for e in self.edges if e["target"] == target and e["type"] == rel]


def check_structure(g: Graph, doc: dict, report: Report) -> None:
    rel_ids: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    for e in g.edges:
        if "id" in e:
            if e["id"] in rel_ids or e["id"] in g.records:
                report.add(ERROR, "11.duplicate-id", e["id"], "relationship ID is already used")
            rel_ids.add(e["id"])
        label = f"{e['source']} --{e['type']}--> {e['target']}"
        ends = [g.local(e["source"]), g.local(e["target"])]
        missing = [ref for ref, local in zip((e["source"], e["target"]), ends) if local is not None and local not in g.records]
        for ref in missing:
            report.add(ERROR, "11.unresolved", e["source"], f"{label}: {ref} is not a record in this model")
        if not missing and None not in ends:
            e["source"], e["target"] = ends  # normalise namespace-qualified local references
        key = (e["source"], e["type"], e["target"])
        if key in seen:
            report.add(ERROR, "11.duplicate-edge", e["source"], f"duplicate relationship {label}")
        seen.add(key)
        if missing or None in ends:
            continue
        source_type, target_type = g.type_of(ends[0]), g.type_of(ends[1])
        if ":" in e["type"] or ":" in source_type or ":" in target_type:
            continue  # extension vocabulary: its endpoint rules live in the extension
        if e["type"] in SAME_TYPE:
            if source_type != target_type:
                report.add(ERROR, "11.endpoint", ends[0], f"{label}: {e['type']} must link records of the same type")
            if ends[0] == ends[1]:
                report.add(ERROR, "11.cycle", ends[0], f"{label}: a record cannot {e['type']} itself")
            continue
        allowed = dict(ENDPOINTS.get(e["type"], []))
        if source_type not in allowed or target_type not in allowed[source_type]:
            report.add(ERROR, "11.endpoint", ends[0], f"{label}: {source_type} --{e['type']}--> {target_type} is not allowed")
    for ref_field, items in (("record", doc.get("exceptions", [])), ("subject", doc.get("assessments", []))):
        for item in items:
            local = g.local(item[ref_field])
            if local is not None and local not in g.records:
                report.add(ERROR, "11.unresolved", item[ref_field], f"{ref_field} {item[ref_field]} is not a record in this model")
    for rel in sorted(SAME_TYPE):
        for cycle in cycles(g, rel):
            report.add(ERROR, "11.cycle", cycle[0], f"{rel} cycle: {' -> '.join(cycle + [cycle[0]])}")
    for cycle in cycles(g, "depends_on"):
        report.add(WARNING, "11.dependency-cycle", cycle[0], f"dependency cycle for review: {' -> '.join(cycle + [cycle[0]])}")


def cycles(g: Graph, rel: str) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan)."""
    nodes = sorted({e["source"] for e in g.edges if e["type"] == rel} | {e["target"] for e in g.edges if e["type"] == rel})
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


def check_traceability(g: Graph, doc: dict, report: Report) -> None:
    """Section 7. Gaps in active records are errors; gaps in proposed records are warnings."""
    of_type = lambda t: [r for r in g.records.values() if r["type"] == t and r["lifecycle"] != "retired"]

    outcomes = {r["id"] for r in of_type("Outcome")}
    capabilities = {c for c in (r["id"] for r in of_type("Capability")) if any(t in outcomes for t in g.out(c, "contributes_to"))}
    processes = {p for p in (r["id"] for r in of_type("Process")) if any(c in capabilities for c in g.out(p, "uses_capability"))}
    justified_requirements = {
        q["id"] for q in of_type("Requirement")
        if any(t in outcomes | capabilities | processes for t in g.out(q["id"], "supports"))
    }

    justified_apps = {
        a["id"] for a in of_type("Application")
        if g.out(a["id"], "implements") or g.into(a["id"], "uses")
    }
    while True:  # shared applications justified by justified dependants
        more = {
            a["id"] for a in of_type("Application")
            if a["id"] not in justified_apps and any(s in justified_apps for s in g.into(a["id"], "depends_on"))
        }
        if not more:
            break
        justified_apps |= more
    reachable_infra: set[str] = set()
    frontier = [t for a in justified_apps for t in g.out(a, "depends_on") if g.type_of(t) == "Infrastructure"]
    while frontier:
        i = frontier.pop()
        if i not in reachable_infra:
            reachable_infra.add(i)
            frontier += [t for t in g.out(i, "depends_on") if g.type_of(t) == "Infrastructure"]

    def gap(record: dict, rule: str, message: str) -> None:
        severity = ERROR if record["lifecycle"] == "active" else WARNING
        if severity == WARNING:
            message = f"proposed record is incomplete: {message}"
        report.add(severity, rule, record["id"], message)

    for r in g.records.values():
        if r["lifecycle"] == "retired":
            continue
        rid, t = r["id"], r["type"]
        if t == "Outcome" and not any(g.type_of(m) == "Measure" for m in g.into(rid, "evaluates")):
            gap(r, "7.1", "no Measure evaluates this Outcome")
        elif t == "Capability" and rid not in capabilities:
            gap(r, "7.2", "does not contribute_to an Outcome")
        elif t == "Process" and not any(g.type_of(c) == "Capability" for c in g.out(rid, "uses_capability")):
            gap(r, "7.3", "does not use_capability a Capability")
        elif t == "Requirement":
            if not g.out(rid, "supports"):
                gap(r, "7.4", "has no supports relationship")
            elif rid not in justified_requirements:
                gap(r, "7.4", "has no business path to an Outcome through supports, uses_capability and contributes_to")
        elif t == "Application" and rid not in justified_apps:
            gap(r, "7.5", "implements no Requirement, is used by no Process, and no justified Application depends on it")
        elif t == "Infrastructure" and rid not in reachable_infra and not any(q in justified_requirements for q in g.out(rid, "implements")):
            gap(r, "7.6", "is not reachable through depends_on from a justified Application and implements no justified Requirement")
        elif t == "Information" and not any(g.type_of(a) == "Application" for a in g.into(rid, "reads") + g.into(rid, "writes")):
            gap(r, "7.7", "no Application reads or writes it")

    check_claims(g, doc, report)

    for e in g.edges:
        source, target = g.records.get(e["source"]), g.records.get(e["target"])
        if (
            e["type"] != "supersedes"
            and source and target
            and source["lifecycle"] == "active"
            and target["lifecycle"] == "retired"
        ):
            report.add(ERROR, "7.retired-target", source["id"], f"active {e['type']} relationship to retired {target['id']}")


def check_claims(g: Graph, doc: dict, report: Report) -> None:
    """Rule 7.8: delivered and verified requirement claims need an implementer and evidence."""
    by_subject: dict[str, list[dict]] = {}
    for a in doc.get("assessments", []):
        by_subject.setdefault(g.local(a["subject"]) or a["subject"], []).append(a)
    for rid, assessments in by_subject.items():
        if g.type_of(rid) != "Requirement":
            continue
        delivered = [a for a in assessments if a["dimension"] == "delivery" and a["state"] == "released"]
        verified = [a for a in assessments if a["dimension"] == "verification" and a["state"] == "passed"]
        if not (delivered or verified):
            continue
        kinds = lambda items: {ev["kind"] for a in items for ev in a["evidence"]}
        if not g.into(rid, "implements"):
            report.add(ERROR, "7.8", rid, "claimed delivered or verified, but nothing implements it")
        if not kinds(delivered) & DELIVERY_EVIDENCE:
            report.add(ERROR, "7.8", rid, "claimed delivered or verified without implementation or deployment evidence on a released delivery assessment")
        if verified and not kinds(verified) & VERIFICATION_EVIDENCE:
            report.add(ERROR, "7.8", rid, "claimed verified without test or decision evidence")


def check_hygiene(g: Graph, doc: dict, report: Report) -> None:
    interval = dt.timedelta(days=doc["model"]["review_interval_days"])
    for r in g.records.values():
        if r["lifecycle"] != "active":
            continue
        reviewed = r["last_reviewed_at"]
        if reviewed is None:
            report.add(WARNING, "11.review-overdue", r["id"], "active record has never been reviewed")
        elif report.date - dt.date.fromisoformat(reviewed) > interval:
            report.add(WARNING, "11.review-overdue", r["id"], f"last reviewed {reviewed}, beyond the {interval.days}-day interval")
    for r in g.records.values():
        if r["type"] == "Measure" and r.get("baseline") == "unknown" and r["lifecycle"] != "retired":
            report.add(WARNING, "11.unknown-baseline", r["id"], f"baseline unknown; {r.get('baseline_owner')} to collect it")


def apply_exceptions(doc: dict, report: Report) -> None:
    """A valid exception waives the error it names (rule and record); an expired one does not.

    Exceptions name rules by the codes in this report, such as 7.2 or 7.retired-target.
    """
    for exc in doc.get("exceptions", []):
        expires = dt.date.fromisoformat(exc["expires_at"])
        targets = [
            f for f in report.findings
            if f.rule == exc["rule"] and f.record == exc["record"] and f.severity == ERROR and not f.waived_by
        ]
        if expires < report.date:
            report.add(WARNING, "11.exception-expired", exc["record"], f"exception to {exc['rule']} expired {exc['expires_at']} and no longer applies")
            continue
        for f in targets:
            f.waived_by = exc
        if not targets:
            report.add(WARNING, "11.exception-unused", exc["record"], f"exception to {exc['rule']} matches no finding")
        elif (expires - report.date).days <= EXPIRY_WARNING_DAYS:
            report.add(WARNING, "11.exception-expiring", exc["record"], f"exception to {exc['rule']} expires {exc['expires_at']}")


def validate(doc: dict, date: dt.date) -> Report:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("map", type=Path, help="product map, YAML or JSON")
    parser.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today(), help="report date (default: today)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = validate(yaml.safe_load(args.map.read_text()), args.date)
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.as_text())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
