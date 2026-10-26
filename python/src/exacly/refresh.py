"""Write a model's state from what its bindings observe (section 9).

The first observer reads test results: JUnit XML reports from one CI run. For each
record with a `tests` binding, it writes a verification assessment for the observed
config revision. With `distribution`, the observed context is how the product is
distributed (for example, users install from main), so a Requirement whose
implementer's reference exists at the revision is also assessed as released there.

State is rebuilt by observing again, never edited: each write increases the serial
and keeps the lineage. Each assessment names its source, `tests` or `distribution`, and
an observation replaces only the assessments of the sources it observed; other
assessments and evidence stay, such as another observer's, unless their record or
relationship has left the config, and so does pending work.
"""

from __future__ import annotations

import copy
import datetime as dt
import fnmatch
import functools
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exacly.core import bindings, identity, is_state, version_of

Doc = dict[str, Any]
CASE_PROPERTY = "exacly-case"
MAX_NAMED_FAILURES = 5
# The source an assessment written before sources were recorded came from: this observer wrote only these.
LEGACY_SOURCES = {"verification": "tests", "delivery": "distribution"}


class RefreshError(Exception):
    """The inputs cannot produce valid state, such as an earlier state for another model or context."""


@dataclass(frozen=True)
class Run:
    """One observation: the config revision and the CI run whose results are read."""

    revision: str
    context: str
    observed_at: dt.datetime
    workflow: str | None = None
    job: str | None = None
    job_status: str | None = None
    url: str | None = None
    source_url: str | None = None  # Base URL for the revision's files, such as https://github.com/o/r/blob/<sha>


def timestamp(run: Run) -> str:
    return run.observed_at.isoformat().replace("+00:00", "Z")


def case_id(case: ET.Element) -> str:
    """A test case's ID: its exacly-case property, or `<file>::<name>`."""
    for prop in case.iter("property"):
        if prop.get("name") == CASE_PROPERTY and prop.get("value"):
            return str(prop.get("value"))
    return f"{case.get('file') or case.get('classname', '')}::{case.get('name', '')}"


def read_junit(paths: Iterable[Path]) -> dict[str, str]:
    """Each test case's outcome by ID: passed, failed or skipped. If an ID repeats, failed wins over passed."""
    results: dict[str, str] = {}
    rank = {"skipped": 0, "passed": 1, "failed": 2}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            children = {child.tag for child in case}
            outcome = "failed" if children & {"failure", "error"} else "skipped" if "skipped" in children else "passed"
            cid = case_id(case)
            if rank[outcome] >= rank.get(results.get(cid, "skipped"), 0):
                results[cid] = outcome
    return results


def observes(binding: Doc, run: Run) -> bool:
    """A binding is observed by this run when it names this run's workflow and job, or the run names neither."""
    for key, value in (("workflow", run.workflow), ("job", run.job)):
        if value is not None and binding.get(key) is not None and binding[key] != value:
            return False
    return True


def named(items: list[str]) -> str:
    """Up to MAX_NAMED_FAILURES items, then how many more."""
    more = f" and {len(items) - MAX_NAMED_FAILURES} more" if len(items) > MAX_NAMED_FAILURES else ""
    return ", ".join(items[:MAX_NAMED_FAILURES]) + more


def verification(binding: Doc, results: Mapping[str, str], run: Run) -> Doc:
    """The verification assessment a tests binding gives in this run."""
    evidence = {
        "locator": run.url or "test results",
        "kind": "test",
        "observed_at": run.observed_at.date().isoformat(),
        "applies_to": run.revision,
    }
    assessment: Doc = {"dimension": "verification", "state": None, "context": run.revision, "at": timestamp(run)}
    assessment["source"] = "tests"
    if identity("tests", binding):
        assessment["binding"] = identity("tests", binding)
    patterns = binding.get("cases")
    job = f"job {binding['job']}" if binding.get("job") else "job"
    if not patterns:
        if run.job_status is None:
            return {**assessment, "state": "not_checked", "evidence": [], "reason": "The job's result was not given."}
        if run.job_status == "success":
            claim = f"The {job} succeeded"
            return {**assessment, "state": "passed", "evidence": [{**evidence, "claim": claim}]}
        claim = f"The {job} ended with status {run.job_status}"
        reason = f"The job ended with status {run.job_status}."
        return {**assessment, "state": "failed", "evidence": [{**evidence, "claim": claim}], "reason": reason}

    selected = {cid: outcome for cid, outcome in results.items() if any(fnmatch.fnmatchcase(cid, p) for p in patterns)}
    # A pattern that selects nothing is a missing observation, such as a renamed test (section 8).
    empty = [p for p in patterns if not any(fnmatch.fnmatchcase(cid, p) for cid in results)]
    missing = f" Selected nothing: {named(empty)}." if empty else ""
    failed = sorted(cid for cid, outcome in selected.items() if outcome == "failed")
    ran = [cid for cid, outcome in selected.items() if outcome != "skipped"]
    if not ran:
        # Nothing was checked, so there is no result to cite; the reason says why (section 8).
        reason = (
            "The binding selects no test case that ran." if not selected else "Every selected test case was skipped."
        ) + missing
        return {**assessment, "state": "not_checked", "evidence": [], "reason": reason}
    if failed:
        claim = f"{len(failed)} of {len(ran)} selected test cases failed"
        return {
            **assessment,
            "state": "failed",
            "evidence": [{**evidence, "claim": claim}],
            "reason": f"Failed: {named(failed)}.{missing}",
        }
    skipped = len(selected) - len(ran)
    claim = f"All {len(ran)} selected test cases passed" + (f"; {skipped} skipped" if skipped else "")
    if empty:
        reason = missing.strip()
        return {**assessment, "state": "inconclusive", "evidence": [{**evidence, "claim": claim}], "reason": reason}
    return {**assessment, "state": "passed", "evidence": [{**evidence, "claim": claim}]}


def delivery(
    requirement: str, records: Mapping[str, Doc], relationships: list[Doc], run: Run, root: Path
) -> Doc | None:
    """A released assessment in the distribution context, when an implementer's reference exists at the revision."""
    found = []
    for rel in relationships:
        implementer = records.get(rel["source"])
        if rel["type"] != "implements" or rel["target"] != requirement or not implementer:
            continue
        ref = implementer.get("implementation_ref") or implementer.get("operational_ref")
        if implementer["lifecycle"] == "active" and ref and not ref.get("planned") and (root / ref["locator"]).exists():
            found.append((implementer, ref["locator"]))
    if not found:
        return None
    evidence = [
        {
            "locator": f"{run.source_url}/{locator}" if run.source_url else locator,
            "kind": "implementation",
            "claim": f"{implementer['name']} ({implementer['id']}) is at {locator} in revision {run.revision}",
            "observed_at": run.observed_at.date().isoformat(),
            "applies_to": run.context,
        }
        for implementer, locator in found
    ]
    return {
        "dimension": "delivery",
        "state": "released",
        "context": run.context,
        "at": timestamp(run),
        "source": "distribution",
        "evidence": evidence,
    }


def with_source(assessment: Doc) -> Doc:
    """An earlier assessment with its source: one written before sources were recorded gets the source of the
    observer that wrote it. An assessment of another dimension without a source is kept as it is."""
    if "source" in assessment or assessment["dimension"] not in LEGACY_SOURCES:
        return assessment
    return {**assessment, "source": LEGACY_SOURCES[assessment["dimension"]]}


def answers(assessment: Doc, binding: Doc, single: bool) -> bool:
    """Whether a tests assessment is this binding's result: it names the binding, or it names none and the binding is
    the record's only one or has no identifying fields."""
    return assessment["binding"] == binding if "binding" in assessment else single or not binding


def observed_again(sources: set[str], seen: list[Doc], current: list[Doc], single: bool, assessment: Doc) -> bool:
    """Whether a run that observed `sources` and the tests bindings `seen` replaces an earlier assessment: one of
    those sources, a tests result of a binding it observed, or one of a tests binding no longer in the config."""
    if assessment.get("source") in sources:
        return True
    return assessment.get("source") == "tests" and (
        any(answers(assessment, b, single) for b in seen) or not any(answers(assessment, b, single) for b in current)
    )


def unattributed(state: Doc) -> list[str]:
    """The assessments in a state that name no source, as `<record> <dimension>`: their origin is unknown."""
    return [
        f"{rid} {a['dimension']}"
        for rid, entry in state["records"].items()
        for a in entry.get("assessments", [])
        if "source" not in a
    ]


@dataclass
class Carried:
    """What a state write starts from: the config, and the earlier state's observations that stay."""

    namespace: str
    version: str
    records: dict[str, Doc]
    relationships: list[Doc]
    observed: Doc
    kept_relationships: list[Doc]
    kept_pending: list[Doc]
    lineage: str
    serial: int


def carry(documents: Mapping[str, Any], context: str, previous: Doc | None) -> Carried:
    """The config from `documents`, and from `previous`, the context's earlier state, its lineage and next serial
    and the observations whose record or relationship is still in the config."""
    configs = [d for d in documents.values() if isinstance(d, dict) and not is_state(d)]
    models = [d["model"] for d in configs if "model" in d]
    if len(models) != 1:
        raise RefreshError("exactly one config document must declare the model boundary")
    namespace = models[0]["namespace"]
    version = version_of(next(d for d in configs if "model" in d))
    records = {r["id"]: r for d in configs for r in d.get("records", [])}

    def local(ref: str) -> str:
        return ref.removeprefix(f"{namespace}:")

    # Endpoints qualified with the model's own namespace name local records.
    relationships = [
        {**rel, "source": local(rel["source"]), "target": local(rel["target"])}
        for d in configs
        for rel in d.get("relationships", [])
    ]

    if previous is not None:
        if previous.get("namespace") != namespace or previous.get("context") != context:
            raise RefreshError(
                f"earlier state is for {previous.get('namespace')} in {previous.get('context')}, "
                f"not {namespace} in {context}"
            )
        lineage, serial = previous["lineage"], previous["serial"] + 1
    else:
        lineage, serial = str(uuid.uuid4()), 1
    earlier = previous or {}

    # Earlier observations stay unless their record or relationship has left the config.
    observed: Doc = {}
    for ref, entry in earlier.get("records", {}).items():
        if local(ref) in records:
            observed[local(ref)] = copy.deepcopy(entry)
            if "assessments" in entry:
                observed[local(ref)]["assessments"] = [with_source(a) for a in observed[local(ref)]["assessments"]]
    ids = {rel["id"] for rel in relationships if "id" in rel}
    keys = {(rel["source"], rel["type"], rel["target"]) for rel in relationships}
    kept_relationships = [
        rel
        for rel in earlier.get("relationships", [])
        if (rel["id"] in ids if "id" in rel else (local(rel["source"]), rel["type"], local(rel["target"])) in keys)
    ]
    kept_pending = [work for work in earlier.get("pending", []) if local(work["record"]) in records]
    return Carried(
        namespace, version, records, relationships, observed, kept_relationships, kept_pending, lineage, serial
    )


def replace(carried: Carried, rid: str, fresh: list[Doc], replaced: Callable[[Doc], bool]) -> None:
    """Put a record's fresh assessments in place of the earlier ones `replaced` selects; the others stay."""
    if rid not in carried.observed and not fresh:
        return
    entry = carried.observed.setdefault(rid, {})
    entry["assessments"] = [*(a for a in entry.get("assessments", []) if not replaced(a)), *fresh]
    if not entry["assessments"] and not entry.get("evidence"):
        del carried.observed[rid]


def assemble(carried: Carried, context: str, revision: str, written_at: str) -> Doc:
    """The state document for `context` observed at the config `revision`."""
    state: Doc = {
        "exacly": carried.version,
        "namespace": carried.namespace,
        "context": context,
        "config_revision": revision,
        "serial": carried.serial,
        "lineage": carried.lineage,
        "written_at": written_at,
        "records": dict(sorted(carried.observed.items())),
    }
    if carried.kept_relationships:
        state["relationships"] = copy.deepcopy(carried.kept_relationships)
    if carried.kept_pending:
        state["pending"] = copy.deepcopy(carried.kept_pending)
    return state


def refresh(
    documents: Mapping[str, Any],
    results: Mapping[str, str],
    run: Run,
    previous: Doc | None = None,
    distribution: bool = False,
    root: Path = Path(),
) -> Doc:
    """The state document for `run.context`, observed from config `documents` and test `results`.

    `previous` is the context's earlier state, whose lineage is kept and whose serial is increased.
    """
    carried = carry(documents, run.context, previous)
    records, relationships = carried.records, carried.relationships
    for rid, record in records.items():
        tests = bindings(record, "tests")
        single = len(tests) == 1
        current = [identity("tests", b) for b in tests]
        fresh, sources, seen = [], set(), []
        for binding in tests:
            if observes(binding, run):
                fresh.append(verification(binding, results, run))
                seen.append(identity("tests", binding))
        if distribution and record["type"] == "Requirement":
            # Delivery is observed either way: without a present implementer, an earlier release no longer holds.
            sources.add("distribution")
            released = delivery(rid, records, relationships, run, root)
            if released:
                fresh.append(released)

        # Replace only the results this run observed: of each tests binding it observed, and of the other sources it
        # observed. Other assessments, such as another observer's or another job's, and evidence stay; a tests
        # result of a binding that has left the config goes.
        replace(carried, rid, fresh, functools.partial(observed_again, sources, seen, current, single))
    return assemble(carried, run.context, run.revision, timestamp(run))
