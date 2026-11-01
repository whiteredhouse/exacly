"""Controlled approval/API fixtures and real local bootstrap storage; no live effects or CI claim."""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import io
import json
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from exacly import (
    automation_authorization,
    automation_controller,
    git_remote_transport,
    handoff_git,
    live_pilot_evidence,
    planning,
    remote_publication,
    resource_guard,
    standing_authorization,
    state_writer,
    worker_integration,
)

from ci import req039_bootstrap as setup
from ci import req039_history as history
from ci import req039_pilot as pilot

Doc = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
CREATED = "2026-11-01T10:00:00+00:00"


def context() -> Doc:
    return {
        "run_id": "123",
        "run_attempt": "1",
        "job_key": "coordinate",
        "nonce": "a" * 32,
        "coordinator_revision": "b" * 40,
        "workflow_id": 99,
        "run_number": 7,
        "workflow_path": history.COORDINATOR_WORKFLOW,
        "created_at": CREATED,
        "deadline_at": "2026-11-01T10:29:00+00:00",
        "host": {"run_id": "123", "job_id": "456", "boot_id": "673072d4-1f12-43f6-a126-e947a82513ce"},
    }


def guard() -> resource_guard.Guard:
    bounds = resource_guard.PILOT_BOUNDS
    from dataclasses import asdict

    identity = {
        "schema_version": "0.1.0",
        "backend": resource_guard.BACKEND,
        "boot_id": context()["host"]["boot_id"],
        "cgroup": "/system.slice/exacly-test.service",
        "mount_id": "10",
        "mount_device": "0:20",
        "device": 20,
        "inode": 30,
        "caps": asdict(bounds),
        "bounds": asdict(bounds),
    }
    return resource_guard.Guard(bounds, json.dumps(identity).encode())


def minimal_bundle() -> Doc:
    return {
        "context": context(),
        "base": "b" * 40,
        "trust": {"branch_protection": "not_required"},
        "delegation": {"scope": "fixed"},
        "contract": {"scope": "local-machine"},
        "components": {"stores": "pinned"},
        "resource_guard": guard().descriptor,
        "worker_identity": {"inputs": "digests only"},
        "baseline_selection": {"run_id": 777, "attempt": 1},
    }


def packet() -> Doc:
    return pilot.packet_for(minimal_bundle(), publisher_available=False)


def comment(selected: Doc | None = None) -> Doc:
    chosen = packet() if selected is None else selected
    value = {
        "id": 1234,
        "user": {"id": pilot.ISSUER_ID, "type": "User", "login": "p1c2u"},
        "created_at": "2026-11-01T10:01:00Z",
        "updated_at": "2026-11-01T10:01:00Z",
        "issue_url": f"https://api.github.com/repos/{setup.REPOSITORY}/issues/{pilot.APPROVAL_ISSUE}",
        "html_url": f"https://github.com/{setup.REPOSITORY}/pull/{pilot.APPROVAL_ISSUE}#issuecomment-1234",
        "performed_via_github_app": None,
        "body": pilot.approval_body(chosen),
    }
    value["original_approval"] = {
        "id": "immutable-node",
        "fullDatabaseId": str(value["id"]),
        "body": value["body"],
        "createdAt": value["created_at"],
        "updatedAt": value["updated_at"],
        "lastEditedAt": None,
        "editor": None,
        "author": {"__typename": "User", "databaseId": pilot.ISSUER_ID},
        "issue": {
            "number": pilot.APPROVAL_ISSUE,
            "repository": {"databaseId": setup.REPOSITORY_ID, "nameWithOwner": setup.REPOSITORY},
        },
    }
    return value


class Comments:
    def __init__(self, values: list[Doc]) -> None:
        self.values = values
        self.reads: list[int] = []

    def comments(self, page: int) -> list[Doc]:
        assert page == 1
        return copy.deepcopy(self.values)

    def comment(self, identity: int) -> Doc:
        self.reads.append(identity)
        return copy.deepcopy(next(item for item in self.values if item["id"] == identity))


def test_human_approval_requires_fresh_server_identity_and_exact_packet() -> None:
    chosen = packet()
    reader = Comments([comment(chosen)])
    receipt = pilot.find_approval(reader, chosen)
    assert receipt is not None and receipt["user_id"] == pilot.ISSUER_ID
    assert reader.reads == [1234]
    pilot.verify_approval(reader, chosen, receipt)
    assert reader.reads == [1234, 1234]
    reader.values[0]["updated_at"] = "2026-11-01T10:02:00Z"
    with pytest.raises(ValueError, match="edited or stale"):
        pilot.verify_approval(reader, chosen, receipt)


@pytest.mark.parametrize(
    "change",
    [
        "bot",
        "other-human",
        "app",
        "edited",
        "other-issue",
        "noncanonical",
        "duplicate-field",
        "missing-app",
        "same-second-edit",
    ],
)
def test_candidate_or_app_cannot_issue_or_rewrite_human_approval(change: str) -> None:
    value = comment()
    if change == "bot":
        value["user"]["type"] = "Bot"
    elif change == "other-human":
        value["user"]["id"] += 1
    elif change == "app":
        value["performed_via_github_app"] = {"id": 999}
    elif change == "edited":
        value["updated_at"] = "2026-11-01T10:02:00Z"
    elif change == "missing-app":
        value.pop("performed_via_github_app")
    elif change == "same-second-edit":
        value["original_approval"]["lastEditedAt"] = "2026-11-01T10:01:00.500Z"
        value["original_approval"]["editor"] = {"__typename": "Bot"}
    elif change == "other-issue":
        value["issue_url"] = value["issue_url"].replace("/93", "/94")
    elif change == "noncanonical":
        value["body"] += "\n"
    else:
        value["body"] = value["body"].replace('"decision":"approve"', '"decision":"approve","decision":"approve"')
    assert pilot.parsed_approval(value) is None
    if change in {"app", "edited", "missing-app", "same-second-edit"}:
        with pytest.raises(ValueError, match="edited human approval history"):
            pilot.find_approval(Comments([value]), packet())
    else:
        assert pilot.find_approval(Comments([value]), packet()) is None


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "run_attempt",
        "job_id",
        "boot_id",
        "nonce",
        "producer_policy_digest",
        "delegation_digest",
        "workflow_contract_digest",
        "resource_guard_digest",
        "packet_digest",
    ],
)
def test_prior_approval_for_same_logical_goal_tombstones_new_run_or_store(field: str) -> None:
    chosen = packet()
    value = comment(chosen)
    body = pilot.approval_value(chosen)
    if field.endswith("digest"):
        body[field] = "sha256:" + "c" * 64
    elif field == "boot_id":
        body[field] = str(uuid.uuid4())
    elif field == "nonce":
        body[field] = "c" * 32
    else:
        body[field] = "999"
    value["body"] = pilot.HEADER + "\n" + planning.canonical(body)
    value["original_approval"]["body"] = value["body"]
    with pytest.raises(ValueError, match="tombstones"):
        pilot.find_approval(Comments([value]), chosen)


def test_discovery_fields_are_not_authority_and_approval_cannot_be_local_json() -> None:
    class ForgedDiscovery(Comments):
        def comments(self, page: int) -> list[Doc]:
            return [comment()]

    fresh = comment()
    fresh["user"]["id"] = 999
    reader = ForgedDiscovery([fresh])
    assert pilot.find_approval(reader, packet()) is None
    assert reader.reads == [1234]


def test_sanitized_packet_excludes_ambient_secrets_and_unselected_bundle_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "reader-secret-never-serialize")
    monkeypatch.setenv(pilot.PUBLISH_TOKEN, "publisher-secret-never-serialize")
    bundle = {**minimal_bundle(), "unselected_secret": "private-key-bytes-never-serialize"}
    value = pilot.packet_for(bundle, publisher_available=True)
    rendered = planning.canonical(value)
    assert "secret-never-serialize" not in rendered and "private-key-bytes" not in rendered
    assert value["credential_requirements"]["scope_verified"] is False
    assert value["digest"] == planning.digest({key: item for key, item in value.items() if key != "digest"})


def test_closed_background_service_profile_forwards_only_literal_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup.private_directory(tmp_path / "private", create=True)
    for name in pilot.FORWARDED:
        monkeypatch.setenv(name, "fake-context-value")
    monkeypatch.setenv(pilot.PUBLISH_TOKEN, "never-in-argv-secret")
    command = pilot.unit_command(root, tmp_path, "b" * 40, context(), "exacly-req039-" + "d" * 32, None)
    assert "--wait" not in command and "--pipe" not in command
    assert "--property=RuntimeMaxSec=1800" in command
    assert "--property=MemoryMax=1073741824" in command and "--property=MemorySwapMax=0" in command
    assert "--property=TasksMax=128" in command and "--property=CPUQuota=100%" in command
    assert "--property=KillMode=control-group" in command
    assert "--setenv=" + pilot.PUBLISH_TOKEN in command
    assert "never-in-argv-secret" not in planning.canonical(command) and "fake-context-value" not in planning.canonical(
        command
    )


def test_private_documents_are_explicit_and_do_not_replace_or_follow_aliases(tmp_path: Path) -> None:
    root = setup.private_directory(tmp_path / "private", create=True)
    path = root / "configuration.json"
    setup.write_json(path, {"pinned": True})
    assert setup.read_json(path) == {"pinned": True}
    with pytest.raises(FileExistsError):
        setup.write_json(path, {"pinned": False})
    alias = root / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        setup.read_json(alias)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="protected bounded"):
        setup.read_json(path)


def object_file(repository: Path, kind: str, content: bytes) -> str:
    encoded = kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
    identity = hashlib.sha1(encoded).hexdigest()
    target = repository / "objects" / identity[:2] / identity[2:]
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target.write_bytes(zlib.compress(encoded))
    target.chmod(0o600)
    return identity


def checkout(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str, str, list[Doc]]:
    root = setup.private_directory(tmp_path / "checkout", create=True)
    repository = root / ".git"
    repository.mkdir(mode=0o700)
    (repository / "objects").mkdir(mode=0o700)
    (repository / "objects/info").mkdir(mode=0o700)
    (repository / "objects/pack").mkdir(mode=0o700)
    structure: dict[str, Any] = {}
    entries: list[Doc] = []
    for name, content in files.items():
        cursor = structure
        parts = name.split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        identity = object_file(repository, "blob", content)
        cursor[parts[-1]] = identity
        entries.append({"path": name, "mode": "100644", "type": "blob", "sha": identity, "size": len(content)})

    def tree(items: dict[str, Any]) -> str:
        encoded: list[tuple[str, bytes]] = []
        for name, value in items.items():
            mode, identity = ("40000", tree(value)) if isinstance(value, dict) else ("100644", value)
            encoded.append(
                (
                    name + ("/" if mode == "40000" else ""),
                    mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(identity),
                )
            )
        return object_file(repository, "tree", b"".join(data for _, data in sorted(encoded)))

    tree_id = tree(structure)
    revision = object_file(
        repository,
        "commit",
        (
            f"tree {tree_id}\nauthor A <a@example.invalid> 1 +0000\ncommitter A <a@example.invalid> 1 +0000\n\nbase\n"
        ).encode(),
    )
    return root, revision, tree_id, entries


def test_exact_regular_object_import_ignores_checkout_configuration_and_verifies_closure(tmp_path: Path) -> None:
    root, revision, _, _ = checkout(tmp_path, {setup.SOURCE: b"approved source\n"})
    (root / ".git" / "config").write_text("[core]\n hooksPath = /evil\n[filter evil]\n smudge = /evil\n")
    target = setup.private_directory(tmp_path / "operator", create=True) / "source.git"
    handoff_git.initialize_repository(target)
    setup.import_base(root, target, revision)
    assert (target / "config").read_bytes() == handoff_git.CONFIG
    assert handoff_git.read_ref(target, "refs/heads/approved-base") == revision
    prepared = worker_integration.prepare(
        target,
        repository_id=handoff_git.inspect_repository(target)["instance_id"],
        revision=revision,
        allowed_paths=[setup.SOURCE],
        protected_paths=setup.PROTECTED_PATHS,
        model_digest="sha256:" + "a" * 64,
    )
    assert prepared.inputs["source/" + setup.SOURCE] == b"approved source\n"


@pytest.mark.parametrize("damage", ["alternates", "symlink", "missing-parent"])
def test_unsafe_or_incomplete_checkout_cannot_seed_authoritative_source(tmp_path: Path, damage: str) -> None:
    root, revision, tree, _ = checkout(tmp_path, {setup.SOURCE: b"approved\n"})
    if damage == "alternates":
        (root / ".git/objects/info/alternates").write_text("/unapproved/object-store\n")
    elif damage == "symlink":
        (root / ".git/objects/aa").mkdir()
        (root / ".git/objects/aa" / ("b" * 38)).symlink_to("/etc/passwd")
    else:
        revision = object_file(
            root / ".git",
            "commit",
            (
                f"tree {tree}\nparent {'f' * 40}\n"
                "author A <a@example.invalid> 1 +0000\n"
                "committer A <a@example.invalid> 1 +0000\n\nshallow\n"
            ).encode(),
        )
    target = setup.private_directory(tmp_path / "operator", create=True) / "source.git"
    handoff_git.initialize_repository(target)
    with pytest.raises(ValueError):
        setup.import_base(root, target, revision)
    assert handoff_git.read_ref(target, "refs/heads/approved-base") is None


class BootstrapSource:
    def __init__(self, files: dict[str, bytes], revision: str, tree: str, entries: list[Doc]) -> None:
        repository = {"id": setup.REPOSITORY_ID, "full_name": setup.REPOSITORY}
        workflow = {"id": 99, "path": setup.WORKFLOW, "state": "active"}
        run = {
            "id": 777,
            "run_attempt": 1,
            "event": "push",
            "head_branch": setup.BRANCH,
            "head_sha": revision,
            "workflow_id": 99,
            "run_number": 7,
            "path": setup.WORKFLOW,
            "repository": repository,
            "head_repository": repository,
            "status": "completed",
            "conclusion": "failure",
        }
        prefix = "/repos/" + setup.REPOSITORY
        self.pages: dict[str, Doc] = {
            prefix: repository,
            prefix + "/git/commits/" + revision: {"sha": revision, "tree": {"sha": tree}},
            prefix + "/git/trees/" + tree + "?recursive=1": {"sha": tree, "truncated": False, "tree": entries},
            prefix + "/actions/workflows/req039-evidence.yml": workflow,
            prefix + "/actions/workflows/99": workflow,
            prefix + "/actions/runs/777": run,
            prefix + "/actions/runs/777/attempts/1": run,
        }
        for entry in entries:
            self.pages[prefix + "/git/blobs/" + entry["sha"]] = {
                "sha": entry["sha"],
                "size": entry["size"],
                "encoding": "base64",
                "content": base64.b64encode(files[entry["path"]]).decode(),
            }

    def get_json(self, path: str) -> Doc:
        assert path in self.pages, path
        return copy.deepcopy(self.pages[path])

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise AssertionError("bootstrap must not manufacture or admit an artifact")


@pytest.fixture
def bootstrap_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> tuple[Path, Doc]:
    files = {name: (ROOT / name).read_bytes() for name in (setup.MODEL, setup.WORKFLOW, setup.SOURCE)}
    checkout_path, revision, tree, entries = checkout(tmp_path, files)
    api = BootstrapSource(files, revision, tree, entries)
    root = setup.private_directory(tmp_path / "pilot", create=True)
    stamp = dt.datetime.now(dt.UTC)
    host = context()
    host.update(
        created_at=stamp.isoformat(),
        deadline_at=(stamp + dt.timedelta(minutes=29)).isoformat(),
        coordinator_revision=revision,
    )

    def runtime(path: Path) -> Doc:
        setup.private_directory(path, create=True)
        return {
            "schema_version": "0.1.0",
            "backend": "linux-bubblewrap-v1",
            "rootfs": str(path),
            "rootfs_digest": "sha256:" + "a" * 64,
            "python": "/usr/bin/python3",
            "bubblewrap": "/usr/bin/bwrap",
        }

    monkeypatch.setattr(setup, "runtime_root", runtime)
    monkeypatch.setattr(
        live_pilot_evidence,
        "implementation_fingerprint",
        lambda *args: {"schema_version": "0.1.0", "revision": revision, "paths": [], "digest": "sha256:" + "e" * 64},
    )
    publisher = None
    if getattr(request, "param", False):
        publisher = {
            "environment": pilot.PUBLISH_TOKEN,
            "present": True,
            "scope_verified": True,
            "app_id": 12345,
            "installation_id": 54321,
            "expires_at": (stamp + dt.timedelta(hours=1)).isoformat(),
            "issuer_evidence": "trusted-workflow-mint-metadata",
            "issuer_server_verified": False,
        }
    bundle = setup.bootstrap(root, checkout_path, revision, host, api, guard(), baseline_run=777, publisher=publisher)
    return root, bundle


def test_bootstrap_creates_only_unchecked_native_state_empty_stores_and_reviewable_candidates(
    bootstrap_case: tuple[Path, Doc],
) -> None:
    root, bundle = bootstrap_case
    state = state_writer.read(Path(bundle["components"]["state"])).document
    assert state is not None and state.get("pending", []) == []
    assert '"passed"' not in planning.canonical(state)
    assert bundle["plan"]["actions"][0]["record"] == setup.RECORD
    assert bundle["trust"]["branch_protection"] == "not_required"
    selected = bundle["components"]["controller"]
    retained = automation_controller.show(Path(selected["journal"]), selected["journal_id"])
    assert retained["event"]["phase"] == "dispatch" and retained["event"]["pending"] is None
    assert bundle["components"]["initial_binding_id"] is None
    assert setup.read_json(root / "bootstrap.json") == bundle
    public = pilot.packet_for(bundle, publisher_available=False)
    assert public["credential_requirements"]["present"] is False
    assert public["original_goal"] == setup.ORIGINAL_GOAL
    assert pilot.parsed_approval(comment(public)) == pilot.approval_value(public)


@pytest.mark.parametrize("identity", [2515732440, 2**63 - 1])
def test_graphql_uses_bigint_comment_identity(identity: int) -> None:
    value = comment()
    value.update(id=identity, node_id="immutable-node")
    original = value.pop("original_approval")
    original["fullDatabaseId"] = str(identity)
    calls = []

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {}

        def getcode(self) -> int:
            return 200

    class Opener:
        def open(self, request: Any, timeout: int) -> Response:
            calls.append(json.loads(request.data))
            return Response(json.dumps({"data": {"node": original}}).encode())

    client: Any = SimpleNamespace(
        get_json=lambda path: copy.deepcopy(value), _opener=Opener(), _token="controlled-reader"
    )
    selected = pilot.ApprovalReader(client).comment(identity)
    assert pilot.parsed_approval(selected) == pilot.approval_value(packet())
    assert "fullDatabaseId" in calls[0]["query"] and "id databaseId body" not in calls[0]["query"]
    original["fullDatabaseId"] = "0" + str(identity)
    with pytest.raises(ValueError, match="conflict"):
        pilot.ApprovalReader(client).comment(identity)


def setup_launch(root: Path, monkeypatch: pytest.MonkeyPatch, selected_context: Doc | None = None) -> Doc:
    setup.private_directory(root / "public", create=True)
    stat = root.stat()
    launch = {
        "schema_version": "0.1.0",
        "unit": "exacly-req039-" + "a" * 32,
        "context": context() if selected_context is None else selected_context,
        "checkout": str(ROOT),
        "storage": {"path": str(root), "device": stat.st_dev, "inode": stat.st_ino},
    }
    setup.write_json(root / "launch.json", launch)
    info = (root / "launch.json").stat()
    setup.write_json(
        root / "preauthority-stage.json",
        {
            "schema_version": "0.1.0",
            "stage": "preauthority",
            "launch_digest": planning.digest(launch),
            "launch_device": info.st_dev,
            "launch_inode": info.st_ino,
        },
    )
    monkeypatch.setattr(pilot, "same_host", lambda metadata: None)
    return launch


def test_actual_upload_action_bare_digest_is_normalized_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = setup.private_directory(tmp_path / "pilot", create=True)
    setup_launch(root, monkeypatch)
    assert pilot.reference_fence(root, 42, "b" * 64) == 0
    assert setup.read_json(root / "fence-reference.json") == {"artifact_id": 42, "digest": "sha256:" + "b" * 64}
    assert pilot.reference_fence(root, 42, "sha256:" + "b" * 64) == 0
    with pytest.raises(ValueError, match="conflicting"):
        pilot.reference_fence(root, 43, "b" * 64)


def test_never_authorized_receipt_requires_stopped_whole_unit_and_irreversible_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup.private_directory(tmp_path / "pilot", create=True)
    setup_launch(root, monkeypatch)
    monkeypatch.setattr(pilot, "unit_state", lambda unit: "unavailable")
    with pytest.raises(ValueError, match="termination"):
        pilot.terminal_noauthority(root)
    assert not (root / "public" / history.NOAUTH_MEMBER).exists()
    monkeypatch.setattr(pilot, "unit_state", lambda unit: "inactive")
    assert pilot.terminal_noauthority(root)
    proof = setup.read_json(root / "public" / history.NOAUTH_MEMBER)
    assert proof["proof_kind"] == "setup-only-never-authorized" and proof["reserved"] == setup.units(0, 0)
    with pytest.raises(ValueError, match="irreversibly stopped"):
        pilot.reference_fence(root, 42, "c" * 64)


@pytest.mark.parametrize("uncertainty", ["authority-construction.json", "approval.json", "unknown-private-store"])
def test_uncertain_private_history_cannot_emit_never_authorized_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertainty: str
) -> None:
    root = setup.private_directory(tmp_path / "pilot", create=True)
    setup_launch(root, monkeypatch)
    setup.write_json(root / uncertainty, {"unknown": True})
    monkeypatch.setattr(pilot, "unit_state", lambda unit: "inactive")
    if uncertainty == "unknown-private-store":
        with pytest.raises(ValueError, match="partial private"):
            pilot.terminal_noauthority(root)
    else:
        assert not pilot.terminal_noauthority(root)
    assert not (root / "public" / history.NOAUTH_MEMBER).exists()


class ControlledEffect:
    """Local durable controlled adapter; never represents native kernel or live consumer proof."""

    def __init__(self, config: Doc, kind: str, path: Path) -> None:
        self.config, self.kind, self.path = config, kind, path
        self.adapter_id = config["effects"][kind]["adapter_id"]
        self.native_configuration_digest = config["native_configuration_digest"]
        self.inspections = 0
        self.invocations = 0
        self.drop_response = kind == "publish"
        self.facts = {
            "schema_version": "0.1.0",
            "objective_id": config["objective"]["id"],
            "original_goal": config["objective"]["original_goal"],
            "action_id": config["actions"][0],
            "contract": config["contract"],
            "repository": config["repository"],
            "base_revision": config["base_revision"],
            "source_paths": [setup.SOURCE],
            "write_paths": [setup.RESULT],
            "destination": None,
            "native": {
                "store": config["effects"][kind]["native_store"],
                "operation_id": planning.digest({"kind": kind}),
            },
            "capabilities": config["effects"][kind]["capabilities"],
            "cost": setup.units(1, 45000),
            "preconditions": {"native_configuration_digest": config["native_configuration_digest"]},
            "authorization": {"operation_id": planning.digest({"kind": kind})},
        }

    def bound_ms(self, method: str, request: Doc) -> int:
        return 45000

    def inspect(self, request: Doc) -> Doc:
        self.inspections += 1
        return copy.deepcopy(self.facts)

    def invoke(self, request: Doc, authorization: Doc) -> Doc:
        self.invocations += 1
        assert authorization == self.facts["authorization"]
        outcome = {"status": "succeeded", "receipt": {"native_operation": self.facts["native"]}}
        setup.write_json(self.path, {"request": request, "authorization": authorization, "outcome": outcome})
        if self.drop_response:
            raise ConnectionResetError("controlled response lost after durable outcome")
        return outcome

    def recover(self, request: Doc, authorization: Doc) -> Doc:
        if not self.path.exists():
            return {
                "status": "not_started",
                "proof": {"native": self.facts["native"], "authorization_digest": planning.digest(authorization)},
            }
        actual = setup.read_json(self.path)
        assert actual["request"] == request and actual["authorization"] == authorization
        return dict(actual["outcome"])


class ControlledReader:
    def __init__(self, config: Doc, purpose: str) -> None:
        self.config, self.purpose = config, purpose
        self.adapter_id = config["reads"][purpose]["adapter_id"]
        self.calls = 0

    def inspect(self, request: Doc) -> Doc:
        scope = self.config["reads"][self.purpose]
        return {
            "schema_version": "0.1.0",
            "repository": self.config["repository"],
            "contract_digest": scope["contract_digest"],
            "native_configuration_digest": self.config["native_configuration_digest"],
            "wall_time_ms": scope["max_wall_time_ms"],
        }

    def read(self, request: Doc, *, wall_time_ms: int) -> Doc:
        self.calls += 1
        return {"status": "observed", "controlled": True}


@pytest.mark.parametrize("bootstrap_case", [True], indirect=True)
def test_bootstrap_budget_covers_six_metered_phases_restart_negatives_and_exact_exhaustion(
    bootstrap_case: tuple[Path, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle = bootstrap_case
    config = bundle["delegation"]
    effects = {
        kind: ControlledEffect(config, kind, root / (kind + "-controlled-intent.json")) for kind in config["effects"]
    }
    readers: dict[str, Any] = {purpose: ControlledReader(config, purpose) for purpose in config["reads"]}

    def authority() -> automation_authorization.Authorizer:
        return automation_authorization.Authorizer(
            Path(config["config_path"]),
            approved_digest=planning.digest(config),
            issuer=config["issuer"],
            approval_id=config["approval_id"],
            effects=effects,
            readers=readers,
        )

    def account() -> Doc:
        return standing_authorization.inspect(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
        )

    # Literal runtime preflight reader with controlled remote callbacks and controlled boundary.
    # All reservation/uncertainty accounting below is the real Authorizer and bootstrap stores.
    setup_launch(root, monkeypatch, bundle["context"])
    setup.write_json(root / "fence-reference.json", {"artifact_id": 42, "digest": "sha256:" + "a" * 64})
    setup.write_json(root / "approval.json", {"controlled": True})
    chosen = pilot.packet_for(bundle, publisher_available=True)
    network = []
    pending = [True]

    def verified(*args: Any) -> Doc:
        network.append("artifact")
        if pending.pop() if pending else False:
            raise history.FencePending("controlled visibility lag")
        return {"artifact_id": 42, "digest": "sha256:" + "a" * 64}

    monkeypatch.setattr(pilot, "verify_approval", lambda *args: network.append("approval"))
    monkeypatch.setattr(pilot, "source", lambda: SimpleNamespace())
    monkeypatch.setattr(pilot, "fence_document", lambda *args: {"controlled": True})
    monkeypatch.setattr(history, "verify_fence", verified)

    def publisher_metadata(*args: Any) -> Doc:
        network.append("scope")
        return dict(bundle["components"]["publisher"])

    monkeypatch.setattr(pilot, "publisher_metadata", publisher_metadata)
    boundary = []

    def controlled_boundary(operation: Any, seconds: float) -> Doc:
        boundary.append(seconds)
        return dict(operation())

    workflow: Any = SimpleNamespace(
        configuration_digest=config["native_configuration_digest"],
        services=SimpleNamespace(
            source=SimpleNamespace(), guard=SimpleNamespace(check=lambda: None), boundary=controlled_boundary
        ),
    )
    readers["authority-preflight"] = pilot.AuthorityPreflight(root, bundle, chosen, workflow)
    initial = account()
    active = authority()
    assert pilot.authority_preflight(root, bundle, chosen, active)["status"] == "fence_pending"
    assert account()["reserved"]["wall_time_ms"] == 45000
    for _ in range(70):
        assert pilot.authority_preflight(root, bundle, chosen, active)["status"] == "verified"
    assert all(0 < bound <= 45 for bound in boundary)
    assert account()["reserved"]["wall_time_ms"] == 71 * 45000

    for kind in effects:
        request = {"controlled_phase": kind}
        effect_id = planning.digest({"phase": kind, "packet": "controlled"})
        outcome = active.execute(kind, request, effect_id=effect_id)
        if kind == "publish":
            assert outcome["status"] == "uncertain"
            retained = account()
            active = authority()
            assert account() == retained
            outcome = active.execute(kind, request, effect_id=effect_id)
        assert outcome["status"] == "completed", outcome
        assert effects[kind].invocations == 1
        if kind == "dispatch":
            # Same-host restart retains the account and exact native result.
            retained = account()
            active = authority()
            assert account() == retained
            assert active.execute(kind, request, effect_id=effect_id)["status"] == "completed"
            assert effects[kind].invocations == 1
    for purpose in (
        "baseline-admission",
        "remote-precondition",
        *["negative-admission"] * 5,
        *["producer-observe"] * 20,
    ):
        assert (
            active.call_metered(purpose, {"controlled": True}, operation_id=planning.digest(purpose))["status"]
            == "completed"
        )
    assert account()["reserved"]["attempts"] == 6
    assert account()["reserved"]["wall_time_ms"] < config["limits"]["wall_time_ms"]
    assert account()["objective"] == initial["objective"]
    # Literal runner budget reader, actual accounting: final request reserves EXACT remainder.
    workflow.services.boundary = lambda operation, seconds: operation()
    budget = pilot.BudgetProbe(bundle, workflow)
    readers["budget-exhaustion"] = budget
    active = authority()
    for _ in range(30):
        remaining = config["limits"]["wall_time_ms"] - account()["reserved"]["wall_time_ms"]
        if not remaining:
            break
        request = {"contract": bundle["scope_tests"]["budget"], "wall_time_ms": min(remaining, 300000)}
        assert (
            active.call_metered("budget-exhaustion", request, operation_id=planning.digest("drain"))["status"]
            == "completed"
        )
    assert account()["reserved"]["wall_time_ms"] == config["limits"]["wall_time_ms"]
    before_network = list(network)
    with pytest.raises(ValueError, match="remaining capacity"):
        pilot.authority_preflight(root, bundle, chosen, active)
    assert network == before_network
    before_calls = readers["producer-observe"].calls
    assert active.call_metered("producer-observe", {}, operation_id=planning.digest("denied"))["status"] == "denied"
    assert readers["producer-observe"].calls == before_calls
    inspections = effects["execute"].inspections
    assert active.execute("execute", {"new": True}, effect_id=planning.digest("fresh-denied"))["status"] in {
        "denied",
        "paused",
    }
    assert effects["execute"].inspections == inspections
    assert all(effect.invocations == 1 for effect in effects.values())
    workflow.effect_registry = lambda: effects
    workflow.reader_registry = lambda: readers
    scopes = pilot.final_scope_probes(root, bundle, chosen, workflow, active, {"event": {"correlations": {}}})
    assert set(scopes) == {"exhausted-objective-budget", "expired-delegation", "revoked-delegation"}
    assert scopes["expired-delegation"]["report"]["status"] == "denied"
    assert scopes["expired-delegation"]["report"]["reason"] == "Standing delegation expired."
    assert scopes["revoked-delegation"]["report"]["status"] == "denied"
    assert scopes["revoked-delegation"]["report"]["reason"].startswith("Standing delegation revoked:")
    assert scopes["exhausted-objective-budget"]["facts"]["remaining_wall_time_ms"] == 0
    assert all(effect.invocations == 1 for effect in effects.values())


class QueueHistory:
    def get_json(self, path: str) -> Doc:
        if path.endswith("/req039-coordinator.yml"):
            return {"id": 99, "path": history.COORDINATOR_WORKFLOW}
        assert "/workflows/99/runs?" in path
        return {
            "total_count": 2,
            "workflow_runs": [
                {"id": 123, "workflow_id": 99, "run_attempt": 1, "run_number": 7, "status": "in_progress"},
                {"id": 124, "workflow_id": 99, "run_attempt": 1, "run_number": 8, "status": "queued"},
            ],
        }

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise AssertionError("a later never-started first attempt cannot have released authority")


def test_newer_never_started_queue_entry_cannot_brick_current_first_attempt() -> None:
    history.reject_prior_authority(QueueHistory(), context())
    changed = context()
    changed["run_number"] = 8
    with pytest.raises(ValueError, match="ordering"):
        history.reject_prior_authority(QueueHistory(), changed)


def test_native_zero_authority_receipt_reads_actual_empty_history(
    bootstrap_case: tuple[Path, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle = bootstrap_case
    setup_launch(root, monkeypatch, bundle["context"])
    monkeypatch.setattr(pilot, "unit_state", lambda unit: "inactive")
    assert pilot.terminal_noauthority(root)
    proof = setup.read_json(root / "public" / history.NOAUTH_MEMBER)
    assert proof["proof_kind"] == "native-zero-history" and proof["accounting_history"]["decisions"] == []
    assert proof["terminal_abort"]["stage"] == proof["preauthority_stage"]


@pytest.mark.parametrize("bootstrap_case", [True], indirect=True)
def test_runtime_construction_is_local_and_fresh_failure_spends_capacity_before_network(
    bootstrap_case: tuple[Path, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle = bootstrap_case
    setup_launch(root, monkeypatch, bundle["context"])
    chosen = pilot.packet_for(bundle, publisher_available=True)
    setup.write_json(root / "public" / "packet.json", chosen)
    selected = comment(chosen)
    stamp = pilot.utc(bundle["context"]["created_at"]) + dt.timedelta(seconds=1)
    for field in ("created_at", "updated_at"):
        selected[field] = stamp.isoformat()
    for field in ("createdAt", "updatedAt"):
        selected["original_approval"][field] = stamp.isoformat()
    receipt = pilot.find_approval(Comments([selected]), chosen)
    assert receipt is not None
    setup.write_json(root / "approval.json", receipt)
    setup.write_json(root / "fence-reference.json", {"artifact_id": 42, "digest": "sha256:" + "a" * 64})
    monkeypatch.setenv(pilot.PUBLISH_TOKEN, "controlled-publisher-not-a-live-credential")
    monkeypatch.setattr(resource_guard.Guard, "check", lambda guard: None)
    config = bundle["delegation"]

    def account() -> Doc:
        return standing_authorization.inspect(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
        )

    class NoNetwork:
        def get_json(self, path: str) -> Doc:
            assert account()["reserved"]["wall_time_ms"] >= 45000
            setup.write_json(root / "paid-network-reached.json", {"path": path})
            raise ValueError("controlled unavailable remote")

    monkeypatch.setattr(pilot, "source", lambda: NoNetwork())
    pilot.runtime_objects(root)
    assert account()["reserved"] == setup.units(0, 0)
    assert not (root / "paid-network-reached.json").exists()
    assert (root / "authority-construction.json").exists()
    with pytest.raises(ValueError, match="remaining capacity"):
        pilot.load_runtime(root)
    assert setup.read_json(root / "paid-network-reached.json")["path"].endswith("/1234")
    actual = account()
    assert actual["reserved"]["wall_time_ms"] == 45000
    assert len(actual["decisions"]) == 1 and actual["decisions"][0]["status"] == "uncertain"
    assert (
        automation_controller.show(
            Path(bundle["components"]["controller"]["journal"]), bundle["components"]["controller"]["journal_id"]
        )["sequence"]
        == 0
    )


@pytest.mark.parametrize("fault", ["missing", "reader-alias", "actions-app", "short-expiry"])
def test_unavailable_or_ineligible_publisher_is_non_authorizing_candidate_metadata(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "controlled-reader")
    monkeypatch.setenv(pilot.PUBLISH_TOKEN, "controlled-publisher")
    monkeypatch.setenv(pilot.PUBLISH_APP, "12345")
    monkeypatch.setenv(pilot.PUBLISH_INSTALLATION, "54321")
    monkeypatch.setenv(pilot.PUBLISH_EXPIRY, "2026-11-01T11:00:00Z")
    if fault == "missing":
        monkeypatch.delenv(pilot.PUBLISH_TOKEN)
    elif fault == "reader-alias":
        monkeypatch.setenv(pilot.PUBLISH_TOKEN, "controlled-reader")
    elif fault == "actions-app":
        monkeypatch.setenv(pilot.PUBLISH_APP, "15368")
    else:
        monkeypatch.setenv(pilot.PUBLISH_EXPIRY, "2026-11-01T10:10:00Z")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ineligible credential must not be used")

    monkeypatch.setattr(git_remote_transport.GitHubGitTransport, "_publisher_scope", forbidden)
    client: Any = SimpleNamespace()
    selected = pilot.publisher_metadata(context(), client)
    assert selected["scope_verified"] is False and selected["issuer_server_verified"] is False
    assert selected["app_id"] != 15368
    public = packet()
    assert public["credential_requirements"]["permissions_server_verified"] is False
    assert public["credential_requirements"]["permissions_independent_approval_required"] is True


class ArtifactHistory:
    def __init__(self) -> None:
        self.pages: dict[str, Doc] = {}
        self.archives: dict[int, bytes] = {}

    def get_json(self, path: str) -> Doc:
        assert path in self.pages, path
        return copy.deepcopy(self.pages[path])

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        assert repository == setup.REPOSITORY
        return self.archives[artifact_id]


def retained_artifact(api: ArtifactHistory, value: Doc, *, member: str, run: int = 122) -> Doc:
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, planning.canonical(value) + "\n")
    data = stream.getvalue()
    prefix = history.FENCE_PREFIX if member == history.FENCE_MEMBER else history.NOAUTH_PREFIX
    metadata = {
        "id": 55,
        "name": prefix + str(run) + "-1",
        "expired": False,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "workflow_run": {
            "id": run,
            "repository_id": setup.REPOSITORY_ID,
            "head_repository_id": setup.REPOSITORY_ID,
            "head_sha": context()["coordinator_revision"],
        },
    }
    api.archives[55] = data
    api.pages[f"/repos/{setup.REPOSITORY}/actions/artifacts/55"] = metadata
    return metadata


def prior_history() -> tuple[ArtifactHistory, Doc, Doc]:
    api = ArtifactHistory()
    prefix = f"/repos/{setup.REPOSITORY}/actions"
    api.pages[prefix + "/workflows/req039-coordinator.yml"] = {"id": 99, "path": history.COORDINATOR_WORKFLOW}
    api.pages[prefix + "/workflows/99/runs?per_page=100&page=1"] = {
        "total_count": 2,
        "workflow_runs": [
            {"id": 123, "workflow_id": 99, "run_attempt": 1, "run_number": 7},
            {"id": 122, "workflow_id": 99, "run_attempt": 1, "run_number": 6},
        ],
    }
    api.pages[prefix + "/runs/122/attempts/1/jobs?per_page=100"] = {
        "total_count": 1,
        "jobs": [
            {
                "id": 456,
                "run_id": 122,
                "status": "completed",
                "steps": [
                    {"name": history.FENCE_STEP, "status": "completed", "conclusion": "skipped"},
                    {"name": history.NOAUTH_STEP, "status": "completed", "conclusion": "success"},
                ],
            }
        ],
    }
    stage = {
        "schema_version": "0.1.0",
        "stage": "preauthority",
        "launch_digest": "sha256:" + "a" * 64,
        "launch_device": 1,
        "launch_inode": 2,
    }
    proof = {
        "schema_version": "0.1.0",
        "kind": "req039-terminal-no-authority",
        "proof_kind": "setup-only-never-authorized",
        "original_goal": setup.ORIGINAL_GOAL,
        "run_id": "122",
        "run_attempt": "1",
        "independent_approval_seen": False,
        "controller_sequence": 0,
        "authority_decisions": [],
        "reserved": setup.units(0, 0),
        "context": {"run_id": "122", "run_attempt": "1", "host": {"job_id": "456"}},
        "preauthority_stage": stage,
        "terminal_abort": {
            "schema_version": "0.1.0",
            "launch_digest": stage["launch_digest"],
            "stage": stage,
            "reason": "terminal-never-authorized",
        },
    }
    artifact = retained_artifact(api, proof, member=history.NOAUTH_MEMBER)
    api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"] = {"total_count": 1, "artifacts": [artifact]}
    return api, proof, artifact


def test_actual_immutable_terminal_artifact_allows_only_proven_never_authorized_prior_run() -> None:
    api, _, _ = prior_history()
    history.reject_prior_authority(api, context())


@pytest.mark.parametrize(
    "fault",
    ["expired", "deleted", "consumed-fence", "partial-history", "approval-seen", "missing-abort", "upload-not-proven"],
)
def test_missing_or_ambiguous_prior_authority_history_never_resets_goal(fault: str) -> None:
    api, proof, artifact = prior_history()
    prefix = f"/repos/{setup.REPOSITORY}/actions"
    if fault == "expired":
        artifact["expired"] = True
    elif fault == "deleted":
        api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"] = {"total_count": 0, "artifacts": []}
    elif fault == "consumed-fence":
        listing = api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"]
        listing["total_count"] += 1
        listing["artifacts"].append({"id": 999, "name": history.FENCE_PREFIX + "122-1", "expired": True})
    elif fault == "partial-history":
        api.pages[prefix + "/workflows/99/runs?per_page=100&page=1"]["total_count"] = 3
    elif fault == "upload-not-proven":
        api.pages[prefix + "/runs/122/attempts/1/jobs?per_page=100"]["jobs"][0]["steps"][1]["conclusion"] = "failure"
    else:
        if fault == "approval-seen":
            proof["independent_approval_seen"] = True
        else:
            proof.pop("terminal_abort")
        fresh = retained_artifact(api, proof, member=history.NOAUTH_MEMBER)
        api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"]["artifacts"] = [fresh]
    with pytest.raises(ValueError):
        history.reject_prior_authority(api, context())


def test_exact_immutable_workflow_and_server_skipped_launch_allow_setup_retry() -> None:
    api, _, _ = prior_history()
    prefix = f"/repos/{setup.REPOSITORY}/actions"
    run = api.pages[prefix + "/workflows/99/runs?per_page=100&page=1"]["workflow_runs"][1]
    run.update(
        event="workflow_dispatch",
        head_branch="main",
        head_sha="a" * 40,
        repository={"id": setup.REPOSITORY_ID},
        head_repository={"id": setup.REPOSITORY_ID},
        path=history.COORDINATOR_WORKFLOW,
    )
    api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"] = {"total_count": 0, "artifacts": []}
    steps = api.pages[prefix + "/runs/122/attempts/1/jobs?per_page=100"]["jobs"][0]["steps"]
    steps.append({"name": history.LAUNCH_STEP, "status": "completed", "conclusion": "skipped"})
    content = b"fixed independently reviewed workflow bytes\n"
    blob = {
        "path": history.COORDINATOR_WORKFLOW,
        "type": "file",
        "encoding": "base64",
        "size": len(content),
        "content": base64.b64encode(content).decode(),
        "sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
    }
    for revision in (run["head_sha"], context()["coordinator_revision"]):
        api.pages[f"/repos/{setup.REPOSITORY}/contents/{history.COORDINATOR_WORKFLOW}?ref={revision}"] = copy.deepcopy(
            blob
        )
    history.reject_prior_authority(api, context())
    steps[-1]["conclusion"] = "failure"
    with pytest.raises(ValueError, match="terminal no-authority"):
        history.reject_prior_authority(api, context())
    steps[-1]["conclusion"] = "skipped"
    api.pages[f"/repos/{setup.REPOSITORY}/contents/{history.COORDINATOR_WORKFLOW}?ref={run['head_sha']}"]["sha"] = (
        "0" * 40
    )
    with pytest.raises(ValueError, match="blob identity"):
        history.reject_prior_authority(api, context())


def rejected_history(monkeypatch: pytest.MonkeyPatch) -> tuple[ArtifactHistory, Doc]:
    api, _, _ = prior_history()
    prefix = f"/repos/{setup.REPOSITORY}/actions"
    run = api.pages[prefix + "/workflows/99/runs?per_page=100&page=1"]["workflow_runs"][1]
    run.update(
        event="push",
        head_branch="pilot/req039-consumer",
        head_sha="a" * 40,
        status="completed",
        conclusion="failure",
    )
    actual = {
        **run,
        "repository": {"id": setup.REPOSITORY_ID},
        "head_repository": {"id": setup.REPOSITORY_ID},
        "path": history.COORDINATOR_WORKFLOW,
    }
    api.pages[prefix + "/runs/122/attempts/1"] = actual
    api.pages[prefix + "/runs/122/attempts/1/jobs?per_page=100"] = {"total_count": 0, "jobs": []}
    api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"] = {"total_count": 0, "artifacts": []}
    # Substitute only the historical immutable-template pin, leaving every live API join intact.
    content = b"one known server-rejected immutable workflow template\n"
    monkeypatch.setattr(history, "REJECTED_WORKFLOW_SHA256", hashlib.sha256(content).hexdigest())
    api.pages[f"/repos/{setup.REPOSITORY}/contents/{history.COORDINATOR_WORKFLOW}?ref={run['head_sha']}"] = {
        "path": history.COORDINATOR_WORKFLOW,
        "type": "file",
        "encoding": "base64",
        "size": len(content),
        "content": base64.b64encode(content).decode(),
        "sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
    }
    return api, run


def test_known_server_rejected_template_needs_fresh_empty_execution_history(monkeypatch: pytest.MonkeyPatch) -> None:
    api, _ = rejected_history(monkeypatch)
    history.reject_prior_authority(api, context())


@pytest.mark.parametrize(
    "fault",
    [
        "unreviewed-template",
        "false-blob-sha",
        "changed-revision",
        "changed-run",
        "changed-workflow",
        "changed-order",
        "changed-branch",
        "foreign-repository",
        "foreign-head-repository",
        "wrong-path",
        "still-running",
        "success",
        "different-event",
        "rerun",
        "boolean-attempt",
        "missing-jobs",
        "partial-jobs",
        "boolean-job-count",
        "executed-job",
        "retained-review-packet",
        "retained-goal-fence",
        "partial-artifacts",
    ],
)
def test_jobless_history_cannot_discard_unknown_or_potentially_authorized_runs(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    api, _ = rejected_history(monkeypatch)
    prefix = f"/repos/{setup.REPOSITORY}/actions"
    actual = api.pages[prefix + "/runs/122/attempts/1"]
    jobs = api.pages[prefix + "/runs/122/attempts/1/jobs?per_page=100"]
    artifacts = api.pages[prefix + "/runs/122/artifacts?per_page=100&page=1"]
    blob = api.pages[f"/repos/{setup.REPOSITORY}/contents/{history.COORDINATOR_WORKFLOW}?ref={'a' * 40}"]
    if fault == "unreviewed-template":
        monkeypatch.setattr(history, "REJECTED_WORKFLOW_SHA256", "0" * 64)
    elif fault == "false-blob-sha":
        blob["sha"] = "0" * 40
    elif fault == "changed-revision":
        actual["head_sha"] = "b" * 40
    elif fault == "changed-run":
        actual["id"] = 121
    elif fault == "changed-workflow":
        actual["workflow_id"] = 100
    elif fault == "changed-order":
        actual["run_number"] = 5
    elif fault == "changed-branch":
        actual["head_branch"] = "main"
    elif fault == "foreign-repository":
        actual["repository"]["id"] += 1
    elif fault == "foreign-head-repository":
        actual["head_repository"]["id"] += 1
    elif fault == "wrong-path":
        actual["path"] = ".github/workflows/another.yml"
    elif fault == "still-running":
        actual["status"] = "in_progress"
    elif fault == "success":
        actual["conclusion"] = "success"
    elif fault == "different-event":
        actual["event"] = "workflow_dispatch"
    elif fault == "rerun":
        actual["run_attempt"] = 2
    elif fault == "boolean-attempt":
        actual["run_attempt"] = True
    elif fault == "missing-jobs":
        jobs.pop("jobs")
    elif fault == "partial-jobs":
        jobs["total_count"] = 1
    elif fault == "boolean-job-count":
        jobs["total_count"] = False
    elif fault == "executed-job":
        jobs.update(total_count=1, jobs=[{"id": 456, "run_id": 122, "status": "completed", "steps": []}])
    elif fault in {"retained-review-packet", "retained-goal-fence"}:
        name = ("exacly-req039-review-" if fault == "retained-review-packet" else history.FENCE_PREFIX) + "122-1"
        artifacts.update(total_count=1, artifacts=[{"id": 55, "name": name}])
    else:
        artifacts["total_count"] = 1
    with pytest.raises(ValueError):
        history.reject_prior_authority(api, context())


@pytest.mark.parametrize("bootstrap_case", [True], indirect=True)
def test_literal_remote_precondition_reader_is_paid_bounded_and_cannot_publish(
    bootstrap_case: tuple[Path, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle = bootstrap_case
    config = bundle["delegation"]
    selected = bundle["scope_tests"]["remote_precondition"]
    actual: Doc = {
        "event": {"status": "completed", "correlations": {"handoff": {"handoff_id": planning.digest("handoff")}}}
    }
    monkeypatch.setattr(automation_controller, "show", lambda *args: copy.deepcopy(actual))
    calls = []
    boundaries = []

    def observed(request: Any, *, authorization: Doc, transport: Any, timeout: float) -> Doc:
        account = standing_authorization.inspect(
            Path(config["accounting"]["path"]),
            store_id=config["accounting"]["store_id"],
            objective_id=config["objective"]["id"],
        )
        assert account["reserved"]["wall_time_ms"] == 45000
        assert authorization == {"controlled": True} and timeout == 30
        assert request.expected_revision == bundle["base"]
        assert (
            request.operation_marker_ref
            == "refs/tags/req039-operations/" + bundle["context"]["nonce"] + "-precondition"
        )
        calls.append("read-only-native-precondition")
        return {
            "status": "conflict",
            "operation_id": planning.digest("unreleased-probe"),
            "observation": {"revision": "c" * 40},
            "claims": {"effect_authorized": False, "remote_published": False},
        }

    def boundary(operation: Any, seconds: float) -> Doc:
        boundaries.append(seconds)
        return dict(operation())

    monkeypatch.setattr(remote_publication, "preview", lambda request: {"authorization_template": {"controlled": True}})
    monkeypatch.setattr(remote_publication, "inspect_precondition", observed)
    monkeypatch.setattr(remote_publication, "lookup", lambda *args: None)
    workflow: Any = SimpleNamespace(
        configuration_digest=config["native_configuration_digest"],
        services=SimpleNamespace(boundary=boundary, transport=SimpleNamespace()),
    )
    reader = pilot.RemotePrecondition(bundle, workflow)
    authority = automation_authorization.Authorizer(
        Path(config["config_path"]),
        approved_digest=planning.digest(config),
        issuer=config["issuer"],
        approval_id=config["approval_id"],
        effects={
            kind: ControlledEffect(config, kind, root / (kind + "-unused-intent.json")) for kind in config["effects"]
        },
        readers={
            **{purpose: ControlledReader(config, purpose) for purpose in config["reads"]},
            "remote-precondition": reader,
        },
    )
    request = {"contract": selected, "handoff_id": actual["event"]["correlations"]["handoff"]["handoff_id"]}
    result = authority.call_metered(
        "remote-precondition", request, operation_id=planning.digest("read-only-ref-conflict")
    )
    assert result["status"] == "completed" and calls == ["read-only-native-precondition"]
    assert result["receipt"]["result"]["report"]["status"] == "conflict"
    assert boundaries == [45]
    actual["event"]["status"] = "running"
    assert (
        authority.call_metered("remote-precondition", request, operation_id=planning.digest("early-probe"))["status"]
        == "paused"
    )
    assert calls == ["read-only-native-precondition"]


@pytest.mark.parametrize("foreign", ["demo", "unattended-lookalike"])
def test_foreign_namespace_with_same_req001_and_fixed_case_is_rejected_before_native_state(
    tmp_path: Path, foreign: str
) -> None:
    files = {name: (ROOT / name).read_bytes() for name in (setup.MODEL, setup.WORKFLOW, setup.SOURCE)}
    files[setup.MODEL] = files[setup.MODEL].replace(b"namespace: req039-consumer", ("namespace: " + foreign).encode())
    checkout_path, revision, tree, entries = checkout(tmp_path, files)
    api = BootstrapSource(files, revision, tree, entries)
    root = setup.private_directory(tmp_path / "foreign-pilot", create=True)
    with pytest.raises(ValueError, match="req039-consumer model namespace"):
        setup.bootstrap(root, checkout_path, revision, context(), api, guard(), baseline_run=777)
    assert not (root / "state" / "current.json").exists()
    assert not (root / "bootstrap.json").exists()
    assert list((root / "ledger").iterdir()) == []
    assert list((root / "controller").iterdir()) == []
    assert list((root / "accounting").iterdir()) == []
