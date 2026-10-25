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
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
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
    repository: str | None = None  # owner/name of the repository the run belongs to
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
    """A binding is observed by this run when it names this run's repository, workflow and job, or leaves out any the
    run doesn't name."""
    for key, value in (("repository", run.repository), ("workflow", run.workflow), ("job", run.job)):
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


def answers(assessment: Doc, binding: dict[str, str], single: bool) -> bool:
    """Whether a tests assessment is this binding's result: it names the binding, or it names none and the binding is
    the record's only one or has no identifying fields."""
    return assessment["binding"] == binding if "binding" in assessment else single or not binding


def unattributed(state: Doc) -> list[str]:
    """The assessments in a state that name no source, as `<record> <dimension>`: their origin is unknown."""
    return [
        f"{rid} {a['dimension']}"
        for rid, entry in state["records"].items()
        for a in entry.get("assessments", [])
        if "source" not in a
    ]


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
        if previous.get("namespace") != namespace or previous.get("context") != run.context:
            raise RefreshError(
                f"earlier state is for {previous.get('namespace')} in {previous.get('context')}, "
                f"not {namespace} in {run.context}"
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
        if rid not in observed and not fresh:
            continue
        # Replace only the results this run observed: of each tests binding it observed, and of the other sources it
        # observed. Other assessments, such as another observer's or another repository's, and evidence stay; a tests
        # result of a binding that has left the config goes.
        entry = observed.setdefault(rid, {})
        entry["assessments"] = [
            *(
                a
                for a in entry.get("assessments", [])
                if a.get("source") not in sources
                and not (
                    a.get("source") == "tests"
                    and (any(answers(a, b, single) for b in seen) or not any(answers(a, b, single) for b in current))
                )
            ),
            *fresh,
        ]
        if not entry["assessments"] and not entry.get("evidence"):
            del observed[rid]

    state: Doc = {
        "exacly": version,
        "namespace": namespace,
        "context": run.context,
        "config_revision": run.revision,
        "serial": serial,
        "lineage": lineage,
        "written_at": timestamp(run),
        "records": dict(sorted(observed.items())),
    }
    if kept_relationships:
        state["relationships"] = copy.deepcopy(kept_relationships)
    if kept_pending:
        state["pending"] = copy.deepcopy(kept_pending)
    return state
