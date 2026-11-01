"""Keep the workflow's upload authority aligned with the pilot's native release gates."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ci import req039_history as history

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
WORKFLOW: dict[Any, Any] = yaml.safe_load((ROOT / history.COORDINATOR_WORKFLOW).read_text())
JOB: dict[str, Any] = WORKFLOW["jobs"]["coordinate"]
STEPS: list[dict[str, Any]] = JOB["steps"]


def step(name: str) -> tuple[int, dict[str, Any]]:
    matches = [(index, item) for index, item in enumerate(STEPS) if item.get("name") == name]
    assert len(matches) == 1, f"the server-side step proof requires one exact step named {name!r}"
    return matches[0]


def test_one_manual_coordinator_serializes_the_original_goal_with_read_only_credentials() -> None:
    # PyYAML's YAML 1.1 loader treats the GitHub Actions key "on" as a boolean.
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}
    assert set(WORKFLOW["jobs"]) == {"coordinate"}
    assert JOB["name"] == "coordinate"
    assert "permissions" not in JOB and "concurrency" not in JOB
    assert WORKFLOW["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert WORKFLOW["concurrency"] == {
        "group": "exacly-req039-original-goal",
        "cancel-in-progress": False,
        "queue": "max",
    }


def test_runner_root_is_initialized_in_a_supported_step_context(tmp_path: Path) -> None:
    # GitHub rejects runner context in job.env before creating any jobs.
    assert "env" not in JOB
    initialize_index, initialize = step("Initialize same-runner pilot root")
    launch_index, _ = step(history.LAUNCH_STEP)
    assert initialize_index == 0 < launch_index
    assert initialize["env"] == {"PILOT_ROOT": "${{ runner.temp }}/exacly-req039"}
    pilot_root = str(tmp_path / "runner temp $literal `literal`" / "exacly-req039")
    environment = tmp_path / "github-env"
    result = subprocess.run(
        ["/bin/bash", "-c", initialize["run"]],
        env={"PATH": os.defpath, "PILOT_ROOT": pilot_root, "GITHUB_ENV": str(environment)},
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert environment.read_text() == "PILOT_ROOT=" + pilot_root + "\n"


def test_workflow_retains_the_exact_approval_fence_before_referencing_native_authority() -> None:
    launch_index, _ = step(history.LAUNCH_STEP)
    wait_index, wait = step("Await independent approval")
    fence_index, fence = step(history.FENCE_STEP)
    reference_index, reference = step("Reference independently checked approval fence")
    native_index, native = step("Await native completion")
    assert launch_index < wait_index < fence_index < reference_index < native_index
    assert "ci.req039_pilot wait-approval" in wait["run"]
    assert fence["id"] == "goal_fence" and "if" not in fence
    assert fence["with"]["name"] == history.FENCE_PREFIX + "${{ github.run_id }}-${{ github.run_attempt }}"
    assert fence["with"]["path"] == "${{ env.PILOT_ROOT }}/public/" + history.FENCE_MEMBER
    assert "if" not in reference
    assert reference["env"] == {
        "PILOT_FENCE_ID": "${{ steps.goal_fence.outputs.artifact-id }}",
        # The pinned action emits bare hex; the CLI normalizes it, then fetches the server digest.
        "PILOT_FENCE_DIGEST": "${{ steps.goal_fence.outputs.artifact-digest }}",
    }
    assert re.search(r"\bci\.req039_pilot\s+advance\b", reference["run"])
    assert '--artifact-id "$PILOT_FENCE_ID"' in reference["run"]
    assert '--artifact-digest "$PILOT_FENCE_DIGEST"' in reference["run"]
    assert "ci.req039_pilot wait --root" in native["run"]


def test_noauthority_upload_requires_cleanup_to_stop_the_unit_and_export_a_proof() -> None:
    cleanup_index, cleanup = step("Stop bounded same-runner coordinator")
    proof_index, proof = step(history.NOAUTH_STEP)
    assert cleanup_index < proof_index
    assert cleanup["id"] == "cleanup" and cleanup["if"] == "always()"
    assert cleanup["run"].index("ci.req039_pilot stop") < cleanup["run"].index("noauthority=true")
    assert "$PILOT_ROOT/public/" + history.NOAUTH_MEMBER in cleanup["run"]
    assert proof["if"] == "always() && steps.cleanup.outputs.noauthority == 'true'"
    assert proof["with"]["name"] == history.NOAUTH_PREFIX + "${{ github.run_id }}-${{ github.run_attempt }}"
    assert proof["with"]["path"] == "${{ env.PILOT_ROOT }}/public/" + history.NOAUTH_MEMBER


@pytest.mark.parametrize("stop_exit", [0, 17])
def test_cleanup_cannot_upload_an_existing_proof_after_stop_fails(tmp_path: Path, stop_exit: int) -> None:
    _, cleanup = step("Stop bounded same-runner coordinator")
    pilot_root = tmp_path / "pilot"
    public = pilot_root / "public"
    public.mkdir(parents=True)
    (pilot_root / "launch.json").write_text("{}")
    (public / history.NOAUTH_MEMBER).write_text("{}")
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_uv = tools / "uv"
    fake_uv.write_text(f"#!/bin/sh\nexit {stop_exit}\n")
    fake_uv.chmod(0o700)
    output = tmp_path / "outputs"
    output.touch()
    # Exercise the actual workflow shell's failure propagation, without starting a native unit.
    result = subprocess.run(
        ["/bin/bash", "-c", cleanup["run"]],
        env={"PATH": str(tools) + os.pathsep + os.defpath, "PILOT_ROOT": str(pilot_root), "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == stop_exit
    assert ("noauthority=true" in output.read_text()) is (stop_exit == 0)


def test_workflow_uploads_only_explicit_public_documents_with_immutable_artifacts() -> None:
    allowed = {
        "packet.json",
        "approval-comment.json",
        history.FENCE_MEMBER,
        history.NOAUTH_MEMBER,
        "acceptance-evidence.json",
    }
    uploaded: set[str] = set()
    for item in STEPS:
        if "upload-artifact@" not in item.get("uses", ""):
            continue
        assert item["uses"] == UPLOAD
        options = item["with"]
        assert options["overwrite"] is False
        assert options["if-no-files-found"] == "error"
        assert options["include-hidden-files"] is False
        for path in options["path"].splitlines():
            prefix = "${{ env.PILOT_ROOT }}/public/"
            assert path.startswith(prefix)
            member = path.removeprefix(prefix)
            assert member in allowed
            uploaded.add(member)
    assert uploaded == allowed


def test_workflow_does_not_expose_artifact_upload_credentials_or_interpolate_inputs_in_shell() -> None:
    text = (ROOT / history.COORDINATOR_WORKFLOW).read_text()
    assert "ACTIONS_RUNTIME_TOKEN" not in text and "ACTIONS_RUNTIME_URL" not in text
    _, launch = step(history.LAUNCH_STEP)
    assert launch["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert launch["env"]["EXACLY_REQ039_PUBLISH_TOKEN"] == "${{ secrets.EXACLY_REQ039_PUBLISH_TOKEN }}"
    assert launch["env"]["PILOT_BASE"] == "${{ inputs.base }}"
    assert launch["env"]["PILOT_BASELINE_RUN"] == "${{ inputs.baseline_run }}"
    for item in STEPS:
        run = item.get("run", "")
        assert "${{ inputs." not in run and "${{ secrets." not in run
        if item is not launch:
            assert not any("secrets." in str(value) for value in item.get("env", {}).values())
