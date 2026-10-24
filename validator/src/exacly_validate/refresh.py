"""Write a model's state from what its bindings observe (section 9).

The first observer reads test results: JUnit XML reports from one CI run. For each
record with a `tests` binding, it writes a verification assessment for the observed
config revision. With `distribution`, the observed context is how the product is
distributed (for example, users install from main), so a Requirement whose
implementer's reference exists at the revision is also assessed as released there.

State is rebuilt by observing again, never edited: each write increases the serial
and keeps the lineage. Records this run did not observe keep their earlier entries.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exacly_validate.core import is_state, version_of

Doc = dict[str, Any]
CASE_PROPERTY = "exacly-case"
MAX_NAMED_FAILURES = 5


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


def verification(binding: Doc, results: Mapping[str, str], run: Run) -> Doc:
    """The verification assessment a tests binding gives in this run."""
    evidence = {
        "locator": run.url or "test results",
        "kind": "test",
        "observed_at": run.observed_at.date().isoformat(),
        "applies_to": run.revision,
    }
    assessment: Doc = {"dimension": "verification", "state": None, "context": run.revision, "at": timestamp(run)}
    patterns = binding.get("cases")
    job = f"job {binding['job']}" if binding.get("job") else "job"
    if not patterns:
        if run.job_status is None:
            return {**assessment, "state": "inconclusive", "evidence": [], "reason": "The job's result was not given."}
        if run.job_status == "success":
            claim = f"The {job} succeeded"
            return {**assessment, "state": "passed", "evidence": [{**evidence, "claim": claim}]}
        claim = f"The {job} ended with status {run.job_status}"
        reason = f"The job ended with status {run.job_status}."
        return {**assessment, "state": "failed", "evidence": [{**evidence, "claim": claim}], "reason": reason}

    selected = {cid: outcome for cid, outcome in results.items() if any(fnmatch.fnmatchcase(cid, p) for p in patterns)}
    failed = sorted(cid for cid, outcome in selected.items() if outcome == "failed")
    ran = [cid for cid, outcome in selected.items() if outcome != "skipped"]
    if not ran:
        reason = (
            "The binding selects no test case that ran." if not selected else "Every selected test case was skipped."
        )
        return {**assessment, "state": "inconclusive", "evidence": [], "reason": reason}
    if failed:
        named = ", ".join(failed[:MAX_NAMED_FAILURES])
        more = f" and {len(failed) - MAX_NAMED_FAILURES} more" if len(failed) > MAX_NAMED_FAILURES else ""
        claim = f"{len(failed)} of {len(ran)} selected test cases failed"
        return {
            **assessment,
            "state": "failed",
            "evidence": [{**evidence, "claim": claim}],
            "reason": f"Failed: {named}{more}.",
        }
    skipped = len(selected) - len(ran)
    claim = f"All {len(ran)} selected test cases passed" + (f"; {skipped} skipped" if skipped else "")
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
        "evidence": evidence,
    }


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
    relationships = [rel for d in configs for rel in d.get("relationships", [])]

    if previous is not None:
        if previous.get("namespace") != namespace or previous.get("context") != run.context:
            raise RefreshError(
                f"earlier state is for {previous.get('namespace')} in {previous.get('context')}, "
                f"not {namespace} in {run.context}"
            )
        lineage, serial = previous["lineage"], previous["serial"] + 1
        observed: Doc = {rid: entry for rid, entry in previous.get("records", {}).items() if rid in records}
    else:
        lineage, serial, observed = str(uuid.uuid4()), 1, {}

    for rid, record in records.items():
        binding = record.get("observe", {}).get("tests")
        assessments = []
        if binding and observes(binding, run):
            assessments.append(verification(binding, results, run))
        if distribution and record["type"] == "Requirement":
            released = delivery(rid, records, relationships, run, root)
            if released:
                assessments.append(released)
        if assessments:
            observed[rid] = {"assessments": assessments}

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
    if previous is not None:
        for key in ("relationships", "pending"):
            if previous.get(key):
                state[key] = previous[key]
    return state
