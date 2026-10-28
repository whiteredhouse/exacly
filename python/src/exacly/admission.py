"""Admit verification reports through one approved GitHub workflow contract.

This establishes report origin and target applicability, not honest tests or complete acceptance coverage.
Receipts are audit records: future completion must obtain them through trusted storage or revalidate them.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import fnmatch
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

import jsonschema
import yaml

from exacly import planning, query
from exacly.applicability import Target
from exacly.core import is_state
from exacly.github_evidence import ARCHIVE_LIMIT, GitHubError, json_object, no_constant
from exacly.refresh import Run, read_junit, refresh

Doc = dict[str, Any]
VERSION = "0.1.0"
WORKFLOW_PATH = ".github/workflows/verification-evidence.yml"
JOB_ID, JOB_NAME = "observe", "Observe verification"
STEPS = ("Run verification", "Package verification evidence", "Upload verification evidence")
ARTIFACT_TEMPLATE = "exacly-evidence-${{ github.run_id }}-${{ github.run_attempt }}"
REPORT_LIMIT = 8 * 1024 * 1024
MANIFEST_LIMIT = 64 * 1024
SHA = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
OBSERVER_PATHS = (
    WORKFLOW_PATH,
    "ci/package_verification_evidence.py",
    "pyproject.toml",
    "uv.lock",
    "mise.toml",
    "python/tests/*",
    "spec/tests/*",
    "tests/*",
    "conftest.py",
    "*/conftest.py",
)
POLICY_SCHEMA: Doc = planning.fields(
    {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "repository": planning.fields(
            {
                "id": {"type": "integer", "minimum": 1},
                "full_name": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
            }
        ),
        "branch": {"const": "main"},
        "workflow": planning.fields(
            {
                "path": {"const": WORKFLOW_PATH},
                "digest": planning.DIGEST,
                "job_id": {"const": JOB_ID},
                "job_name": {"const": JOB_NAME},
            }
        ),
        "observer_digest": planning.DIGEST,
        "model": planning.fields(
            {
                "revision": SHA,
                "files": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}},
                "digest": planning.DIGEST,
            }
        ),
    }
)
MANIFEST_SCHEMA: Doc = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "repository_id": {"type": "integer", "minimum": 1},
        "repository": {"type": "string"},
        "run_id": {"type": "integer", "minimum": 1},
        "run_attempt": {"type": "integer", "minimum": 1},
        "workflow_path": {"const": WORKFLOW_PATH},
        "job": {"const": JOB_ID},
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


def observer_digest(entries: list[Doc]) -> str:
    """Pin observer code, locked dependencies and all files in the controlled check collection roots."""
    selected = []
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not any(fnmatch.fnmatchcase(path, pattern) for pattern in OBSERVER_PATHS):
            continue
        if entry.get("type") == "tree":
            continue
        if not safe_path(path) or entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise ValueError("observer files must be regular Git blobs")
        if not re.fullmatch(r"[0-9a-f]{40}", str(entry.get("sha", ""))):
            raise ValueError("observer file has an invalid Git identity")
        selected.append({key: entry[key] for key in ("path", "mode", "sha")})
    names = [entry["path"] for entry in selected]
    if len(names) != len(set(names)) or not set(OBSERVER_PATHS[:5]).issubset(names):
        raise ValueError("observer closure is incomplete or ambiguous")
    if not all(
        any(path.startswith(prefix + "/") for path in names) for prefix in ("python/tests", "spec/tests", "tests")
    ):
        raise ValueError("observer check collection roots must be present")
    return planning.digest(sorted(selected, key=lambda entry: entry["path"]))


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


def workflow_contract(content: bytes) -> None:
    try:
        document = mapping(yaml.safe_load(content))
    except yaml.YAMLError:
        raise Rejected("approved workflow cannot be parsed") from None
    trigger = mapping(document.get("on", cast(dict[Any, Any], document).get(True)))
    require(
        set(trigger) == {"push"} and mapping(trigger["push"]).get("branches") == ["main"], "unsupported workflow events"
    )
    jobs = mapping(document.get("jobs"))
    require(set(jobs) == {JOB_ID}, "observer workflow must have exactly one producer job")
    job = mapping(jobs[JOB_ID])
    require(
        job.get("name") == JOB_NAME and not any(key in job for key in ("uses", "strategy")), "unsupported producer job"
    )
    steps = job.get("steps", [])
    require(isinstance(steps, list) and all(isinstance(step, dict) for step in steps), "invalid workflow steps")
    require(all(sum(step.get("name") == name for step in steps) == 1 for name in STEPS), "ambiguous producer steps")
    uploads = [step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    require(len(uploads) == 1 and uploads[0].get("name") == STEPS[2], "observer must have one designated uploader")
    require(
        mapping(uploads[0].get("with")).get("name") == ARTIFACT_TEMPLATE, "artifact name must identify run and attempt"
    )
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


def validate_policy(policy_doc: Any) -> Doc:
    try:
        jsonschema.validate(policy_doc, POLICY_SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid trust policy: {error.message}") from error
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
    name: str = "github-verification",
    version: str = "1",
) -> Doc:
    """Build candidate fingerprints from an approved checkout for independent review, without granting trust."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("repository and full immutable revision are required")
    base = f"/repos/{repository}"
    info = source.get_json(base)
    entries = tree(source, base, revision)
    workflow = blob(source, base, entries, WORKFLOW_PATH)
    workflow_contract(workflow)
    documents = {}
    for path in model_files:
        if not safe_path(path):
            raise ValueError("model files must be safe relative paths")
        documents[path] = yaml.safe_load(blob(source, base, entries, path).decode("utf-8"))
    candidate = {
        "name": name,
        "version": version,
        "repository": {"id": info.get("id"), "full_name": repository},
        "branch": "main",
        "workflow": {"path": WORKFLOW_PATH, "digest": sha256(workflow), "job_id": JOB_ID, "job_name": JOB_NAME},
        "observer_digest": observer_digest(entries),
        "model": {"revision": revision, "files": model_files, "digest": config_digest(documents)},
    }
    return validate_policy(candidate)


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
    base = f"/repos/{repository}"
    receipt: Doc = {
        "schema_version": VERSION,
        "status": "rejected",
        "policy": {"name": chosen["name"], "version": chosen["version"], "digest": planning.digest(chosen)},
        "target": selection,
        "as_of": date.isoformat(),
        "claims": {
            "scope": "origin_and_target_applicability",
            "test_honesty_verified": False,
            "coverage_verified": False,
            "self_authenticating": False,
        },
        "checks": [],
    }
    stage = "repository"
    try:
        info = source.get_json(base)
        require(
            positive_id(info.get("id"))
            and info.get("id") == chosen["repository"]["id"]
            and info.get("full_name") == repository,
            "repository identity mismatch",
        )
        branch = source.get_json(f"{base}/branches/main")
        require(branch.get("name") == "main" and branch.get("protected") is True, "main is not currently protected")
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "repository matched and main is currently protected"}
        )
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
            run.get("event") == "push" and run.get("head_branch") == "main",
            "only same-repository main push runs are supported",
        )
        require(run.get("head_sha") == selection["revision"], "run implementation identity does not match target")
        require(
            run.get("status") == "completed" and run.get("conclusion") in ("success", "failure"),
            "run is incomplete or interrupted",
        )
        require(
            run.get("path") in (WORKFLOW_PATH, WORKFLOW_PATH + "@main", WORKFLOW_PATH + "@refs/heads/main")
            and positive_id(run.get("workflow_id")),
            "unexpected observer workflow",
        )
        workflow_info = source.get_json(f"{base}/actions/workflows/{run['workflow_id']}")
        require(
            positive_id(workflow_info.get("id"))
            and workflow_info.get("id") == run["workflow_id"]
            and workflow_info.get("path") == WORKFLOW_PATH,
            "workflow identity mismatch",
        )
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "completed push run and exact attempt matched target"}
        )
        stage = "observer"
        entries = tree(source, base, selection["revision"])
        workflow = blob(source, base, entries, WORKFLOW_PATH)
        require(sha256(workflow) == chosen["workflow"]["digest"], "workflow content is not approved")
        workflow_contract(workflow)
        require(
            observer_digest(entries) == chosen["observer_digest"],
            "observer code, dependencies or check definitions changed",
        )
        documents = model_files(source, base, chosen["model"], date)
        receipt["checks"].append(
            {"code": stage, "passed": True, "reason": "approved observer closure and immutable model content matched"}
        )
        stage = "producer"
        jobs = pages(source, f"{base}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        require(len(jobs) == 1 and jobs[0].get("name") == JOB_NAME, "ambiguous or unexpected producer job")
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
        for name in STEPS:
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
        name = f"exacly-evidence-{run_id}-{attempt}"
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
            origin.get("head_sha") == selection["revision"] and origin.get("head_branch") == "main",
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
            "workflow_path": WORKFLOW_PATH,
            "job": JOB_ID,
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
        with tempfile.TemporaryDirectory(prefix="exacly-admission-") as scratch:
            path = Path(scratch) / "junit.xml"
            path.write_bytes(report)
            results = read_junit([path])
        require(
            not (check["conclusion"] == "success" and "failed" in results.values()),
            "successful producer step supplied failed checks",
        )
        observed = refresh(
            documents,
            results,
            Run(
                revision=selection["revision"],
                model_revision=selection["model_revision"],
                context=selection["context"],
                observed_at=completed,
                workflow=WORKFLOW_PATH,
                job=JOB_ID,
                job_status=check["conclusion"],
                url=f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}",
                source_url=f"https://github.com/{repository}/blob/{selection['revision']}",
            ),
        )
        incomplete = check["conclusion"] != "success" or not any(value != "skipped" for value in results.values())
        if incomplete:
            for record in observed["records"].values():
                for assessment in record.get("assessments", []):
                    if assessment["state"] == "passed":
                        assessment.update(
                            state="inconclusive",
                            reason="The producer did not establish complete successful check execution.",
                        )
        receipt["status"] = "admitted"
        receipt["origin"] = {
            "repository": repository,
            "repository_id": info["id"],
            "run_id": run_id,
            "run_attempt": attempt,
            "workflow_revision": selection["revision"],
            "workflow_path": WORKFLOW_PATH,
            "job_id": job.get("id"),
            "job_key": JOB_ID,
            "job_name": JOB_NAME,
            "artifact_id": artifact["id"],
            "artifact_name": name,
            "archive_digest": sha256(archive),
            "report_digest": sha256(report),
            "association": "approved_single_job_workflow_contract",
            "branch_protection_checked_at": dt.datetime.now(dt.UTC).isoformat(),
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
    lines += [
        f"  {check['code']}: {'accepted' if check['passed'] else 'rejected'} — {check['reason']}"
        for check in receipt["checks"]
    ]
    lines.append(
        "Origin and applicability only; test honesty and acceptance coverage are unverified. "
        "Receipts are not self-authenticating."
    )
    return "\n".join(lines)
