"""Questions about a model, answered from its config and state: the shared core behind the CLI and other interfaces.

- `show`: one record, its relationships, its state in each context, and the findings about it.
- `trace`: why a record exists, up to the Outcomes it serves, and how it is realized, down to what runs it.
- `impact`: what a change to some records affects, through the records that rely on them.
- `gaps`: what is missing, by section 7, section 8 and section 10's coverage view.
- `changes`: the records and relationships that differ between two revisions of a model's config.

Every answer is plain data (dicts and lists), so an interface can print it as text or JSON.
"""

from __future__ import annotations

import datetime as dt
import difflib
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from exacly.core import PARTS, Edge, Graph, Report, is_state, validate_documents

Doc = dict[str, Any]

# Which end of each relationship is the reason for the other. Following it from a record leads up to why the
# record exists, ending at Outcomes; following it backwards leads down to how the record is realized.
# For example a Requirement supports a Process, so the Process is why; an Application reads Information, so the
# Application is why the Information is modelled; Infrastructure stores Information, so the Information is why.
WHY_IS_TARGET = {"contributes_to", "uses_capability", "supports", "implements", "evaluates", "stores"}
WHY_IS_SOURCE = {"uses", "reads", "writes", "depends_on", "contains"}
DIMENSIONS = ("delivery", "verification", "operation", "outcome_evaluation")


class QueryError(Exception):
    """The model can't answer: it doesn't load, or a record or context doesn't exist."""


@dataclass
class Model:
    """A 0.2 model ready for questions: its graph, its validation report, and its state by context."""

    graph: Graph
    report: Report
    states: dict[str, Doc] = field(default_factory=dict)
    relationships: list[Doc] = field(default_factory=list)  # As authored, including references to other models

    @property
    def records(self) -> dict[str, Doc]:
        return self.graph.records

    def record(self, rid: str) -> Doc:
        local = self.graph.local(rid)
        if local is None or local not in self.records:
            close = difflib.get_close_matches(rid, list(self.records), n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise QueryError(f"{rid} is not a record in this model{hint}")
        return self.records[local]

    def assessments(self, rid: str) -> dict[str, list[Doc]]:
        """The record's assessments in each context that has any."""
        found = {}
        for context, state in self.states.items():
            for ref, observed in state["records"].items():
                if self.graph.local(ref) == rid and observed.get("assessments"):
                    found[context] = observed["assessments"]
        return found

    def status(self, rid: str) -> dict[str, dict[str, str]]:
        """The state of each assessed dimension, by context: {"main": {"verification": "passed"}}."""
        return {
            context: {a["dimension"]: a["state"] for a in assessments}
            for context, assessments in self.assessments(rid).items()
        }

    def summary(self, rid: str) -> Doc:
        r = self.records[rid]
        return {"id": rid, "type": r["type"], "name": r["name"], "lifecycle": r["lifecycle"]}

    def why(self, rid: str) -> list[tuple[Edge, str]]:
        """The relationships that give this record a reason to exist, each with the record at the other end."""
        return [(e, e.target) for e in self.graph.edges if e.source == rid and e.type in WHY_IS_TARGET] + [
            (e, e.source) for e in self.graph.edges if e.target == rid and e.type in WHY_IS_SOURCE
        ]

    def how(self, rid: str) -> list[tuple[Edge, str]]:
        """The relationships through which other records realize or serve this one."""
        return [(e, e.source) for e in self.graph.edges if e.target == rid and e.type in WHY_IS_TARGET] + [
            (e, e.target) for e in self.graph.edges if e.source == rid and e.type in WHY_IS_SOURCE
        ]


def empty(namespace: str) -> Model:
    """A model with nothing in it, such as the base of a change that adds the whole model."""
    doc: Doc = {"model": {"namespace": namespace}, "records": [], "relationships": []}
    return Model(Graph(doc, Report(doc, dt.date.today())), Report(doc, dt.date.today()))


def load(documents: Mapping[str, Any], date: dt.date | None = None, revision: str | None = None) -> Model:
    """A model from named 0.2 config and state documents. Raises QueryError when it doesn't pass the schema checks."""
    report = validate_documents(documents, date or dt.date.today(), revision)
    if not report.graph_checked:
        problems = "; ".join(f"{f.document or 'model'}: {f.message}" for f in report.errors[:3])
        raise QueryError(f"the model can't be read: {problems}")
    configs = {n: d for n, d in sorted(documents.items()) if not is_state(d)}
    merged: Doc = {"model": report.doc["model"], **{part: [] for part in PARTS}}
    for doc in configs.values():
        for part in PARTS:
            merged[part] += doc.get(part, [])
    sources = {id(item): name for name, doc in configs.items() for part in PARTS for item in doc.get(part, [])}
    graph = Graph(merged, Report(merged, report.date), sources)
    read = {s["document"] for s in report.states}
    states = {d["context"]: d for n, d in documents.items() if n in read}
    return Model(graph, report, states, merged["relationships"])


def finding(f: Any) -> Doc:
    return {k: v for k, v in asdict(f).items() if v is not None}


def show(model: Model, rid: str) -> Doc:
    """Everything the model says about one record."""
    record = model.record(rid)
    rid = record["id"]
    relationships = [
        {
            "direction": "out" if e.source == rid else "in",
            "type": e.type,
            **({"role": e.role} if e.role else {}),
            "record": model.summary(e.target if e.source == rid else e.source),
        }
        for e in model.graph.edges
        if rid in (e.source, e.target)
    ]
    observed = {}
    for context, state in model.states.items():
        for ref, entry in state["records"].items():
            if model.graph.local(ref) == rid:
                observed[context] = {"config_revision": state["config_revision"], **entry}
        pending = [w for w in state.get("pending", []) if model.graph.local(w["record"]) == rid]
        if pending:
            observed.setdefault(context, {"config_revision": state["config_revision"]})["pending"] = pending
    return {
        "record": record,
        "document": model.graph.document_of(record),
        "relationships": relationships,
        "state": observed,
        "findings": [finding(f) for f in model.report.findings if f.record == rid],
    }


def tree(model: Model, rid: str, step: str, seen: set[str], via: Doc | None = None) -> Doc:
    """The records reached from `rid` by repeatedly following `step` ("why" or "how"). Each node's `via` is the
    relationship from its parent, whose direction is `out` when the parent is its source. A record reached twice
    is listed again, marked `repeated`, without its branches, so cycles and shared parents stay finite."""
    node: Doc = {**model.summary(rid), **({"via": via} if via else {})}
    status = model.status(rid)
    if status:
        node["status"] = status
    if rid in seen:
        return {**node, "repeated": True}
    seen.add(rid)
    branches = getattr(model, step)(rid)
    children = [
        tree(
            model,
            other,
            step,
            seen,
            {"type": e.type, "direction": "out" if e.source == rid else "in", **({"role": e.role} if e.role else {})},
        )
        for e, other in sorted(branches, key=lambda b: (b[0].type, b[1]))
    ]
    return {**node, "children": children} if children else node


def trace(model: Model, rid: str) -> Doc:
    """Why a record exists (up to Outcomes) and how it is realized (down to what runs it)."""
    rid = model.record(rid)["id"]
    return {"why": tree(model, rid, "why", set()), "how": tree(model, rid, "how", set())}


def reached(model: Model, starts: Iterable[str], step: str) -> dict[str, list[str]]:
    """Every record reachable from `starts` by `step`, with the shortest path to it (as record IDs)."""
    paths = {rid: [rid] for rid in starts}
    queue = deque(paths)
    while queue:
        rid = queue.popleft()
        for _, other in getattr(model, step)(rid):
            if other not in paths:
                paths[other] = [*paths[rid], other]
                queue.append(other)
    return paths


def impact(model: Model, changed: Iterable[str]) -> Doc:
    """What a change to `changed` records affects: every record that relies on them, directly or through others,
    with the path that links it; and the Requirements whose verification to check again, with their bindings."""
    ids = sorted({model.record(rid)["id"] for rid in changed})
    paths = reached(model, ids, "why")
    affected = [
        {**model.summary(rid), "path": path, **({"status": model.status(rid)} if model.status(rid) else {})}
        for rid, path in sorted(paths.items(), key=lambda item: (len(item[1]), item[0]))
        if rid not in ids
    ]
    recheck = [
        {**model.summary(rid), "observe": model.records[rid].get("observe", {})}
        for rid in sorted(paths)
        if model.records[rid]["type"] == "Requirement" and model.records[rid]["lifecycle"] != "retired"
    ]
    outcomes = sorted(rid for rid in paths if model.records[rid]["type"] == "Outcome")
    return {
        "changed": [model.summary(rid) for rid in ids],
        "affected": affected,
        "outcomes": outcomes,
        "recheck": recheck,
    }


def gaps(model: Model) -> Doc:
    """What the model is missing. Findings are the validator's (sections 7 and 11). Coverage lists what conforms
    but is not yet established (section 10), for active and proposed records alike."""
    coverage: dict[str, list[Doc]] = {
        "unverified": [],
        "unreleased": [],
        "unobserved": [],
        "proposed_measures": [],
    }
    for rid, r in sorted(model.records.items()):
        if r["lifecycle"] == "retired":
            continue
        status = model.status(rid)
        if r["type"] == "Requirement":
            verification = {c: s["verification"] for c, s in status.items() if "verification" in s}
            delivery = {c: s["delivery"] for c, s in status.items() if "delivery" in s}
            if "passed" not in verification.values():
                coverage["unverified"].append({**model.summary(rid), "verification": verification})
            if "released" not in delivery.values():
                coverage["unreleased"].append({**model.summary(rid), "delivery": delivery})
            if not r.get("observe"):
                coverage["unobserved"].append(model.summary(rid))
        if r["type"] == "Measure" and r["lifecycle"] == "proposed":
            evaluates = [e.target for e in model.graph.edges if e.source == rid and e.type == "evaluates"]
            coverage["proposed_measures"].append({**model.summary(rid), "evaluates": evaluates})
    pending = [
        {"context": context, **work} for context, state in model.states.items() for work in state.get("pending", [])
    ]
    return {
        "result": model.report.result,
        "errors": [finding(f) for f in model.report.errors],
        "warnings": [finding(f) for f in model.report.warnings],
        "waived": [finding(f) for f in model.report.waived],
        "coverage": coverage,
        "pending": pending,
        "contexts": {
            c: {"config_revision": s["config_revision"], "serial": s["serial"]} for c, s in model.states.items()
        },
    }


def changes(base: Model, head: Model) -> Doc:
    """Records and relationships added, changed or removed from `base` to `head`, as in section 10's
    release comparison. A record is changed when any of its authored fields differ."""
    before, after = base.records, head.records
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(rid for rid in set(before) & set(after) if before[rid] != after[rid])

    old, new = authored(base), authored(head)
    return {
        "records": {
            "added": [head.summary(rid) for rid in added],
            "changed": [
                {
                    **head.summary(rid),
                    "fields": sorted(
                        k for k in before[rid].keys() | after[rid].keys() if before[rid].get(k) != after[rid].get(k)
                    ),
                }
                for rid in changed
            ],
            "removed": [base.summary(rid) for rid in removed],
        },
        "relationships": {
            "added": [label(new[k]) for k in sorted(new.keys() - old.keys())],
            "changed": [
                {"relationship": label(new[k]), "fields": differing(old[k], new[k])}
                for k in sorted(new.keys() & old.keys())
                if differing(old[k], new[k])
            ],
            "removed": [label(old[k]) for k in sorted(old.keys() - new.keys())],
        },
    }


def authored(model: Model) -> dict[tuple[str, str, str], Doc]:
    """The model's relationships as authored, by identity (section 6), with endpoints in this model made local.
    A relationship with an `id` is identified by it; any other by its source, type and target."""
    found = {}
    for rel in model.relationships:
        local = {**rel, "source": model.graph.key(rel)[0], "target": model.graph.key(rel)[2]}
        found[("id", rel["id"], "") if "id" in rel else model.graph.key(rel)] = local
    return found


def label(rel: Doc) -> str:
    return f"{rel['source']} --{rel['type']}--> {rel['target']}"


def differing(old: Doc, new: Doc) -> list[str]:
    return sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))


def reliant(model: Model, rel: Doc) -> set[str]:
    """The records in `model` at the end of a relationship that relies on the other: the reason end, or both
    when neither is. An end in another model is left out."""
    if rel["type"] in WHY_IS_TARGET:
        ends = {rel["target"]}
    elif rel["type"] in WHY_IS_SOURCE:
        ends = {rel["source"]}
    else:
        ends = {rel["source"], rel["target"]}
    return {end for end in ends if end in model.records}


def change_impact(base: Model, head: Model) -> Doc:
    """The changes from `base` to `head`, and what they affect. Added and changed records are traced in `head`;
    removed ones in `base`, keeping only what still exists in `head`. A relationship added, changed or removed
    changes the record that relies on the other end: a Requirement that gains an implementer, or an Application
    that reads new Information, but not the Information itself."""
    diff = changes(base, head)
    touched = {r["id"] for kind in ("added", "changed") for r in diff["records"][kind]}
    removed = {r["id"] for r in diff["records"]["removed"]}
    old, new = authored(base), authored(head)
    retargeted = {k for k in new.keys() & old.keys() if differing(old[k], new[k])}
    for key in new.keys() - old.keys() | retargeted:
        touched |= reliant(head, new[key])
    # A changed relationship affects its earlier version's ends too, such as a Requirement that lost its implementer.
    for key in old.keys() - new.keys() | retargeted:
        for rid in reliant(base, old[key]):
            (touched if rid in head.records else removed).add(rid)
    result = impact(head, touched)
    # A record whose relationships changed, but not its fields, is affected rather than changed.
    edited = {r["id"] for kind in ("added", "changed") for r in diff["records"][kind]}
    result["changed"] = [r for r in result["changed"] if r["id"] in edited]
    result["affected"][:0] = [
        {**head.summary(rid), "path": [rid], **({"status": head.status(rid)} if head.status(rid) else {})}
        for rid in sorted(touched - edited)
    ]
    known = {a["id"] for a in result["affected"]} | touched
    for rid, path in sorted(reached(base, removed, "why").items()):
        if rid in head.records and rid not in known:
            known.add(rid)
            result["affected"].append({**head.summary(rid), "path": path})
            if head.records[rid]["type"] == "Outcome":
                result["outcomes"] = sorted({*result["outcomes"], rid})
            if head.records[rid]["type"] == "Requirement" and head.records[rid]["lifecycle"] != "retired":
                result["recheck"].append({**head.summary(rid), "observe": head.records[rid].get("observe", {})})
    return {**diff, "impact": result}
