"""Evidence admission checks authoritative origin and retained bytes before observing results."""

import base64
import copy
import datetime as dt
import hashlib
import io
import json
import stat
import struct
import warnings
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from exacly import admission, policy, query
from exacly.applicability import Target
from test_applicability import WHEN, A, B
from test_refresh import config, requirement

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "acme/demo"
REPOSITORY_ID = 123
WORKFLOW = ".github/workflows/verification-evidence.yml"
MODEL_FILE = "spec/exacly/model.yaml"
RUN_ID, ATTEMPT, ARTIFACT_ID = 777, 1, 888
BASE = f"repos/{REPOSITORY}"
MODEL_TREE, IMPLEMENTATION_TREE = "d" * 40, "e" * 40
START, END = "2026-10-26T09:00:00Z", "2026-10-26T09:05:00Z"
CASE = "python/tests/test_requirement.py::test_requirement"
READINESS: dict[str, Any] = {
    "name": "verification",
    "version": "1",
    "requirements": [{"record": "REQ-1", "dimensions": ["verification"]}],
}


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def bundle(entries: list[tuple[str, bytes, int]]) -> bytes:
    out = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name, (2026, 10, 26, 9, 3, 0))
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return out.getvalue()


class Source:
    """Authoritative GitHub metadata and artifact bytes, with no network or external writes."""

    def __init__(self, outcome: str = "success", report: bytes | None = None) -> None:
        self.calls: list[str] = []
        self.downloads: list[tuple[str, int]] = []
        self.config = config(requirement("REQ-1", {"workflow": WORKFLOW, "job": "observe", "cases": [CASE]}))
        self.report = report or (
            b'<testsuite><testcase file="python/tests/test_requirement.py" name="test_requirement">'
            + (b'<failure message="wrong behavior"/>' if outcome == "failure" else b"")
            + b"</testcase></testsuite>"
        )
        self.manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "repository_id": REPOSITORY_ID,
            "repository": REPOSITORY,
            "run_id": RUN_ID,
            "run_attempt": ATTEMPT,
            "workflow_path": WORKFLOW,
            "job": "observe",
            "implementation_revision": B,
            "report_digest": sha256(self.report),
            "check_outcome": outcome,
        }
        self.archive = b""
        self.run: dict[str, Any] = {
            "id": RUN_ID,
            "run_attempt": ATTEMPT,
            "event": "push",
            "status": "completed",
            "conclusion": outcome,
            "head_branch": "main",
            "head_sha": B,
            "path": WORKFLOW,
            "workflow_id": 99,
            "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            "head_repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}",
            "created_at": START,
            "run_started_at": START,
            "updated_at": END,
        }
        self.job: dict[str, Any] = {
            "id": 444,
            "run_id": RUN_ID,
            "run_attempt": ATTEMPT,
            "name": "Observe verification",
            "status": "completed",
            "conclusion": outcome,
            "head_sha": B,
            "started_at": START,
            "completed_at": END,
            "steps": [
                {
                    "name": name,
                    "number": i,
                    "status": "completed",
                    "conclusion": outcome if name == "Run verification" else "success",
                    "started_at": START,
                    "completed_at": END,
                }
                for i, name in enumerate(
                    ["Run verification", "Package verification evidence", "Upload verification evidence"], 1
                )
            ],
        }
        self.artifact: dict[str, Any] = {
            "id": ARTIFACT_ID,
            "name": f"exacly-evidence-{RUN_ID}-{ATTEMPT}",
            "expired": False,
            "created_at": "2026-10-26T09:03:00Z",
            "updated_at": "2026-10-26T09:04:00Z",
            "expires_at": "2026-11-25T09:04:00Z",
            "workflow_run": {
                "id": RUN_ID,
                "repository_id": REPOSITORY_ID,
                "head_repository_id": REPOSITORY_ID,
                "head_branch": "main",
                "head_sha": B,
            },
        }
        self.pages: dict[str, dict[str, Any]] = {
            BASE: {"id": REPOSITORY_ID, "full_name": REPOSITORY, "default_branch": "main"},
            f"{BASE}/branches/main": {"name": "main", "protected": True, "commit": {"sha": B}},
            f"{BASE}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}": self.run,
            f"{BASE}/actions/workflows/99": {"id": 99, "path": WORKFLOW, "state": "active"},
            f"{BASE}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?page=1": {"total_count": 1, "jobs": [self.job]},
            f"{BASE}/actions/runs/{RUN_ID}/artifacts?page=1": {"total_count": 1, "artifacts": [self.artifact]},
            f"{BASE}/actions/artifacts/{ARTIFACT_ID}": self.artifact,
            f"{BASE}/git/commits/{A}": {"sha": A, "tree": {"sha": MODEL_TREE}},
            f"{BASE}/git/commits/{B}": {"sha": B, "tree": {"sha": IMPLEMENTATION_TREE}},
        }
        workflow = (ROOT / WORKFLOW).read_bytes()
        self.workflow = workflow
        files = {
            WORKFLOW: workflow,
            "ci/package_verification_evidence.py": b"# pinned publisher\n",
            "pyproject.toml": b"# pinned package definition\n",
            "uv.lock": b"# pinned dependencies\n",
            "mise.toml": b"# pinned tasks\n",
            "python/tests/test_requirement.py": b"# pinned acceptance checks\n",
            "spec/tests/test_schema.py": b"# pinned schema checks\n",
            "tests/test_ci.py": b"# pinned workflow checks\n",
        }
        self.tree = [self.add_blob(path, content) for path, content in files.items()]
        self.pages[f"{BASE}/git/trees/{IMPLEMENTATION_TREE}"] = {
            "sha": IMPLEMENTATION_TREE,
            "truncated": False,
            "tree": self.tree,
        }
        config_bytes = yaml.safe_dump(self.config).encode()
        self.pages[f"{BASE}/git/trees/{MODEL_TREE}"] = {
            "sha": MODEL_TREE,
            "truncated": False,
            "tree": [self.add_blob(MODEL_FILE, config_bytes)],
        }
        self.policy: dict[str, Any] = {
            "name": "trusted-ci",
            "version": "1",
            "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            "branch": "main",
            "workflow": {
                "path": WORKFLOW,
                "digest": sha256(workflow),
                "job_id": "observe",
                "job_name": "Observe verification",
            },
            "observer_digest": admission.observer_digest(self.tree),
            "model": {
                "revision": A,
                "files": [MODEL_FILE],
                "digest": admission.config_digest({MODEL_FILE: self.config}),
            },
        }
        self.rebundle()

    def add_blob(self, path: str, content: bytes) -> dict[str, Any]:
        sha = blob_sha(content)
        self.pages[f"{BASE}/git/blobs/{sha}"] = {
            "sha": sha,
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
            "size": len(content),
        }
        return {"path": path, "mode": "100644", "type": "blob", "sha": sha, "size": len(content)}

    def rebundle(self, entries: list[tuple[str, bytes, int]] | None = None) -> None:
        regular = stat.S_IFREG | 0o644
        self.archive = bundle(
            entries
            or [
                ("manifest.json", json.dumps(self.manifest).encode(), regular),
                ("junit.xml", self.report, regular),
            ]
        )
        self.artifact.update(digest=sha256(self.archive), size_in_bytes=len(self.archive))

    def get_json(self, relative_path: str) -> dict[str, Any]:
        self.calls.append(relative_path)
        url = urlsplit(relative_path)
        path = url.path.lstrip("/")
        query = parse_qs(url.query)
        if path.endswith(("/jobs", "/artifacts")):
            path += "?page=" + query.get("page", ["1"])[0]
        if path not in self.pages:
            raise AssertionError(f"unexpected source request: {relative_path}")
        return copy.deepcopy(self.pages[path])

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        self.downloads.append((repository, artifact_id))
        assert (repository, artifact_id) == (REPOSITORY, self.artifact["id"])
        return self.archive


def admit(source: Source, **selection: Any) -> Any:
    chosen = Target({"context": "main", "model_revision": A, "revision": B, **selection})
    return admission.admit(source, source.policy, chosen, RUN_ID, ATTEMPT, WHEN)


def rejected(result: Any) -> None:
    assert result.receipt["status"] == "rejected"
    assert result.state is None
    assert result.archive is None
    assert result.report is None


@pytest.mark.parametrize("outcome,expected", [("success", "passed"), ("failure", "failed")])
def test_admitted_origin_preserves_success_and_failure_observations(outcome: str, expected: str) -> None:
    source = Source(outcome)
    before = copy.deepcopy((source.policy, source.pages))
    result = admit(source)
    assert result.receipt["status"] == "admitted", result.receipt
    assert result.archive == source.archive
    assert result.report == source.report
    assert result.state["config_revision"] == A
    observed = result.state["records"]["REQ-1"]["assessments"][0]
    assert observed["state"] == expected
    assert observed["subject"] == {"model_revision": A, "revision": B}
    model = query.load({MODEL_FILE: source.config, "state": result.state}, WHEN)
    selected = Target({"context": "main", "model_revision": A, "revision": B})
    assert query.assess(model, "REQ-1", selected)["status"]["verification"] == expected
    assert policy.evaluate(model, READINESS, selected)["result"] == (
        "satisfied" if expected == "passed" else "not_satisfied"
    )
    assert result.receipt["claims"]["test_honesty_verified"] is False
    assert result.receipt["claims"]["coverage_verified"] is False
    assert result.receipt["claims"]["self_authenticating"] is False
    assert (source.policy, source.pages) == before
    assert source.downloads == [(REPOSITORY, ARTIFACT_ID)]


def test_admission_of_a_skipped_report_cannot_establish_verification() -> None:
    report = (
        b'<testsuite><testcase file="python/tests/test_requirement.py" name="test_requirement">'
        b"<skipped/></testcase></testsuite>"
    )
    source = Source(report=report)
    result = admit(source)
    assert result.receipt["status"] == "admitted", result.receipt
    assert result.state["records"]["REQ-1"]["assessments"][0]["state"] == "not_checked"


@pytest.mark.parametrize(
    "case", ["wrong_repository", "fork", "pull_request", "branch", "attempt", "incomplete", "cancelled"]
)
def test_untrusted_run_metadata_never_supplies_observations(case: str) -> None:
    source = Source()
    if case == "wrong_repository":
        source.run["repository"]["id"] += 1
    elif case == "fork":
        source.run["head_repository"]["id"] += 1
    elif case == "pull_request":
        source.run["event"] = "pull_request"
    elif case == "branch":
        source.run["head_branch"] = "feature"
    elif case == "attempt":
        source.run["run_attempt"] += 1
    elif case == "incomplete":
        source.run["status"] = "in_progress"
    else:
        source.run["conclusion"] = "cancelled"
    rejected(admit(source))


@pytest.mark.parametrize("suffix", ["", "@main", "@refs/heads/main"])
def test_run_path_accepts_the_documented_main_branch_forms(suffix: str) -> None:
    source = Source()
    source.run["path"] = WORKFLOW + suffix
    assert admit(source).receipt["status"] == "admitted"


@pytest.mark.parametrize("path", [WORKFLOW + "@feature", ".github/workflows/other.yml@main"])
def test_run_path_cannot_select_another_workflow_or_branch(path: str) -> None:
    source = Source()
    source.run["path"] = path
    rejected(admit(source))


def test_an_unprotected_branch_cannot_supply_trusted_evidence() -> None:
    source = Source()
    source.pages[f"{BASE}/branches/main"]["protected"] = False
    rejected(admit(source))


def test_malformed_authoritative_metadata_produces_a_rejected_receipt() -> None:
    source = Source()
    source.run["conclusion"] = ["success"]
    rejected(admit(source))


@pytest.mark.parametrize("identity", [None, 0, -1, True, "444", "missing"])
def test_producer_job_requires_a_positive_integer_identity(identity: Any) -> None:
    source = Source()
    if identity == "missing":
        source.job.pop("id")
    else:
        source.job["id"] = identity
    rejected(admit(source))


def test_optional_job_attempt_must_match_when_supplied() -> None:
    source = Source()
    source.job["run_attempt"] = ATTEMPT + 1
    rejected(admit(source))
    source.job.pop("run_attempt")
    assert admit(source).receipt["status"] == "admitted"


@pytest.mark.parametrize(
    "field",
    [
        "repository",
        "run_repository",
        "head_repository",
        "artifact_repository",
        "artifact_head_repository",
        "run_attempt",
        "job_attempt",
        "workflow",
        "workflow_response",
        "artifact",
    ],
)
def test_boolean_provenance_ids_cannot_stand_for_integer_one(field: str) -> None:
    source = Source()
    source.policy["repository"]["id"] = 1
    source.manifest["repository_id"] = 1
    identities = {
        "repository": source.pages[BASE],
        "run_repository": source.run["repository"],
        "head_repository": source.run["head_repository"],
    }
    for metadata in identities.values():
        metadata["id"] = 1
    origin = source.artifact["workflow_run"]
    origin["repository_id"] = origin["head_repository_id"] = 1
    if field in identities:
        identities[field]["id"] = True
    elif field.startswith("artifact_"):
        origin["repository_id" if field == "artifact_repository" else "head_repository_id"] = True
    elif field in {"run_attempt", "job_attempt"}:
        (source.run if field == "run_attempt" else source.job)["run_attempt"] = True
    elif field in {"workflow", "workflow_response"}:
        identity = True if field == "workflow" else 1
        source.run["workflow_id"] = identity
        source.pages[f"{BASE}/actions/workflows/{identity}"] = {"id": True, "path": WORKFLOW}
    else:
        source.artifact["id"] = True
        source.pages[f"{BASE}/actions/artifacts/True"] = source.artifact
    source.rebundle()
    rejected(admit(source))


def test_completed_job_after_the_selected_date_cannot_supply_an_observation() -> None:
    source = Source()
    selected = Target({"context": "main", "model_revision": A, "revision": B})
    result = admission.admit(source, source.policy, selected, RUN_ID, ATTEMPT, WHEN - dt.timedelta(days=1))
    rejected(result)
    assert source.downloads == []


@pytest.mark.parametrize("case", ["wrong_job", "duplicate_job", "missing_step", "skipped_step", "upload_failure"])
def test_required_producer_job_and_steps_must_complete(case: str) -> None:
    source = Source()
    if case == "wrong_job":
        source.job["name"] = "Untrusted job"
    elif case == "duplicate_job":
        page = source.pages[f"{BASE}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?page=1"]
        page["jobs"].append({**source.job, "id": 445})
        page["total_count"] = 2
    elif case == "missing_step":
        source.job["steps"] = source.job["steps"][:-1]
    elif case == "skipped_step":
        source.job["steps"][0]["conclusion"] = "skipped"
    else:
        source.job["steps"][-1]["conclusion"] = "failure"
    rejected(admit(source))


@pytest.mark.parametrize("case", ["expired", "wrong_run", "wrong_revision", "outside_job", "duplicate", "digest"])
def test_artifact_metadata_must_match_the_run_and_retained_bytes(case: str) -> None:
    source = Source()
    if case == "expired":
        source.artifact["expired"] = True
    elif case == "wrong_run":
        source.artifact["workflow_run"]["id"] += 1
    elif case == "wrong_revision":
        source.artifact["workflow_run"]["head_sha"] = "c" * 40
    elif case == "outside_job":
        source.artifact["created_at"] = "2026-10-26T08:00:00Z"
    elif case == "duplicate":
        page = source.pages[f"{BASE}/actions/runs/{RUN_ID}/artifacts?page=1"]
        page["artifacts"].append({**source.artifact, "id": 889})
        page["total_count"] = 2
    else:
        source.artifact["digest"] = "sha256:" + "0" * 64
    rejected(admit(source))


def test_artifact_pagination_finds_the_expected_attempt_bundle() -> None:
    source = Source()
    source.pages[f"{BASE}/actions/runs/{RUN_ID}/artifacts?page=1"] = {
        "total_count": 101,
        "artifacts": [{**source.artifact, "id": i, "name": f"other-{i}"} for i in range(100)],
    }
    source.pages[f"{BASE}/actions/runs/{RUN_ID}/artifacts?page=2"] = {
        "total_count": 101,
        "artifacts": [source.artifact],
    }
    result = admit(source)
    assert result.receipt["status"] == "admitted", result.receipt
    assert any("page=2" in call for call in source.calls)


@pytest.mark.parametrize("case", ["workflow", "observer", "model", "truncated", "symlink"])
def test_pinned_workflow_observer_and_model_content_are_required(case: str) -> None:
    source = Source()
    if case == "workflow":
        source.policy["workflow"]["digest"] = "sha256:" + "0" * 64
    elif case == "observer":
        next(entry for entry in source.tree if entry["path"].startswith("python/tests/"))["sha"] = "f" * 40
    elif case == "model":
        source.policy["model"]["digest"] = "sha256:" + "0" * 64
    elif case == "truncated":
        source.pages[f"{BASE}/git/trees/{IMPLEMENTATION_TREE}"]["truncated"] = True
    else:
        next(entry for entry in source.tree if entry["path"] == WORKFLOW)["mode"] = "120000"
    rejected(admit(source))


def test_an_approved_legacy_model_cannot_supply_split_identity_observations() -> None:
    source = Source()
    content = (ROOT / "spec/examples/0.1/invoice-reminders.yaml").read_bytes()
    legacy = yaml.safe_load(content)
    assert not query.load({MODEL_FILE: legacy}, WHEN).report.errors
    source.pages[f"{BASE}/git/trees/{MODEL_TREE}"]["tree"] = [source.add_blob(MODEL_FILE, content)]
    source.policy["model"]["digest"] = admission.config_digest({MODEL_FILE: legacy})
    rejected(admit(source))


@pytest.mark.parametrize("case", ["extra_job", "untrusted_event", "checkout_revision"])
def test_approved_fingerprints_cannot_bypass_the_supported_workflow_profile(case: str) -> None:
    source = Source()
    workflow = yaml.safe_load(source.workflow)
    if case == "extra_job":
        workflow["jobs"]["other"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}
    elif case == "untrusted_event":
        workflow[True] = {"pull_request": {}}
    else:
        workflow["jobs"]["observe"]["steps"][0]["with"]["ref"] = "unreviewed-branch"
    content = yaml.safe_dump(workflow).encode()
    index = next(i for i, entry in enumerate(source.tree) if entry["path"] == WORKFLOW)
    source.tree[index] = source.add_blob(WORKFLOW, content)
    source.policy["workflow"]["digest"] = sha256(content)
    source.policy["observer_digest"] = admission.observer_digest(source.tree)
    rejected(admit(source))


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_attempt", 2),
        ("implementation_revision", "c" * 40),
        ("report_digest", "sha256:" + "0" * 64),
        ("check_outcome", "failure"),
        ("extra", True),
    ],
)
def test_manifest_must_match_authoritative_metadata_and_report_bytes(field: str, value: Any) -> None:
    source = Source()
    source.manifest[field] = value
    source.rebundle()
    rejected(admit(source))


@pytest.mark.parametrize(
    "case",
    ["missing_report", "extra_file", "traversal", "duplicate", "symlink", "encrypted", "oversized", "corrupt_deflate"],
)
def test_unsafe_or_incomplete_archives_are_rejected_without_observing(case: str) -> None:
    source = Source()
    regular = stat.S_IFREG | 0o644
    entries = [("manifest.json", json.dumps(source.manifest).encode(), regular), ("junit.xml", source.report, regular)]
    if case == "missing_report":
        entries.pop()
    elif case == "extra_file":
        entries.append(("state.json", b"{}", regular))
    elif case == "traversal":
        entries[1] = ("../junit.xml", source.report, regular)
    elif case == "duplicate":
        entries.append(entries[1])
    elif case == "symlink":
        entries[1] = ("junit.xml", b"outside.xml", stat.S_IFLNK | 0o777)
    source.rebundle(entries)
    if case == "corrupt_deflate":
        raw = bytearray(source.archive)
        with zipfile.ZipFile(io.BytesIO(source.archive)) as archive:
            position = archive.getinfo("junit.xml").header_offset
        name_size, extra_size = struct.unpack_from("<HH", raw, position + 26)
        position += 30 + name_size + extra_size
        raw[position] = (raw[position] & ~6) | 6  # DEFLATE's reserved block type cannot be decoded.
        source.archive = bytes(raw)
        source.artifact.update(digest=sha256(source.archive), size_in_bytes=len(source.archive))
    elif case in {"encrypted", "oversized"}:
        raw = bytearray(source.archive)
        position = 0
        while (position := raw.find(b"PK\x01\x02", position)) >= 0:
            if case == "encrypted":
                flags = struct.unpack_from("<H", raw, position + 8)[0]
                struct.pack_into("<H", raw, position + 8, flags | 1)
            else:
                struct.pack_into("<I", raw, position + 24, 2**31)
            position += 4
        source.archive = bytes(raw)
        source.artifact.update(digest=sha256(source.archive), size_in_bytes=len(source.archive))
    rejected(admit(source))


@pytest.mark.parametrize(
    "report",
    [
        b"<testsuite>",
        b"<testsuite/>",
        b"<document><testcase/></document>",
        (
            b'<testsuite><error message="collection failed"/>'
            b'<testcase file="python/tests/test_requirement.py" name="test_requirement"/></testsuite>'
        ),
        (
            '<!DOCTYPE testsuite [<!ENTITY case "test_requirement">]>'
            '<testsuite><testcase file="python/tests/test_requirement.py" name="&case;"/></testsuite>'
        ).encode("utf-16"),
    ],
    ids=["incomplete", "no_cases", "wrong_root", "suite_error_with_success", "utf16_declaration"],
)
def test_invalid_or_unsupported_report_documents_cannot_supply_observations(report: bytes) -> None:
    rejected(admit(Source(report=report)))


@pytest.mark.parametrize("encoding", ["UTF-16", "fake-encoding"])
def test_utf8_report_bytes_cannot_declare_another_encoding(encoding: str) -> None:
    report = f'<?xml version="1.0" encoding="{encoding}"?>'.encode() + Source().report
    rejected(admit(Source(report=report)))


@pytest.mark.parametrize(
    "declaration",
    [
        b"",
        b'<?xml version="1.0"?>',
        b'<?xml version="1.0" encoding="UTF-8"?>',
        b'<?xml version="1.0" encoding="UTF8"?>',
    ],
    ids=["no_declaration", "no_encoding", "utf8_hyphenated", "utf8"],
)
def test_utf8_reports_accept_absent_or_matching_encoding_declarations(declaration: bytes) -> None:
    source = Source(report=declaration + Source().report)
    result = admit(source)
    assert result.receipt["status"] == "admitted", result.receipt
    assert result.report == source.report
    assert result.state["records"]["REQ-1"]["assessments"][0]["state"] == "passed"


@pytest.mark.parametrize("selection", [{"revision": "c" * 40}, {"model_revision": "c" * 40}])
def test_selected_target_cannot_be_substituted_by_admission_metadata(selection: dict[str, Any]) -> None:
    source = Source()
    if "model_revision" in selection:
        with pytest.raises(ValueError, match="target model revision"):
            admit(source, **selection)
        assert source.calls == []
    else:
        rejected(admit(source, **selection))


@pytest.mark.parametrize(
    "selection",
    [
        {"builds": {"app": {"digest": "sha256:build"}}},
        {"environment": "production"},
        {"platform": "ios"},
        {"release": "1.0"},
        {"revision": "b" * 7},
    ],
)
def test_unsupported_target_scope_and_abbreviations_are_invalid_input(selection: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        admit(Source(), **selection)


def test_invalid_trust_policy_is_rejected_before_source_access() -> None:
    source = Source()
    source.policy["allow_untrusted"] = True
    with pytest.raises(ValueError):
        admit(source)
    assert source.calls == [] and source.downloads == []


def test_source_failures_cannot_supply_an_observation() -> None:
    from exacly.github_evidence import GitHubError

    class Unavailable(Source):
        def get_json(self, relative_path: str) -> dict[str, Any]:
            raise GitHubError("GitHub could not establish run identity")

    rejected(admit(Unavailable()))
