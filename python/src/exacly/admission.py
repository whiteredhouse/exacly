"""Admit verification reports through one approved GitHub workflow contract.

This establishes report origin and target applicability, not honest tests or complete acceptance coverage.
Receipts are audit records: future completion must obtain them through trusted storage or revalidate them.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import io
import json
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import quote

import jsonschema
import yaml

from exacly import planning, query
from exacly.applicability import Target
from exacly.core import is_state
from exacly.github_evidence import ARCHIVE_LIMIT, GitHubError, json_object, no_constant
from exacly.refresh import Run, read_junit, refresh

Doc = dict[str, Any]
VERSION = "0.1.0"
RECEIPT_VERSION = "0.2.0"
REPORT_LIMIT = 8 * 1024 * 1024
MANIFEST_LIMIT = 64 * 1024
SHA = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
TEXT = {"type": "string", "pattern": r"\S"}
JOB_KEY = {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_-]*$"}
STEP_SCHEMA: Doc = planning.fields({key: TEXT for key in ("verification", "package", "upload")})
ARTIFACT_SCHEMA: Doc = planning.fields(
    {"name_prefix": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$", "maxLength": 100}}
)
WORKFLOW_PRODUCER_SCHEMA: Doc = planning.fields(
    {"path": TEXT, "job_id": JOB_KEY, "job_name": TEXT, "steps": STEP_SCHEMA}
)
PRODUCER_SCHEMA: Doc = planning.fields(
    {"branch": TEXT, "workflow": WORKFLOW_PRODUCER_SCHEMA, "artifact": ARTIFACT_SCHEMA}
)
POLICY_SCHEMA: Doc = planning.fields(
    {
        "name": TEXT,
        "version": TEXT,
        "repository": planning.fields(
            {
                "id": {"type": "integer", "minimum": 1},
                "full_name": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
            }
        ),
        "branch": TEXT,
        "branch_protection": {"enum": ["required", "not_required"]},
        "workflow": planning.fields({**WORKFLOW_PRODUCER_SCHEMA["properties"], "digest": planning.DIGEST}),
        "artifact": ARTIFACT_SCHEMA,
        "observer": planning.fields(
            {
                "paths": {"type": "array", "minItems": 1, "uniqueItems": True, "items": TEXT},
                "digest": planning.DIGEST,
            }
        ),
        "model": planning.fields(
            {
                "revision": SHA,
                "files": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}},
                "digest": planning.DIGEST,
            }
        ),
    },
    optional=("branch_protection",),
)
MANIFEST_SCHEMA: Doc = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "repository_id": {"type": "integer", "minimum": 1},
        "repository": {"type": "string"},
        "run_id": {"type": "integer", "minimum": 1},
        "run_attempt": {"type": "integer", "minimum": 1},
        "workflow_path": TEXT,
        "job": JOB_KEY,
        "implementation_revision": SHA,
        "report_digest": planning.DIGEST,
        "check_outcome": {"enum": ["success", "failure"]},
    }
)


class Source(Protocol):
    def get_json(self, path: str) -> Doc: ...

    def download_artifact(self, repository: str, artifact_id: int) -> bytes: ...


@dataclass
class AdmissionResult:
    receipt: Doc
    archive: bytes | None = None
    report: bytes | None = None
    state: Doc | None = None


class Rejected(Exception):
    pass


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise Rejected(reason)


def mapping(value: Any) -> Doc:
    require(isinstance(value, dict), "expected an object in GitHub evidence metadata")
    return dict(value)


def positive_id(value: Any) -> bool:
    return type(value) is int and value > 0


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def timestamp(value: Any) -> dt.datetime:
    require(isinstance(value, str), "missing provenance timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Rejected("invalid provenance timestamp") from None
    require(parsed.utcoffset() is not None, "provenance timestamp must identify its timezone")
    return parsed


def safe_path(path: Any) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
        and not any(ord(char) < 32 or ord(char) == 127 for char in path)
    )


def safe_workflow_path(path: Any) -> bool:
    return (
        safe_path(path)
        and len(path.split("/")) == 3
        and path.startswith(".github/workflows/")
        and PurePosixPath(path).suffix in {".yml", ".yaml"}
        and bool(PurePosixPath(path).stem)
        and not any(char in path for char in "*?[]")
    )


def safe_branch(branch: Any) -> bool:
    """An exact Git branch that is also literal in GitHub's branch-filter syntax."""
    return (
        isinstance(branch, str)
        and bool(branch)
        and branch != "@"
        and not branch.startswith("-")
        and not branch.endswith(".")
        and ".." not in branch
        and "@{" not in branch
        and not any(ord(char) <= 32 or ord(char) == 127 or char in "~^:?*[\\+!%]" for char in branch)
        and all(part and not part.startswith(".") and not part.endswith(".lock") for part in branch.split("/"))
    )


def validate_observer_paths(paths: Any) -> list[str]:
    if (
        not isinstance(paths, list)
        or not paths
        or not all(safe_path(path) and not any(char in path for char in "*?[]") for path in paths)
    ):
        raise ValueError("observer paths must be explicit safe literal file or directory paths")
    if len(paths) != len(set(paths)):
        raise ValueError("observer paths must not contain duplicate selectors")
    return list(paths)


def observer_digest(entries: list[Doc], paths: list[str]) -> str:
    """Pin selected regular Git files and absence beneath explicit literal selectors.

    A selector may name a file or a directory subtree. Empty selections intentionally pin absence;
    independent policy approval owns whether the selected coverage is sufficient.
    """
    selectors = validate_observer_paths(paths)
    selected: list[Doc] = []
    relevant: set[str] = set()
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        included = any(path == selector or path.startswith(selector + "/") for selector in selectors)
        ancestor = any(selector.startswith(path + "/") for selector in selectors)
        if not included and not ancestor:
            continue
        if not safe_path(path) or path in relevant:
            raise ValueError("observer Git paths are unsafe or ambiguous")
        relevant.add(path)
        if entry.get("type") == "tree" and entry.get("mode") == "040000":
            continue
        if ancestor:
            raise ValueError("observer path has a non-directory ancestor")
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise ValueError("observer files must be regular Git blobs")
        if not isinstance(entry.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", entry["sha"]):
            raise ValueError("observer file has an invalid Git identity")
        selected.append({key: entry[key] for key in ("path", "mode", "sha")})
    return planning.digest({"paths": sorted(selectors), "files": sorted(selected, key=lambda entry: entry["path"])})


def config_digest(documents: Doc) -> str:
    return planning.digest(sorted(documents.values(), key=planning.canonical))


def tree(source: Source, base: str, revision: str) -> list[Doc]:
    commit = source.get_json(f"{base}/git/commits/{revision}")
    require(commit.get("sha") == revision, "Git commit identity does not match the selected revision")
    identity = mapping(commit.get("tree")).get("sha")
    require(isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{40}", identity), "invalid Git tree identity")
    found = source.get_json(f"{base}/git/trees/{identity}?recursive=1")
    require(found.get("sha") == identity and found.get("truncated") is False, "Git tree is incomplete")
    entries = found.get("tree")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise Rejected("invalid Git tree")
    return list(entries)


def blob(source: Source, base: str, entries: list[Doc], path: str) -> bytes:
    found = [entry for entry in entries if entry.get("path") == path]
    require(len(found) == 1, "required approved file is missing or ambiguous")
    entry = found[0]
    require(entry.get("type") == "blob" and entry.get("mode") in ("100644", "100755"), "approved file is not regular")
    identity = entry.get("sha")
    require(isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{40}", identity), "invalid Git blob identity")
    value = source.get_json(f"{base}/git/blobs/{identity}")
    require(value.get("sha") == identity and value.get("encoding") == "base64", "unsupported Git blob response")
    require(isinstance(value.get("content"), str), "invalid Git blob encoding")
    try:
        content = base64.b64decode("".join(value["content"].split()), validate=True)
    except (KeyError, AttributeError, ValueError):
        raise Rejected("invalid Git blob encoding") from None
    require(len(content) <= REPORT_LIMIT, "approved file exceeds the permitted size")
    actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    require(actual == identity, "Git blob bytes do not match their identity")
    return content


def model_files(source: Source, base: str, selection: Doc, date: dt.date) -> Doc:
    entries = tree(source, base, selection["revision"])
    documents = {}
    for path in selection["files"]:
        try:
            documents[path] = yaml.safe_load(blob(source, base, entries, path).decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            raise Rejected("approved model file cannot be parsed") from None
    require(not any(is_state(doc) for doc in documents.values()), "model files cannot supply observed state")
    require(
        all(isinstance(doc, dict) and doc.get("exacly") == "0.2.0" for doc in documents.values()),
        "GitHub admission requires a 0.2 model",
    )
    try:
        loaded = query.load(documents, date, revision=selection["revision"])
    except query.QueryError:
        raise Rejected("approved model does not match the config schema") from None
    require(not loaded.report.errors, "approved model does not conform")
    require(config_digest(documents) == selection["digest"], "approved model content digest does not match")
    return documents


def pages(source: Source, path: str, field: str) -> list[Doc]:
    result: list[Doc] = []
    for page in range(1, 101):
        found = source.get_json(f"{path}?per_page=100&page={page}")
        count, items = found.get("total_count"), found.get(field)
        if type(count) is not int or count < 0 or not isinstance(items, list):
            raise Rejected("invalid paginated GitHub metadata")
        require(all(isinstance(item, dict) for item in items), "invalid GitHub metadata entry")
        result.extend(items)
        if len(result) == count:
            return result
        require(items and len(result) < count, "GitHub metadata pagination is incomplete")
    raise Rejected("GitHub metadata exceeds the supported page limit")


def workflow_contract(content: bytes, selected: Doc) -> None:
    try:
        document = mapping(yaml.safe_load(content))
    except yaml.YAMLError:
        raise Rejected("approved workflow cannot be parsed") from None
    workflow = selected["workflow"]
    trigger = mapping(document.get("on", cast(dict[Any, Any], document).get(True)))
    require(set(trigger) == {"push"}, "unsupported workflow events")
    push = mapping(trigger["push"])
    require(
        set(push) == {"branches"} and push["branches"] == [selected["branch"]],
        "unsupported workflow branch selector",
    )
    jobs = mapping(document.get("jobs"))
    require(set(jobs) == {workflow["job_id"]}, "observer workflow must have exactly one selected producer job")
    job = mapping(jobs[workflow["job_id"]])
    require(
        job.get("name") == workflow["job_name"] and not any(key in job for key in ("uses", "strategy")),
        "unsupported producer job",
    )
    require(job.get("continue-on-error", False) is False, "producer job must preserve verification failures")
    steps = job.get("steps", [])
    require(isinstance(steps, list) and all(isinstance(step, dict) for step in steps), "invalid workflow steps")
    names = workflow["steps"]
    require(
        all(sum(step.get("name") == name for step in steps) == 1 for name in names.values()),
        "ambiguous producer steps",
    )
    verification = next(step for step in steps if step.get("name") == names["verification"])
    require(
        verification.get("continue-on-error", False) is False, "verification step must preserve verification failures"
    )
    uploads = [step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    require(
        len(uploads) == 1 and uploads[0].get("name") == names["upload"], "observer must have one designated uploader"
    )
    template = selected["artifact"]["name_prefix"] + "-${{ github.run_id }}-${{ github.run_attempt }}"
    require(mapping(uploads[0].get("with")).get("name") == template, "artifact name must identify run and attempt")
    checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]
    require(len(checkouts) == 1, "observer must have one explicit implementation checkout")
    require(mapping(checkouts[0].get("with")).get("ref") == "${{ github.sha }}", "checkout must select the run commit")


def unpack(archive: bytes) -> tuple[Doc, bytes]:
    require(len(archive) <= ARCHIVE_LIMIT, "artifact archive exceeds the permitted size")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            require(
                len(entries) == 2 and {item.filename for item in entries} == {"manifest.json", "junit.xml"},
                "unexpected archive members",
            )
            for item in entries:
                mode = stat.S_IFMT(item.external_attr >> 16)
                require(
                    mode in {0, stat.S_IFREG}
                    and not item.flag_bits & 1
                    and item.orig_filename == item.filename
                    and item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    "archive members must be unencrypted regular files",
                )
                limit = MANIFEST_LIMIT if item.filename == "manifest.json" else REPORT_LIMIT
                require(0 <= item.file_size <= limit, "artifact member exceeds the permitted size")
            manifest_bytes = zipped.read("manifest.json")
            report = zipped.read("junit.xml")
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=json_object, parse_constant=no_constant)
        jsonschema.validate(manifest, MANIFEST_SCHEMA)
    except (
        zipfile.BadZipFile,
        RuntimeError,
        UnicodeDecodeError,
        ValueError,
        jsonschema.ValidationError,
        zlib.error,
        EOFError,
        OSError,
        RecursionError,
    ):
        raise Rejected("artifact archive or manifest is invalid") from None
    require(safe_workflow_path(manifest["workflow_path"]), "manifest workflow path is invalid")
    require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", manifest["job"]), "manifest job key is invalid")
    require(sha256(report) == manifest["report_digest"], "retained report digest does not match manifest")
    try:
        report_text = report.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("retained report must be UTF-8 XML") from None
    require(
        "\0" not in report_text and "<!DOCTYPE" not in report_text.upper() and "<!ENTITY" not in report_text.upper(),
        "report declarations are unsupported",
    )
    declaration = re.match(r"^\ufeff?<\?xml\s+([^?]*)\?>", report_text)
    if declaration:
        encoding = re.search(r"\bencoding\s*=\s*(['\"])(.*?)\1", declaration[1])
        require(encoding is None or encoding[2].lower() in ("utf-8", "utf8"), "retained report must declare UTF-8")
    try:
        root = ET.fromstring(report)
    except (ET.ParseError, ValueError, LookupError):
        raise Rejected("retained report is not complete JUnit XML") from None
    require(
        root.tag in {"testsuite", "testsuites"} and next(root.iter("testcase"), None) is not None,
        "report has no usable test cases",
    )
    require(
        manifest["check_outcome"] != "success"
        or not any(element.tag in {"error", "failure"} for element in root.iter()),
        "successful producer step supplied failed checks",
    )
    return dict(manifest), report


def validate_producer(producer: Any) -> Doc:
    try:
        jsonschema.validate(producer, PRODUCER_SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid producer contract: {error.message}") from error
    if not safe_branch(producer["branch"]):
        raise ValueError("producer branch must be a safe exact Git branch")
    workflow = producer["workflow"]
    if not safe_workflow_path(workflow["path"]):
        raise ValueError("producer workflow must name one file in .github/workflows")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", workflow["job_id"]):
        raise ValueError("producer job ID must be a valid GitHub Actions key")
    prefix = producer["artifact"]["name_prefix"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", prefix):
        raise ValueError("producer artifact prefix must be a safe literal name")
    names = list(workflow["steps"].values())
    if len(set(names)) != len(names):
        raise ValueError("producer step names must be distinct")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in name) for name in [workflow["job_name"], *names]):
        raise ValueError("producer job and step names cannot contain control characters")
    return copy.deepcopy(producer)


def validate_policy(policy_doc: Any) -> Doc:
    try:
        jsonschema.validate(policy_doc, POLICY_SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid trust policy: {error.message}") from error
    validate_producer(
        {
            "branch": policy_doc["branch"],
            "workflow": {key: value for key, value in policy_doc["workflow"].items() if key != "digest"},
            "artifact": policy_doc["artifact"],
        }
    )
    validate_observer_paths(policy_doc["observer"]["paths"])
    for path in policy_doc["model"]["files"]:
        if not safe_path(path) or PurePosixPath(path).suffix not in {".yaml", ".yml", ".json"}:
            raise ValueError("trust policy model files must be safe relative config paths")
    if any(part in {".", ".."} for part in policy_doc["repository"]["full_name"].split("/")):
        raise ValueError("invalid trust policy repository")
    return copy.deepcopy(policy_doc)


def propose_policy(
    source: Source,
    repository: str,
    revision: str,
    model_files: list[str],
    *,
    producer: Doc,
    observer_paths: list[str],
    name: str = "github-verification",
    version: str = "1",
    branch_protection: str | None = None,
) -> Doc:
    """Prepare explicit producer/model fingerprints for independent approval; grant no trust."""
    if branch_protection is not None and branch_protection not in ("required", "not_required"):
        raise ValueError("branch protection must be required or not_required")
    contract = validate_producer(producer)
    selectors = validate_observer_paths(observer_paths)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("repository and full immutable revision are required")
    if (
        not model_files
        or len(model_files) != len(set(model_files))
        or not all(safe_path(path) and PurePosixPath(path).suffix in {".yaml", ".yml", ".json"} for path in model_files)
    ):
        raise ValueError("model files must be distinct safe relative config paths")
    base = f"/repos/{repository}"
    info = source.get_json(base)
    entries = tree(source, base, revision)
    workflow = blob(source, base, entries, contract["workflow"]["path"])
    workflow_contract(workflow, contract)
    documents = {}
    for path in model_files:
        documents[path] = yaml.safe_load(blob(source, base, entries, path).decode("utf-8"))
    candidate = {
        "name": name,
        "version": version,
        "repository": {"id": info.get("id"), "full_name": repository},
        **contract,
        "workflow": {**contract["workflow"], "digest": sha256(workflow)},
        "observer": {"paths": selectors, "digest": observer_digest(entries, selectors)},
        "model": {"revision": revision, "files": model_files, "digest": config_digest(documents)},
    }
    if branch_protection is not None:
        candidate["branch_protection"] = branch_protection
    return validate_policy(candidate)


def observe_report(documents: Doc, report: bytes, run: Run) -> Doc:
    """Derive observations from retained, validated JUnit bytes and explicit producer metadata.

    This pure observation contract performs no live provenance checks. Admission calls it only after
    validating the producer and archive; completion-journal validation uses it to rederive the same
    observations from its retained proof. Fresh state lineage is generated independently on each call.
    """
    require(isinstance(report, bytes) and len(report) <= REPORT_LIMIT, "report exceeds the permitted size")
    with tempfile.TemporaryDirectory(prefix="exacly-admission-") as scratch:
        path = Path(scratch) / "junit.xml"
        path.write_bytes(report)
        results = read_junit([path])
    require(
        not (run.job_status == "success" and "failed" in results.values()),
        "successful producer step supplied failed checks",
    )
    observed = refresh(documents, results, run)
    incomplete = run.job_status != "success" or not any(value != "skipped" for value in results.values())
    if incomplete:
        for record in observed["records"].values():
            for assessment in record.get("assessments", []):
                if assessment["state"] == "passed":
                    assessment.update(
                        state="inconclusive",
                        reason="The producer did not establish complete successful check execution.",
                    )
    return observed


def admit(source: Source, policy_doc: Any, target: Target, run_id: int, attempt: int, date: dt.date) -> AdmissionResult:
    """Fetch live provenance and retained bytes before producing fresh observations; never accept supplied state."""
    chosen = validate_policy(policy_doc)
    selection = Target(copy.deepcopy(target.data)).data
    if type(date) is not dt.date or type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1:
        raise ValueError("an explicit date and positive run and attempt are required")
    if set(selection) != {"context", "model_revision", "revision"} or not all(
        re.fullmatch(r"[0-9a-f]{40}", str(selection[key])) for key in ("model_revision", "revision")
    ):
        raise ValueError(
            "GitHub admission supports source verification targets with full model and implementation revisions"
        )
    if selection["model_revision"] != chosen["model"]["revision"]:
        raise ValueError("target model revision must match the independently approved trust policy")
    repository = chosen["repository"]["full_name"]
    selected_branch = chosen["branch"]
    selected_workflow = chosen["workflow"]
    workflow_path = selected_workflow["path"]
    job_key, job_name = selected_workflow["job_id"], selected_workflow["job_name"]
    base = f"/repos/{repository}"
    receipt: Doc = {
        "schema_version": RECEIPT_VERSION,
        "status": "rejected",
        "policy": {"name": chosen["name"], "version": chosen["version"], "digest": planning.digest(chosen)},
        "target": selection,
        "as_of": date.isoformat(),
        "claims": {
            "scope": "origin_and_target_applicability",
            "branch_protection_verified": False,
            "test_honesty_verified": False,
            "coverage_verified": False,
            "self_authenticating": False,
        },
        "checks": [],
    }
    protection_checked_at: str | None = None
    stage = "repository"
    try:
        info = source.get_json(base)
        require(
            positive_id(info.get("id"))
            and info.get("id") == chosen["repository"]["id"]
            and info.get("full_name") == repository,
            "repository identity mismatch",
        )
        branch = source.get_json(f"{base}/branches/{quote(selected_branch, safe='')}")
        require(branch.get("name") == selected_branch, "selected branch identity does not match")
        if chosen.get("branch_protection", "required") == "required":
            require(branch.get("protected") is True, "selected branch is not currently protected")
            protection_checked_at = dt.datetime.now(dt.UTC).isoformat()
            receipt["claims"]["branch_protection_verified"] = True
            repository_reason = "repository matched and selected branch is currently protected"
        else:
            repository_reason = (
                "repository and branch matched; branch protection is not required by the approved policy"
            )
        receipt["checks"].append({"code": stage, "passed": True, "reason": repository_reason})
        stage = "run"
        run = source.get_json(f"{base}/actions/runs/{run_id}/attempts/{attempt}")
        require(
            positive_id(run.get("id"))
            and run.get("id") == run_id
            and positive_id(run.get("run_attempt"))
            and run.get("run_attempt") == attempt,
            "run attempt does not match",
        )
        require(
            all(
                positive_id(mapping(run.get(key)).get("id")) and mapping(run.get(key)).get("id") == info["id"]
                for key in ("repository", "head_repository")
            ),
            "run is from another repository",
        )
        require(
            run.get("event") == "push" and run.get("head_branch") == selected_branch,
            "only same-repository push runs on the selected branch are supported",
        )
        require(run.get("head_sha") == selection["revision"], "run implementation identity does not match target")
        require(
            run.get("status") == "completed" and run.get("conclusion") in ("success", "failure"),
            "run is incomplete or interrupted",
        )
        require(
            run.get("path")
            in (workflow_path, workflow_path + "@" + selected_branch, workflow_path + "@refs/heads/" + selected_branch)
            and positive_id(run.get("workflow_id")),
            "unexpected observer workflow",
        )
        workflow_info = source.get_json(f"{base}/actions/workflows/{run['workflow_id']}")
        require(
            positive_id(workflow_info.get("id"))
            and workflow_info.get("id") == run["workflow_id"]
            and workflow_info.get("path") == workflow_path,
            "workflow identity mismatch",
        )
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "completed push run and exact attempt matched target"}
        )
        stage = "observer"
        entries = tree(source, base, selection["revision"])
        workflow = blob(source, base, entries, workflow_path)
        require(sha256(workflow) == chosen["workflow"]["digest"], "workflow content is not approved")
        workflow_contract(workflow, chosen)
        require(
            observer_digest(entries, chosen["observer"]["paths"]) == chosen["observer"]["digest"],
            "observer code, dependencies or check definitions changed",
        )
        documents = model_files(source, base, chosen["model"], date)
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "approved observer closure and immutable model content matched"}
        )
        stage = "producer"
        jobs = pages(source, f"{base}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        require(len(jobs) == 1 and jobs[0].get("name") == job_name, "ambiguous or unexpected producer job")
        job = jobs[0]
        require(
            positive_id(job.get("id"))
            and positive_id(job.get("run_id"))
            and job.get("run_id") == run_id
            and job.get("head_sha") == selection["revision"]
            and ("run_attempt" not in job or (positive_id(job["run_attempt"]) and job["run_attempt"] == attempt)),
            "producer job identity mismatch",
        )
        require(
            job.get("status") == "completed" and job.get("conclusion") in ("success", "failure"),
            "producer job is incomplete or interrupted",
        )
        steps = job.get("steps", [])
        require(isinstance(steps, list) and all(isinstance(step, dict) for step in steps), "producer steps are invalid")
        selected_steps = []
        for name in (selected_workflow["steps"][key] for key in ("verification", "package", "upload")):
            found = [step for step in steps if step.get("name") == name]
            require(
                len(found) == 1 and found[0].get("status") == "completed",
                "required producer step is missing or incomplete",
            )
            selected_steps.append(found[0])
        check, package, upload = selected_steps
        require(
            check.get("conclusion") in ("success", "failure")
            and package.get("conclusion") == upload.get("conclusion") == "success",
            "producer checks or retention did not complete",
        )
        require(
            job["conclusion"] == run["conclusion"] == check["conclusion"],
            "producer outcome contradicts the approved workflow contract",
        )
        start, completed = timestamp(job.get("started_at")), timestamp(job.get("completed_at"))
        require(start <= completed, "producer timestamps are invalid")
        require(completed.astimezone(dt.UTC).date() <= date, "producer completed after the selected date")
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "exact-attempt single producer completed checks and retention"}
        )
        stage = "artifact"
        name = f"{chosen['artifact']['name_prefix']}-{run_id}-{attempt}"
        matches = [
            artifact
            for artifact in pages(source, f"{base}/actions/runs/{run_id}/artifacts", "artifacts")
            if artifact.get("name") == name
        ]
        require(len(matches) == 1 and positive_id(matches[0].get("id")), "artifact is missing or ambiguous")
        artifact = source.get_json(f"{base}/actions/artifacts/{matches[0]['id']}")
        require(
            positive_id(artifact.get("id"))
            and artifact.get("id") == matches[0]["id"]
            and artifact.get("name") == name
            and artifact.get("expired") is False,
            "artifact identity or availability changed",
        )
        origin = mapping(artifact.get("workflow_run"))
        require(
            all(positive_id(origin.get(key)) for key in ("id", "repository_id", "head_repository_id"))
            and origin.get("id") == run_id
            and origin.get("repository_id") == origin.get("head_repository_id") == info["id"],
            "artifact workflow origin does not match",
        )
        require(
            origin.get("head_sha") == selection["revision"] and origin.get("head_branch") == selected_branch,
            "artifact implementation does not match",
        )
        require(
            start <= timestamp(artifact.get("created_at")) <= completed, "artifact is outside the producer job interval"
        )
        require(timestamp(artifact.get("expires_at")).date() >= date, "artifact has expired for the selected date")
        archive = source.download_artifact(repository, artifact["id"])
        require(artifact.get("digest") == sha256(archive), "artifact archive digest does not match GitHub metadata")
        manifest, report = unpack(archive)
        expected = {
            "repository_id": info["id"],
            "repository": repository,
            "run_id": run_id,
            "run_attempt": attempt,
            "workflow_path": workflow_path,
            "job": job_key,
            "implementation_revision": selection["revision"],
            "check_outcome": check["conclusion"],
        }
        require(
            all(manifest.get(key) == value for key, value in expected.items()),
            "manifest contradicts authoritative provenance",
        )
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "unique attempt artifact, archive and report digests matched"}
        )
        stage = "observation"
        observed = observe_report(
            documents,
            report,
            Run(
                revision=selection["revision"],
                model_revision=selection["model_revision"],
                context=selection["context"],
                observed_at=completed,
                workflow=workflow_path,
                job=job_key,
                job_status=check["conclusion"],
                url=f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}",
                source_url=f"https://github.com/{repository}/blob/{selection['revision']}",
            ),
        )
        receipt["status"] = "admitted"
        receipt["origin"] = {
            "repository": repository,
            "repository_id": info["id"],
            "run_id": run_id,
            "run_attempt": attempt,
            "workflow_revision": selection["revision"],
            "workflow_path": workflow_path,
            "job_id": job.get("id"),
            "job_key": job_key,
            "job_name": job_name,
            "artifact_id": artifact["id"],
            "artifact_name": name,
            "archive_digest": sha256(archive),
            "report_digest": sha256(report),
            "association": "approved_single_job_workflow_contract",
            "branch_protection": chosen.get("branch_protection", "required"),
            "branch_protection_checked_at": protection_checked_at,
        }
        receipt["state_digest"] = planning.digest(observed)
        receipt["checks"].append(
            {
                "code": stage,
                "passed": True,
                "reason": "report produced fresh observations; admission does not establish passed verification",
            }
        )
        receipt["digest"] = planning.digest(receipt)
        return AdmissionResult(receipt, archive, report, observed)
    except (Rejected, GitHubError, ValueError, query.QueryError) as error:
        receipt["checks"].append({"code": stage, "passed": False, "reason": str(error)})
        receipt["digest"] = planning.digest(receipt)
        return AdmissionResult(receipt)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        receipt["checks"].append({"code": stage, "passed": False, "reason": "malformed evidence metadata"})
        receipt["digest"] = planning.digest(receipt)
        return AdmissionResult(receipt)


def as_text(receipt: Doc) -> str:
    lines = [f"GitHub evidence admission: {receipt['status']}", f"Receipt: {receipt['digest']}"]
    if "branch_protection_verified" in receipt["claims"]:
        protection = "verified" if receipt["claims"]["branch_protection_verified"] else "not verified"
        lines.append(f"Branch protection: {protection}")
    lines += [
        f"  {check['code']}: {'accepted' if check['passed'] else 'rejected'} — {check['reason']}"
        for check in receipt["checks"]
    ]
    lines.append(
        "Origin and applicability only; test honesty and acceptance coverage are unverified. "
        "Receipts are not self-authenticating."
    )
    return "\n".join(lines)
