"""Exercise installed four-producer composition outside the source checkout.

GitHub metadata and archive bytes are deterministic local fixtures. Socket creation is blocked
with a positive control. This tests installed command and retention behavior, not live provenance.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.metadata
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

Doc = dict[str, Any]
REPOSITORY = "acme/composition-smoke"
REPOSITORY_ID = 456
MODEL_REVISION, REVISION = "a" * 40, "b" * 40
TREE = "c" * 40
MODEL_FILE = "spec/composition/model.json"
PLATFORMS = ("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64")
DATE = dt.date(2026, 10, 30)
START, END = "2026-10-30T09:00:00Z", "2026-10-30T09:05:00Z"
TOKEN = "composition-smoke-fixture-token"
CONSOLE_WRAPPER = (
    "import runpy,sys; helper=runpy.run_path(sys.argv[1]); "
    "helper['console'](sys.argv[3],sys.argv[4:],require_installed=sys.argv[2]=='installed')"
)


def installation(*, require_installed: bool = True) -> Doc:
    import exacly

    module = Path(exacly.__file__).resolve()
    distribution = importlib.metadata.distribution("exacly")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if require_installed and (
        direct.get("dir_info", {}).get("editable") is True
        or not any(
            entry.parts == ("exacly", "__init__.py") and Path(str(distribution.locate_file(entry))).resolve() == module
            for entry in distribution.files or ()
        )
    ):
        raise RuntimeError(f"composition smoke requires an installed package, found {module}")
    return {"version": distribution.version, "module": str(module)}


def producer(index: int) -> Doc:
    member = PLATFORMS[index]
    return {
        "branch": "main",
        "workflow": {
            "path": f".github/workflows/{member}.yml",
            "job_id": "observe",
            "job_name": f"Observe {member}",
            "steps": {"verification": "Verify", "package": "Package", "upload": "Retain"},
        },
        "artifact": {"name_prefix": f"native-{member}"},
    }


def model() -> Doc:
    common = {"owner": "team", "lifecycle": "active", "evidence": [], "last_reviewed_at": DATE.isoformat()}

    def record(rid: str, kind: str, **fields: Any) -> Doc:
        return {"id": rid, "type": kind, "name": rid, "description": "Composition smoke fixture.", **common, **fields}

    requirements = [
        record(
            f"REQ-{index + 1}",
            "Requirement",
            statement=f"Check {member}.",
            acceptance_criteria=["The selected producer reports the matching check."],
            verification_method="Native fixture case",
            observe={
                "tests": {
                    "workflow": producer(index)["workflow"]["path"],
                    "job": "observe",
                    "cases": [f"tests/test_native.py::test_member_{index}"],
                }
            },
        )
        for index, member in enumerate(PLATFORMS)
    ]
    return {
        "exacly": "0.2.0",
        "model": {
            "namespace": "composition-smoke",
            "product": "Composition smoke",
            "owner": "team",
            "scope": {"covers": "Fixture", "excludes": []},
            "environment": "main",
            "review_interval_days": 90,
        },
        "records": [
            record("OUT-1", "Outcome", beneficiary="Teams", success_criterion="Done", evaluation_period="Quarterly"),
            record("MEAS-1", "Measure", lifecycle="proposed"),
            record("CAP-1", "Capability", scope="Fixture"),
            record(
                "PROC-1",
                "Process",
                perspective="business",
                actors=["Team"],
                trigger="Start",
                steps=["Work"],
                completion="Done",
                exceptions=[],
            ),
            record(
                "APP-1",
                "Application",
                responsibility="Fixture",
                interfaces=["CLI"],
                implementation_ref={"locator": "src/service.py"},
            ),
            *requirements,
        ],
        "relationships": [
            {"source": "MEAS-1", "type": "evaluates", "target": "OUT-1"},
            {"source": "CAP-1", "type": "contributes_to", "target": "OUT-1"},
            {"source": "PROC-1", "type": "uses_capability", "target": "CAP-1"},
            {"source": "PROC-1", "type": "uses", "target": "APP-1"},
            *({"source": requirement["id"], "type": "supports", "target": "PROC-1"} for requirement in requirements),
            *({"source": "APP-1", "type": "implements", "target": requirement["id"]} for requirement in requirements),
        ],
    }


def workflow(contract: Doc) -> bytes:
    return json.dumps(
        {
            "on": {"push": {"branches": ["main"]}},
            "jobs": {
                "observe": {
                    "name": contract["workflow"]["job_name"],
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"uses": "actions/checkout@" + "1" * 40, "with": {"ref": "${{ github.sha }}"}},
                        {"name": "Verify", "run": "pytest tests --junitxml=out/junit.xml"},
                        {"name": "Package", "run": "exacly evidence package --report out/junit.xml --out out/evidence"},
                        {
                            "name": "Retain",
                            "uses": "actions/upload-artifact@" + "2" * 40,
                            "with": {
                                "name": contract["artifact"]["name_prefix"]
                                + "-${{ github.run_id }}-${{ github.run_attempt }}"
                            },
                        },
                    ],
                }
            },
        }
    ).encode()


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class Source:
    """Four separately authorized producers using native GitHub/manifest/JUnit validation."""

    def __init__(self, mode: str = "success") -> None:
        self.calls: list[str] = []
        self.downloads: list[tuple[str, int]] = []
        self.archives: dict[int, bytes] = {}
        base = f"repos/{REPOSITORY}"
        self.pages: Doc = {
            base: {"id": REPOSITORY_ID, "full_name": REPOSITORY, "default_branch": "main"},
            f"{base}/branches/main": {"name": "main", "protected": True},
            **{
                f"{base}/git/commits/{revision}": {"sha": revision, "tree": {"sha": TREE}}
                for revision in (MODEL_REVISION, REVISION)
            },
        }
        files = {producer(i)["workflow"]["path"]: workflow(producer(i)) for i in range(4)}
        files.update({MODEL_FILE: json.dumps(model()).encode(), "tests/test_native.py": b"# reviewed fixture checks\n"})
        entries = []
        for path, content in files.items():
            identity = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": identity})
            self.pages[f"{base}/git/blobs/{identity}"] = {
                "sha": identity,
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
            }
        self.pages[f"{base}/git/trees/{TREE}"] = {"sha": TREE, "truncated": False, "tree": entries}
        for index, _member in enumerate(PLATFORMS):
            contract = producer(index)
            run_id, artifact_id, workflow_id = 700 + index, 800 + index, 900 + index
            outcome = "failure" if mode == "failure" and index == 3 else "success"
            fragment = '<failure message="native fixture failed"/>' if outcome == "failure" else ""
            report = (
                f'<testsuite><testcase file="tests/test_native.py" name="test_member_{index}">'
                f"{fragment}</testcase></testsuite>"
            ).encode()
            manifest = {
                "schema_version": "0.1.0",
                "repository_id": REPOSITORY_ID,
                "repository": REPOSITORY,
                "run_id": run_id,
                "run_attempt": 1,
                "workflow_path": contract["workflow"]["path"],
                "job": "observe",
                "implementation_revision": REVISION,
                "report_digest": digest(report),
                "check_outcome": outcome,
            }
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                for name, content in (("manifest.json", json.dumps(manifest).encode()), ("junit.xml", report)):
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = (stat.S_IFREG | 0o600) << 16
                    bundle.writestr(entry, content)
            self.archives[artifact_id] = archive.getvalue()
            run: Doc = {
                "id": run_id,
                "run_attempt": 1,
                "event": "push",
                "status": "completed",
                "conclusion": outcome,
                "head_branch": "main",
                "head_sha": REVISION,
                "path": contract["workflow"]["path"],
                "workflow_id": workflow_id,
                "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
                "head_repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            }
            job = {
                "id": 1000 + index,
                "run_id": run_id,
                "run_attempt": 1,
                "name": contract["workflow"]["job_name"],
                "status": "completed",
                "conclusion": outcome,
                "head_sha": REVISION,
                "started_at": START,
                "completed_at": END,
                "steps": [
                    {"name": name, "status": "completed", "conclusion": outcome if name == "Verify" else "success"}
                    for name in ("Verify", "Package", "Retain")
                ],
            }
            artifact = {
                "id": artifact_id,
                "name": f"{contract['artifact']['name_prefix']}-{run_id}-1",
                "expired": False,
                "created_at": "2026-10-30T09:03:00Z",
                "expires_at": "2026-11-30T09:03:00Z",
                "digest": digest(self.archives[artifact_id]),
                "workflow_run": {
                    "id": run_id,
                    "repository_id": REPOSITORY_ID,
                    "head_repository_id": REPOSITORY_ID,
                    "head_branch": "main",
                    "head_sha": REVISION,
                },
            }
            self.pages.update(
                {
                    f"{base}/actions/runs/{run_id}/attempts/1": run,
                    f"{base}/actions/workflows/{workflow_id}": {
                        "id": workflow_id,
                        "path": contract["workflow"]["path"],
                    },
                    f"{base}/actions/runs/{run_id}/attempts/1/jobs?page=1": {"total_count": 1, "jobs": [job]},
                    f"{base}/actions/runs/{run_id}/artifacts?page=1": {"total_count": 1, "artifacts": [artifact]},
                    f"{base}/actions/artifacts/{artifact_id}": artifact,
                }
            )

    def get_json(self, path: str) -> Doc:
        self.calls.append(path)
        url = urlsplit(path)
        key = unquote(url.path).lstrip("/")
        if key.endswith(("/jobs", "/artifacts")):
            key += "?page=" + parse_qs(url.query).get("page", ["1"])[0]
        if key not in self.pages:
            raise RuntimeError(f"unexpected composition fixture GitHub request: {path}")
        return copy.deepcopy(self.pages[key])

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        self.downloads.append((repository, artifact_id))
        if repository != REPOSITORY or artifact_id not in self.archives:
            raise RuntimeError("unexpected composition fixture artifact")
        return self.archives[artifact_id]


def scenario(root: Path) -> tuple[Path, Path, Source]:
    """Write a complete four-member definition and invocation using fixture-derived policies."""
    from exacly import admission, composition_io

    root.mkdir(parents=True)
    source = Source()
    model_path = root / "models" / MODEL_FILE
    model_path.parent.mkdir(parents=True)
    model_path.write_text(json.dumps(model()))
    members = []
    selections = []
    for index, member in enumerate(PLATFORMS):
        trusted = admission.propose_policy(
            source,
            REPOSITORY,
            MODEL_REVISION,
            [MODEL_FILE],
            producer=producer(index),
            observer_paths=[producer(index)["workflow"]["path"], "tests"],
            name=f"native-{member}",
            version="1",
        )
        readiness = {
            "name": f"ready-{member}",
            "version": "1",
            "requirements": [{"record": f"REQ-{index + 1}", "dimensions": ["verification"]}],
        }
        (root / f"trust-{member}.json").write_text(json.dumps(trusted))
        (root / f"ready-{member}.json").write_text(json.dumps(readiness))
        members.append(
            {
                "id": member,
                "trust_policy": f"trust-{member}.json",
                "readiness_policy": f"ready-{member}.json",
                "model_root": "models",
            }
        )
        selections.append(
            {
                "id": member,
                "run": 700 + index,
                "attempt": 1,
                "target": {"context": member, "model_revision": MODEL_REVISION, "revision": REVISION},
            }
        )
    definition = root / "definition.json"
    definition.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "name": "native-release",
                "version": "1",
                "members": members,
                "constraints": [{"kind": kind, "members": list(PLATFORMS)} for kind in ("same_revision", "same_model")],
            }
        )
    )
    selection = root / "selection.json"
    selection.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "definition_digest": composition_io.load_definition(definition, DATE).prepared.digest,
                "members": selections,
            }
        )
    )
    source.calls.clear()
    return definition, selection, source


def console(mode: str, argv: list[str], *, require_installed: bool = True) -> NoReturn:
    from exacly import github_evidence
    from exacly.cli import exacly_main

    installation(require_installed=require_installed)
    source = Source(mode)
    attempted: list[bool] = []

    def blocked_socket(*args: Any, **kwargs: Any) -> NoReturn:
        attempted.append(True)
        raise RuntimeError("composition fixture forbids network sockets")

    if "COMPOSITION_SMOKE_UMASK" in os.environ:
        os.umask(int(os.environ["COMPOSITION_SMOKE_UMASK"], 8))
    with patch.object(socket, "socket", blocked_socket):
        with suppress(RuntimeError):
            socket.socket()
        assert attempted == [True], "socket guard positive control failed"
        attempted.clear()
        with patch.object(github_evidence, "GitHubClient", return_value=source):
            status = exacly_main(argv)
        assert not attempted, "unexpected network attempt during composition fixture"
    trace = os.environ.get("COMPOSITION_SMOKE_TRACE")
    if trace:
        descriptor = os.open(trace, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {
                    "source_reads": len(source.calls),
                    "downloads": len(source.downloads),
                    "socket_calls": len(attempted),
                    "socket_guard_control": True,
                },
                stream,
            )
    raise SystemExit(status)


def run(*, require_installed: bool = True) -> Doc:
    package = installation(require_installed=require_installed)
    helper = Path(__file__).resolve()
    results: Doc = {}
    with tempfile.TemporaryDirectory(prefix="exacly-composition-smoke-") as directory:
        root = Path(directory)
        definition, selection, _ = scenario(root / "inputs")
        original = {path: path.read_bytes() for path in definition.parent.rglob("*") if path.is_file()}

        def invoke(
            name: str, argv: list[str], *, mode: str = "success", umask: str = "022"
        ) -> tuple[subprocess.CompletedProcess[str], Doc]:
            trace = root / f"{name}-trace.json"
            done = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    CONSOLE_WRAPPER,
                    str(helper),
                    "installed" if require_installed else "unit",
                    mode,
                    *argv,
                ],
                cwd=root,
                env={
                    **os.environ,
                    "GH_TOKEN": TOKEN,
                    "GITHUB_TOKEN": "",
                    "COMPOSITION_SMOKE_TRACE": str(trace),
                    "COMPOSITION_SMOKE_UMASK": umask,
                },
                capture_output=True,
                text=True,
            )
            assert trace.is_file(), done.stderr
            return done, json.loads(trace.read_text())

        validated, trace = invoke(
            "validate",
            ["composition", "validate", "--definition", str(definition), "--date", DATE.isoformat(), "--json"],
        )
        assert validated.returncode == 0, validated.stderr
        assert (
            json.loads(validated.stdout)["definition_digest"] == json.loads(selection.read_text())["definition_digest"]
        )
        assert trace["source_reads"] == trace["downloads"] == trace["socket_calls"] == 0
        results["offline_definition_validation"] = True
        base_args = [
            "compose",
            "github-actions",
            "--definition",
            str(definition),
            "--selection",
            str(selection),
            "--date",
            DATE.isoformat(),
            "--json",
        ]
        for mask in ("022", "000", "777"):
            out = root / f"pass-{mask}"
            done, trace = invoke(f"pass-{mask}", [*base_args, "--out", str(out)], umask=mask)
            assert done.returncode == 0, done.stderr
            report = json.loads(done.stdout)
            assert report == json.loads((out / "report.json").read_text())
            assert report["complete"] is True and report["result"] == "satisfied"
            assert report["required_members"] == list(PLATFORMS)
            assert trace["downloads"] == 4 and trace["source_reads"] > 0 and trace["socket_guard_control"] is True
            for index, member in enumerate(report["members"]):
                assert member["admission"]["status"] == "admitted" and member["gate"]["result"] == "satisfied"
                state = json.loads((out / member["evidence"]["state"]).read_text())
                assert state["namespace"] == "composition-smoke" and state["context"] == PLATFORMS[index]
                assert state["config_revision"] == MODEL_REVISION
                assert member["selection"]["target"]["revision"] == REVISION
            for path in (out, *out.rglob("*")):
                assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path
        results["four_native_admissions_and_gates"] = True
        results["fresh_admission_on_rerun"] = True
        results["private_permissions_under_022_000_777"] = True
        results["namespace_and_target_preserved"] = True
        failed, trace = invoke("failed", [*base_args, "--out", str(root / "failed")], mode="failure")
        assert failed.returncode == 1, failed.stderr
        blocked = json.loads(failed.stdout)
        assert blocked["complete"] is True and blocked["result"] == "blocked" and trace["downloads"] == 4
        assert sum(member["status"] == "satisfied" for member in blocked["members"]) == 3
        assert blocked["members"][-1]["admission"]["status"] == "admitted"
        assert blocked["members"][-1]["gate"]["result"] == "not_satisfied"
        results["admitted_failure_blocks_aggregate"] = True
        missing = root / "missing.json"
        document = json.loads(selection.read_text())
        document["members"].pop()
        missing.write_text(json.dumps(document))
        bad_args = [
            "compose",
            "github-actions",
            "--definition",
            str(definition),
            "--selection",
            str(missing),
            "--date",
            DATE.isoformat(),
            "--out",
            str(root / "missing"),
            "--json",
        ]
        rejected, trace = invoke("missing", bad_args)
        assert rejected.returncode == 2 and trace["source_reads"] == trace["downloads"] == 0
        assert not (root / "missing").exists()
        results["omission_rejected_before_source_access"] = True
        existing, trace = invoke("existing", [*base_args, "--out", str(root / "pass-022")])
        assert existing.returncode == 2 and trace["source_reads"] == 0
        results["existing_output_preserved"] = True
        assert all(path.read_bytes() == content for path, content in original.items())
        results["inputs_preserved"] = True
    return {
        "package": package,
        **results,
        "github_transport": "fixture_with_sockets_blocked",
        "scope": "composition_fixture_only",
    }


def main() -> int:
    print(json.dumps(run(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
