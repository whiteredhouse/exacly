"""Exercise the installed trust-policy drafting CLI outside the source checkout.

GitHub metadata and repository contents are local fixtures. This checks installed command and policy
format compatibility; it grants no approval and establishes no live provenance, test honesty or coverage.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

Doc = dict[str, Any]
REPOSITORY = "acme/policy-smoke"
REVISION = "a" * 40
TREE = "b" * 40
WORKFLOW = ".github/workflows/verification-evidence.yml"
MODEL = "spec/exacly/model.json"
TOKEN = "policy-smoke-fixture-token"
PRODUCER: Doc = {
    "branch": "main",
    "workflow": {
        "path": WORKFLOW,
        "job_id": "observe",
        "job_name": "Observe verification",
        "steps": {
            "verification": "Run verification",
            "package": "Package verification evidence",
            "upload": "Upload verification evidence",
        },
    },
    "artifact": {"name_prefix": "exacly-evidence"},
}
PROPOSAL: Doc = {"producer": PRODUCER, "observer_paths": [WORKFLOW, "tests", "ci"]}
CONSOLE_WRAPPER = (
    "import runpy, sys; "
    "helper = runpy.run_path(sys.argv[1]); "
    "helper['console'](sys.argv[3], sys.argv[4:], require_installed=sys.argv[2] == 'installed')"
)


def installation(*, require_installed: bool = True) -> Doc:
    import exacly

    module = Path(exacly.__file__).resolve()
    distribution = importlib.metadata.distribution("exacly")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    # uv may load dependencies from an overlay or symlinked cache outside sys.prefix.
    # The installed distribution's RECORD must own the imported package file.
    if require_installed and (
        direct.get("dir_info", {}).get("editable") is True
        or not any(
            entry.parts == ("exacly", "__init__.py") and Path(str(distribution.locate_file(entry))).resolve() == module
            for entry in distribution.files or ()
        )
    ):
        raise RuntimeError(f"policy smoke requires an installed package, found {module}")
    return {"version": distribution.version, "module": str(module)}


def model() -> Doc:
    """A standalone conforming 0.2 config, independent of checkout fixtures and schemas."""
    common = {"owner": "team", "lifecycle": "active", "evidence": [], "last_reviewed_at": "2026-10-29"}

    def record(rid: str, kind: str, **fields: Any) -> Doc:
        return {"id": rid, "type": kind, "name": rid, "description": "Smoke fixture.", **common, **fields}

    return {
        "exacly": "0.2.0",
        "model": {
            "namespace": "smoke",
            "product": "Policy smoke",
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
            record(
                "REQ-1",
                "Requirement",
                statement="Draft the policy.",
                acceptance_criteria=["Policy content matches the selected repository."],
                verification_method="Tests",
                observe={
                    "tests": {"workflow": WORKFLOW, "job": "observe", "cases": ["tests/test_policy.py::test_draft"]}
                },
            ),
        ],
        "relationships": [
            {"source": "MEAS-1", "type": "evaluates", "target": "OUT-1"},
            {"source": "CAP-1", "type": "contributes_to", "target": "OUT-1"},
            {"source": "PROC-1", "type": "uses_capability", "target": "CAP-1"},
            {"source": "PROC-1", "type": "uses", "target": "APP-1"},
            {"source": "REQ-1", "type": "supports", "target": "PROC-1"},
            {"source": "APP-1", "type": "implements", "target": "REQ-1"},
        ],
    }


class Source:
    """Strict GitHub read fixture with content-addressed Git blobs and no artifact access."""

    def __init__(self) -> None:
        workflow = {
            "on": {"push": {"branches": ["main"]}},
            "jobs": {
                "observe": {
                    "name": PRODUCER["workflow"]["job_name"],
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"uses": "actions/checkout@" + "1" * 40, "with": {"ref": "${{ github.sha }}"}},
                        {"name": "Run verification", "run": "pytest tests --junitxml=out/junit.xml"},
                        {
                            "name": "Package verification evidence",
                            "run": "exacly evidence package --report out/junit.xml --out out/evidence",
                        },
                        {
                            "name": "Upload verification evidence",
                            "uses": "actions/upload-artifact@" + "2" * 40,
                            "with": {"name": "exacly-evidence-${{ github.run_id }}-${{ github.run_attempt }}"},
                        },
                    ],
                }
            },
        }
        files = {
            WORKFLOW: json.dumps(workflow).encode(),
            MODEL: json.dumps(model()).encode(),
            "tests/test_policy.py": b"def test_draft(): pass\n",
            "ci/verify.py": b"# fixture observer\n",
            "src/service.py": b"# fixture implementation\n",
        }
        base = f"/repos/{REPOSITORY}"
        self.pages: Doc = {
            base: {"id": 123, "full_name": REPOSITORY, "default_branch": "main"},
            f"{base}/git/commits/{REVISION}": {"sha": REVISION, "tree": {"sha": TREE}},
        }
        entries = []
        for path, content in files.items():
            identity = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": identity, "size": len(content)})
            self.pages[f"{base}/git/blobs/{identity}"] = {
                "sha": identity,
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
            }
        self.pages[f"{base}/git/trees/{TREE}?recursive=1"] = {"sha": TREE, "truncated": False, "tree": entries}

    def get_json(self, path: str) -> Doc:
        if path not in self.pages:
            raise RuntimeError(f"unexpected fixture GitHub request: {path}")
        return dict(copy.deepcopy(self.pages[path]))

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise RuntimeError("policy drafting must not download evidence artifacts")


def console(executable: str, args: list[str], *, require_installed: bool = True) -> NoReturn:
    """Run the actual installed console entrypoint, replacing only its GitHub transport."""
    installation(require_installed=require_installed)
    from exacly import github_evidence

    def client(token: str) -> Source:
        if token != TOKEN:
            raise RuntimeError("policy smoke did not use its fixture credentials")
        return Source()

    def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("network access is forbidden in the policy smoke fixture")

    sys.argv = [executable, *args]
    with patch.object(github_evidence, "GitHubClient", client), patch.object(socket, "socket", no_network):
        runpy.run_path(executable, run_name="__main__")
    raise RuntimeError("console entrypoint returned without an exit status")


def run(*, require_installed: bool = True) -> Doc:
    installed = installation(require_installed=require_installed)
    from exacly import admission, query

    checked = query.load({MODEL: model()}, dt.date(2026, 10, 29), revision=REVISION)
    if checked.report.errors:
        raise RuntimeError("policy smoke fixture does not conform to the installed model schema")
    executable = Path(sys.executable).parent / ("exacly.exe" if os.name == "nt" else "exacly")
    if not executable.is_file():
        raise RuntimeError(f"installed console command is missing: {executable}")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN", "PYTHONPATH", "PYTHONHOME"}
    }
    environment["GH_TOKEN"] = TOKEN
    with tempfile.TemporaryDirectory(prefix="exacly-policy-wheel-") as scratch:
        consumer = Path(scratch)
        proposal = consumer / "producer proposal.json"
        proposal.write_text(json.dumps(PROPOSAL))
        original = proposal.read_bytes()

        def invoke(args: list[str], expected: int = 0) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    CONSOLE_WRAPPER,
                    str(Path(__file__).resolve()),
                    "installed" if require_installed else "source-unit-mode",
                    str(executable),
                    *args,
                ],
                cwd=consumer,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != expected:
                raise RuntimeError(
                    f"installed policy CLI exited {result.returncode}, expected {expected}: "
                    f"{result.stdout} {result.stderr}"
                )
            if expected and (result.stdout or not result.stderr):
                raise RuntimeError("invalid policy inputs did not produce a clean error diagnostic")
            return result

        prefix = ["evidence", "propose-policy", "github-actions"]
        help_text = invoke([*prefix, "--help"]).stdout
        if not all(flag in help_text for flag in ("--proposal", "--revision", "--branch-protection", "--out")):
            raise RuntimeError("installed command help is missing policy drafting options")

        def arguments(out: Path) -> list[str]:
            return [
                *prefix,
                "--repository",
                REPOSITORY,
                "--revision",
                REVISION,
                "--proposal",
                str(proposal),
                "--model-file",
                MODEL,
                "--name",
                "consumer-verification",
                "--version",
                "7",
                "--out",
                str(out),
            ]

        expected = admission.propose_policy(
            Source(),
            REPOSITORY,
            REVISION,
            [MODEL],
            producer=PRODUCER,
            observer_paths=PROPOSAL["observer_paths"],
            name="consumer-verification",
            version="7",
        )
        outputs = []
        for protection in (None, "required", "not_required"):
            out = consumer / f"policy {protection}.json"
            args = arguments(out)
            if protection is not None:
                args += ["--branch-protection", protection]
            result = invoke([*args, "--json"])
            if result.stderr or result.stdout != out.read_text():
                raise RuntimeError("policy JSON output differs from the retained file")
            candidate = json.loads(out.read_text())
            desired = expected if protection is None else {**expected, "branch_protection": protection}
            if candidate != desired or admission.validate_policy(candidate) != desired:
                raise RuntimeError("installed CLI changed the existing policy proposal contract")
            outputs.append(out)
        human = consumer / "human policy.json"
        result = invoke(arguments(human))
        if not result.stdout.strip() or result.stderr or json.loads(human.read_text()) != expected:
            raise RuntimeError("human CLI output did not retain the same draft policy")
        retained = outputs[0].read_bytes()
        invoke(arguments(outputs[0]), 2)
        if outputs[0].read_bytes() != retained:
            raise RuntimeError("policy CLI overwrote an existing policy")
        invalid = consumer / "invalid policy.json"
        bad_revision = arguments(invalid)
        bad_revision[bad_revision.index("--revision") + 1] = "main"
        invoke(bad_revision, 2)
        if invalid.exists():
            raise RuntimeError("invalid revision created a policy")
        if proposal.read_bytes() != original:
            raise RuntimeError("policy CLI changed its proposal input")
        proposal.write_text(json.dumps({**PROPOSAL, "unexpected": True}))
        invoke(arguments(invalid), 2)
        if invalid.exists():
            raise RuntimeError("invalid proposal created a policy")
        return {
            "package": installed,
            "api_equivalent": True,
            "explicit_branch_protection_retained": True,
            "default_branch_protection_omitted": True,
            "exact_json_output": True,
            "existing_output_preserved": True,
            "invalid_inputs_rejected": True,
            "github_transport": "fixture_with_sockets_blocked",
            "scope": "policy_drafting_fixture_only",
        }


def main() -> int:
    print(json.dumps(run(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
