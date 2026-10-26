"""The manual-qa extension: verification from people who run the product, such as a check on a real phone.

A record binds `manual-qa:observations` to the observation files people commit, and a target manifest per context
says which release and builds are assessed now and which criteria are due in that release. `exacly observe
manual-qa` reads both and writes one verification assessment per platform of each binding, replacing only this
source's assessments. An observation counts only for the manifest's release and exact builds, and only when its
output is retained and matches its digest. The latest result of each criterion on each platform wins, so a later run
that leaves a failed criterion out doesn't hide the failure.

A platform is verified only when every criterion passed there. When every criterion due in the release passed but
others are not yet covered, the assessment is inconclusive and names them, and `x-release-scope` says the release's
scope passed.
"""

from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from exacly.core import bindings, identity
from exacly.refresh import RefreshError, assemble, carry, replace

Doc = dict[str, Any]
EXTENSION = "manual-qa"
SOURCE = f"{EXTENSION}:observations"

TEXT = {"type": "string", "pattern": "\\S"}
BUILDS = {
    "type": "object",
    "minProperties": 1,
    "description": "Each build by name, such as app or backend, identified by fields such as version and digest.",
    "additionalProperties": {"type": "object", "minProperties": 1, "additionalProperties": TEXT},
}
# An RFC 3339 timestamp with its offset, checked here because jsonschema checks the date-time format only when an
# optional package is installed.
TIMESTAMP = {
    "type": "string",
    "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$",
}
RESULT = {"enum": ["passed", "failed", "inconclusive"]}
CRITERIA = {"type": "array", "items": {"type": "integer", "minimum": 1}, "uniqueItems": True}
BINDING_SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "manual-qa:observations binding",
    "type": "object",
    "required": ["path"],
    "additionalProperties": False,
    "properties": {
        "path": {**TEXT, "description": "Observation files, as a glob relative to the repository root."},
        "platforms": {
            "type": "array",
            "items": TEXT,
            "minItems": 1,
            "uniqueItems": True,
            "description": "Each must pass for the record to count as verified.",
        },
    },
}
TARGET_SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "manual-qa target manifest",
    "type": "object",
    "required": ["context", "model_revision", "release", "builds", "due"],
    "additionalProperties": False,
    "properties": {
        "context": TEXT,
        "model_revision": {**TEXT, "description": "The config revision the due criteria are numbered at."},
        "release": TEXT,
        "builds": BUILDS,
        "due": {
            "type": "object",
            "description": "By record, the criteria in scope for this release, numbered from 1.",
            "additionalProperties": CRITERIA,
        },
    },
}
OBSERVATION_SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "manual-qa observation",
    "type": "object",
    "required": ["observed_by", "observed_at", "record", "release", "model_revision", "builds", "results", "output"],
    "additionalProperties": False,
    "properties": {
        "observed_by": TEXT,
        "observed_at": {**TIMESTAMP, "description": "A timestamp with its offset: two runs on one day must not tie."},
        "record": TEXT,
        "release": TEXT,
        "model_revision": {**TEXT, "description": "The config revision the criteria are numbered at."},
        "platform": TEXT,
        "device": {**TEXT, "description": "Descriptive only, never used for matching."},
        "builds": BUILDS,
        "results": {
            "type": "object",
            "minProperties": 1,
            "propertyNames": {"pattern": "^[1-9][0-9]*$"},
            "additionalProperties": {
                "type": "object",
                "required": ["result"],
                "additionalProperties": False,
                "properties": {"result": RESULT, "reason": TEXT},
                "if": {"properties": {"result": {"enum": ["failed", "inconclusive"]}}},
                "then": {"required": ["reason"]},
            },
        },
        "output": {
            "type": "object",
            "required": ["file", "digest"],
            "additionalProperties": False,
            "properties": {
                "file": {**TEXT, "description": "The retained output, relative to the repository root."},
                "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "locator": {**TEXT, "description": "Where the output was first shared, such as a chat thread."},
            },
        },
    },
}


ASSESSMENT_SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "manual-qa:observations assessment fields",
    "description": "The x- fields this source writes on a state assessment; the core fields are the state schema's.",
    "type": "object",
    "required": ["x-release", "x-builds", "x-criteria"],
    "properties": {
        "x-platform": TEXT,
        "x-release": TEXT,
        "x-builds": BUILDS,
        "x-criteria": {
            "type": "object",
            "propertyNames": {"pattern": "^[1-9][0-9]*$"},
            "additionalProperties": RESULT,
        },
        "x-release-scope": {"enum": ["passed", "open"]},
        "x-unused": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "why"],
                "additionalProperties": False,
                "properties": {"file": TEXT, "why": TEXT},
            },
        },
    },
}


@dataclass
class Observation:
    file: str
    doc: Doc
    at: dt.datetime
    results: dict[int, Doc] = field(default_factory=dict)


def problems(doc: Any, schema: Doc) -> list[str]:
    """Where a document breaks a schema, one line each."""
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [
        f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    ]


def plain(value: Any) -> Any:
    """YAML as JSON would read it: timestamps as text, and mapping keys such as criterion numbers as strings."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dt.datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def read(path: Path) -> Any:
    text = path.read_text()
    return plain(json.loads(text) if path.suffix == ".json" else yaml.safe_load(text))


def numbers(items: list[int]) -> str:
    """Criteria as words: "criterion 3", "criteria 1 and 2", "criteria 1, 2 and 3"."""
    if len(items) == 1:
        return f"criterion {items[0]}"
    return "criteria " + ", ".join(map(str, items[:-1])) + f" and {items[-1]}"


def build_words(builds: Doc) -> str:
    """Builds by name with their main identifier, such as "app 1.4.1, backend b7f3e21"."""
    words = []
    for name, build in builds.items():
        value = build.get("version") or build.get("revision") or next(iter(build.values()))
        words.append(f"{name} {value}")
    return ", ".join(words)


def digest_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def check_target(target: Any) -> Doc:
    found = problems(target, TARGET_SCHEMA)
    if found:
        raise RefreshError("the target manifest is not valid: " + "; ".join(found))
    return dict(target)


def usable(
    file: str,
    doc: Any,
    rid: str,
    platforms: list[str] | None,
    target: Doc,
    criteria: list[str],
    root: Path,
    stale: Callable[[str, str], bool],
) -> tuple[Observation | None, str | None]:
    """An observation of record `rid` that counts for the target, or why it doesn't; (None, None) when it is another
    record's or another release's or builds', which is history rather than a problem."""
    found = problems(doc, OBSERVATION_SCHEMA)
    if found:
        return None, "not a valid observation: " + "; ".join(found)
    try:
        at = dt.datetime.fromisoformat(doc["observed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        return None, f"not a valid observation: /observed_at: {error}"
    if doc["record"] != rid:
        return None, None
    if platforms is not None and doc.get("platform") not in platforms:
        return None, f"platform {doc.get('platform', '(none)')} is not one of {', '.join(platforms)}"
    if doc["release"] != target["release"]:
        return None, None
    if set(doc["builds"]) != set(target["builds"]):
        names = ", ".join(sorted(doc["builds"]))
        return None, f"names the builds {names}, but the target assesses {', '.join(sorted(target['builds']))}"
    if doc["builds"] != target["builds"]:
        return None, None
    if stale(doc["model_revision"], rid):
        return None, f"stale: {rid}'s criteria at {doc['model_revision']} differ from the current ones"
    output = root / doc["output"]["file"]
    if not output.is_file():
        return None, f"unusable: its output {doc['output']['file']} is missing"
    if digest_of(output) != doc["output"]["digest"]:
        return None, f"unusable: its output {doc['output']['file']} doesn't match its digest"
    results = {int(n): result for n, result in doc["results"].items()}
    beyond = sorted(n for n in results if n > len(criteria))
    if beyond:
        return None, f"{numbers(beyond)} beyond {rid}'s {len(criteria)}"
    return Observation(file, doc, at, results), None


def latest(observations: list[Observation]) -> dict[int, tuple[Doc, Observation]]:
    """Each criterion's latest result and the observation that gave it. Two at one time for a criterion are an
    error, since neither can win."""
    found: dict[int, tuple[Doc, Observation]] = {}
    for obs in sorted(observations, key=lambda o: o.at):
        for n, result in obs.results.items():
            if n in found and found[n][1].at == obs.at:
                other = found[n][1].file
                raise RefreshError(f"{other} and {obs.file} both give criterion {n} at {obs.doc['observed_at']}")
            found[n] = (result, obs)
    return found


def assessment(
    rid: str, platform: str | None, target: Doc, criteria: list[str], observations: list[Observation], at: str
) -> Doc:
    """The verification assessment of one platform of one binding."""
    where = f" on {platform}" if platform else ""
    release = target["release"]
    due = sorted(target["due"].get(rid, []))
    results = latest(observations)
    by_state = {s: sorted(n for n, (r, _) in results.items() if r["result"] == s) for s in ("passed", "failed")}
    unsure = sorted(n for n, (r, _) in results.items() if r["result"] == "inconclusive")
    a: Doc = {"dimension": "verification", "state": None, "context": target["context"], "at": at, "source": SOURCE}
    a["subject"] = {
        "model_revision": target["model_revision"],
        "builds": target["builds"],
        "release": target["release"],
        "environment": target["context"],
        **({"platform": platform} if platform else {}),
    }
    details: Doc = {
        **({"x-platform": platform} if platform else {}),
        "x-release": release,
        "x-builds": target["builds"],
        "x-criteria": {str(n): r["result"] for n, (r, _) in sorted(results.items())},
    }
    if due:
        # The release's scope is its due criteria: a failure outside them fails the record, not the release.
        scope = all(n in by_state["passed"] for n in due)
        details["x-release-scope"] = "passed" if scope else "open"
    used = sorted({id(o): o for _, o in results.values()}.values(), key=lambda o: o.at)
    evidence = [
        {
            "locator": o.doc["output"].get("locator") or o.doc["output"]["file"],
            **({"snapshot": o.doc["output"]["file"]} if "locator" in o.doc["output"] else {}),
            "kind": "test",
            "claim": f"{o.doc['observed_by']} checked {numbers(sorted(o.results))} of {rid}{where} "
            f"with {build_words(target['builds'])} ({o.file})",
            "observed_at": o.at.date().isoformat(),
            "applies_to": target["context"],
            "digest": o.doc["output"]["digest"],
        }
        for o in used
    ]
    if by_state["failed"]:
        reasons = " ".join(f"Criterion {n} failed{where}: {results[n][0]['reason']}" for n in by_state["failed"])
        return {**a, "state": "failed", "reason": reasons, "evidence": evidence, **details}
    if unsure:
        reasons = " ".join(f"Criterion {n} was inconclusive{where}: {results[n][0]['reason']}" for n in unsure)
        return {**a, "state": "inconclusive", "reason": reasons, "evidence": evidence, **details}
    if not results:
        reason = f"No observation{where} for release {release} with {build_words(target['builds'])}."
        return {**a, "state": "not_checked", "reason": reason, "evidence": [], **details}
    every = list(range(1, len(criteria) + 1))
    if by_state["passed"] == every:
        return {**a, "state": "passed", "evidence": evidence, **details}
    outstanding = [n for n in every if n not in by_state["passed"]]
    if due and details["x-release-scope"] == "passed":
        reason = f"{numbers(due).capitalize()} passed for release {release}{where}; {numbers(outstanding)} not covered."
        return {**a, "state": "inconclusive", "reason": reason, "evidence": evidence, **details}
    open_due = [n for n in due if n not in by_state["passed"]] or outstanding
    reason = f"{numbers(open_due).capitalize()} not yet checked for release {release}{where}."
    return {**a, "state": "not_checked", "reason": reason, "evidence": evidence, **details}


def observe(
    documents: Mapping[str, Any],
    target: Any,
    revision: str,
    written_at: str,
    previous: Doc | None = None,
    root: Path = Path(),
    criteria_at: Callable[[str], Mapping[str, list[str]] | None] = lambda revision: None,
) -> tuple[Doc, list[str]]:
    """The state for the target's context, with this source's assessments observed from the observation files the
    bindings name, and notes on the observations that were not used and why.

    `criteria_at` gives each record's acceptance criteria at an earlier config revision, or None when that revision
    can't be read; an observation or a due set numbered at a revision where a record's criteria differ is stale.
    """
    target = check_target(target)
    carried = carry(documents, target["context"], previous)
    current = {rid: list(r.get("acceptance_criteria", [])) for rid, r in carried.records.items()}
    earlier: dict[str, Mapping[str, list[str]] | None] = {}

    def differ(revision: str, rid: str) -> bool:
        """Whether a record's criteria at an earlier revision differ from the current ones, or can't be read."""
        if revision not in earlier:
            earlier[revision] = criteria_at(revision)
        at = earlier[revision]
        return at is None or list(at.get(rid, [])) != current.get(rid)

    def stale(earlier_revision: str, rid: str) -> bool:
        # Every observation is compared with the current criteria, including one at the target's revision: the due
        # set is checked against them only for the records it lists.
        return earlier_revision != revision and differ(earlier_revision, rid)

    if target["model_revision"] != revision:
        for rid in target["due"]:
            if rid in current and differ(target["model_revision"], rid):
                raise RefreshError(
                    f"the target's due criteria for {rid} are numbered at {target['model_revision']}, "
                    f"where its criteria differ from {revision}"
                )
    for rid, due in target["due"].items():
        if rid not in current:
            raise RefreshError(f"the target lists due criteria for {rid}, which is not a record in this model")
        beyond = [n for n in due if n > len(current[rid])]
        if beyond:
            raise RefreshError(f"the target's {numbers(beyond)} for {rid} are beyond its {len(current[rid])}")

    notes: list[str] = []
    files: dict[str, Any] = {}
    for rid, record in carried.records.items():
        fresh = []
        several = len(bindings(record, SOURCE)) > 1
        for binding in bindings(record, SOURCE):
            platforms = binding.get("platforms")
            found: dict[str | None, list[Observation]] = {p: [] for p in platforms or [None]}
            unused: dict[str | None, list[Doc]] = {p: [] for p in found}
            for path in sorted(glob.glob(str(root / binding["path"]), recursive=True)):
                name = Path(path).relative_to(root).as_posix() if Path(path).is_relative_to(root) else path
                if name not in files:
                    try:
                        files[name] = read(Path(path))
                    except (OSError, ValueError, yaml.YAMLError) as error:
                        files[name] = error
                doc = files[name]
                obs: Observation | None = None
                why: str | None = f"can't be read: {doc}"
                if not isinstance(doc, Exception):
                    obs, why = usable(name, doc, rid, platforms, target, current[rid], root, stale)
                # A platform that isn't text, such as a list, makes the observation invalid; it is noted on every
                # platform rather than used as a key.
                given = doc.get("platform") if isinstance(doc, dict) and platforms else None
                on: str | None = given if isinstance(given, str) else None
                if obs is not None:
                    found[on].append(obs)
                elif why is not None and (not isinstance(doc, dict) or doc.get("record") in (rid, None)):
                    notes.append(f"{rid}: {name} is not used: {why}")
                    for key in [on] if on in unused else list(unused):
                        unused[key].append({"file": name, "why": why})
            for platform, observations in found.items():
                result = assessment(rid, platform, target, current[rid], observations, written_at)
                if several:
                    result["binding"] = identity(SOURCE, binding)
                if unused[platform]:
                    result["x-unused"] = unused[platform]
                fresh.append(result)
        # Records whose binding left the config lose this source's assessments too.
        replace(carried, rid, fresh, lambda a: a.get("source") == SOURCE)
    return assemble(carried, target["context"], revision, written_at), list(dict.fromkeys(notes))


def findings(assessment: Doc) -> list[str]:
    """What core gaps can't tell from the core state of one of this source's assessments: a release whose scope
    passed while other criteria are open, and observations that were not used."""
    invalid = problems(assessment, ASSESSMENT_SCHEMA)
    if invalid:
        # Hand-edited or damaged state: say so rather than read fields that aren't there.
        return [f"its {SOURCE} fields are not valid: " + "; ".join(invalid)]
    where = f" on {assessment['x-platform']}" if "x-platform" in assessment else ""
    found = []
    if assessment.get("x-release-scope") == "passed" and assessment["state"] != "passed":
        found.append(f"release {assessment['x-release']} scope passed{where}, but the record is not verified")
    found += [f"{u['file']} is not used{where}: {u['why']}" for u in assessment.get("x-unused", [])]
    return found
