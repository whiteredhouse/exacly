"""Required native producers remain independent while their readiness is composed with AND."""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from exacly import admission, composition
from exacly.github_evidence import GitHubError
from test_admission import MODEL_TREE, Source, blob_sha, sha256
from test_applicability import WHEN, A, B
from test_refresh import config, requirement

PLATFORMS = ("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64")
MODEL_FILE = ".quality/contracts.yaml"


def producer(index: int) -> dict[str, Any]:
    platform = PLATFORMS[index]
    return {
        "branch": "release/stable",
        "workflow": {
            "path": f".github/workflows/native-{platform}.yml",
            "job_id": "observe",
            "job_name": f"Observe {platform}",
            "steps": {
                "verification": "Exercise release behavior",
                "package": "Build retained results",
                "upload": "Retain contract report",
            },
        },
        "artifact": {"name_prefix": f"native-{platform}"},
    }


def case(index: int) -> str:
    return f"checks/test_native.py::test_{PLATFORMS[index].replace('-', '_')}"


def workflow(contract: dict[str, Any]) -> bytes:
    fixture = Source(layout="custom")
    fixture.producer = contract
    fixture.branch = contract["branch"]
    fixture.job_id = contract["workflow"]["job_id"]
    fixture.job_name = contract["workflow"]["job_name"]
    fixture.artifact_prefix = contract["artifact"]["name_prefix"]
    return fixture.custom_workflow()


class Router:
    """Four real native fixture producers; no admission or readiness functions are stubbed."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.archives: dict[tuple[str, int], bytes] = {}
        self.calls: list[str] = []
        self.downloads: list[tuple[str, int]] = []
        self.unavailable: set[str] = set()

    def get_json(self, path: str) -> dict[str, Any]:
        self.calls.append(path)
        if path in self.unavailable:
            raise GitHubError("selected producer is unavailable")
        url = urlsplit(path)
        key = unquote(url.path).lstrip("/")
        if key.endswith(("/jobs", "/artifacts")):
            key += "?page=" + parse_qs(url.query).get("page", ["1"])[0]
        assert key in self.pages, f"unexpected GitHub request: {path}"
        return copy.deepcopy(self.pages[key])

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        key = repository, artifact_id
        self.downloads.append(key)
        return self.archives[key]


@dataclass
class Example:
    definition: dict[str, Any]
    inputs: dict[str, composition.MemberInputs]
    source: Router
    selection: dict[str, Any]
    fixtures: dict[str, Source]

    def prepare(self) -> composition.PreparedDefinition:
        return composition.validate_definition(self.definition, self.inputs, WHEN)

    def evaluate(self) -> composition.CompositionResult:
        return composition.evaluate(self.source, self.prepare(), self.selection, WHEN)


def example(
    *,
    outcomes: dict[str, str] | None = None,
    reports: dict[str, bytes] | None = None,
    repositories: tuple[str, ...] = ("acme/native",) * 4,
    revisions: tuple[str, ...] = (B,) * 4,
) -> Example:
    """Build coherent GitHub metadata, immutable trees, archives and independently approved policies."""
    definition: dict[str, Any] = {
        "schema_version": "0.1.0",
        "name": "native-release",
        "version": "1",
        "members": [
            {
                "id": platform,
                "trust_policy": f"trusted/{platform}.json",
                "readiness_policy": f"readiness/{platform}.yaml",
                "model_root": ".",
            }
            for platform in PLATFORMS
        ],
        "constraints": [],
    }
    router = Router()
    inputs = {}
    fixtures = {}
    selections = []
    repository_ids = {name: 400 + index for index, name in enumerate(dict.fromkeys(repositories))}
    for index, platform in enumerate(PLATFORMS):
        repository, revision = repositories[index], revisions[index]
        repo_id = repository_ids[repository]
        base = f"repos/{repository}"
        run_id, artifact_id, workflow_id = 700 + index, 800 + index, 900 + index
        contract = producer(index)
        documents = config(
            *(
                requirement(
                    f"REQ-{i + 1}",
                    {"workflow": producer(i)["workflow"]["path"], "job": "observe", "cases": [case(i)]},
                )
                for i in range(4)
            )
        )
        documents["model"]["namespace"] = repository.replace("/", "-")
        data: dict[str, bytes] = {
            **{producer(i)["workflow"]["path"]: workflow(producer(i)) for i in range(4)},
            "tools/report_bundle.py": b"# reviewed report packager\n",
            "requirements.lock": b"# reviewed dependencies\n",
            "quality.toml": b"# reviewed runner configuration\n",
            "checks/test_native.py": b"# reviewed native verification cases\n",
            MODEL_FILE: yaml.safe_dump(documents).encode(),
        }
        tree = []
        for path, content in data.items():
            digest = blob_sha(content)
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": digest, "size": len(content)})
            router.pages[f"{base}/git/blobs/{digest}"] = {
                "sha": digest,
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
                "size": len(content),
            }
        implementation_tree = "e" * 40
        router.pages[f"{base}/git/trees/{implementation_tree}"] = {
            "sha": implementation_tree,
            "truncated": False,
            "tree": tree,
        }
        model_entry = next(entry for entry in tree if entry["path"] == MODEL_FILE)
        router.pages[f"{base}/git/trees/{MODEL_TREE}"] = {"sha": MODEL_TREE, "truncated": False, "tree": [model_entry]}
        router.pages[f"{base}/git/commits/{A}"] = {"sha": A, "tree": {"sha": MODEL_TREE}}
        router.pages[f"{base}/git/commits/{revision}"] = {"sha": revision, "tree": {"sha": implementation_tree}}
        router.pages[base] = {"id": repo_id, "full_name": repository, "default_branch": "main"}
        router.pages[f"{base}/branches/release/stable"] = {
            "name": "release/stable",
            "protected": True,
            "commit": {"sha": revision},
        }
        outcome = (outcomes or {}).get(platform, "success")
        fragment = b'<failure message="native behavior failed"/>' if outcome == "failure" else b""
        test_file, test_name = case(index).split("::")
        report = (reports or {}).get(
            platform,
            f'<testsuite><testcase file="{test_file}" name="{test_name}">'.encode()
            + fragment
            + b"</testcase></testsuite>",
        )
        fixture = Source(outcome, report, layout="custom")
        fixture.run.update(
            id=run_id,
            head_sha=revision,
            path=contract["workflow"]["path"],
            workflow_id=workflow_id,
            repository={"id": repo_id, "full_name": repository},
            head_repository={"id": repo_id, "full_name": repository},
            html_url=f"https://github.com/{repository}/actions/runs/{run_id}",
        )
        fixture.job.update(id=1000 + index, run_id=run_id, head_sha=revision, name=contract["workflow"]["job_name"])
        fixture.artifact.update(
            id=artifact_id,
            name=f"{contract['artifact']['name_prefix']}-{run_id}-1",
            workflow_run={
                "id": run_id,
                "repository_id": repo_id,
                "head_repository_id": repo_id,
                "head_branch": "release/stable",
                "head_sha": revision,
            },
        )
        fixture.manifest.update(
            repository_id=repo_id,
            repository=repository,
            run_id=run_id,
            workflow_path=contract["workflow"]["path"],
            job="observe",
            implementation_revision=revision,
        )
        fixture.rebundle()
        router.pages[f"{base}/actions/runs/{run_id}/attempts/1"] = fixture.run
        router.pages[f"{base}/actions/workflows/{workflow_id}"] = {
            "id": workflow_id,
            "path": contract["workflow"]["path"],
            "state": "active",
        }
        router.pages[f"{base}/actions/runs/{run_id}/attempts/1/jobs?page=1"] = {"total_count": 1, "jobs": [fixture.job]}
        router.pages[f"{base}/actions/runs/{run_id}/artifacts?page=1"] = {
            "total_count": 1,
            "artifacts": [fixture.artifact],
        }
        router.pages[f"{base}/actions/artifacts/{artifact_id}"] = fixture.artifact
        router.archives[repository, artifact_id] = fixture.archive
        selectors = [contract["workflow"]["path"], "tools", "requirements.lock", "quality.toml", "checks"]
        trust = {
            "name": f"approved-{platform}",
            "version": "1",
            "repository": {"id": repo_id, "full_name": repository},
            **copy.deepcopy(contract),
            "observer": {"paths": selectors, "digest": admission.observer_digest(tree, selectors)},
            "model": {"revision": A, "files": [MODEL_FILE], "digest": admission.config_digest({MODEL_FILE: documents})},
        }
        trust["workflow"]["digest"] = sha256(data[contract["workflow"]["path"]])
        ready = {
            "name": f"ready-{platform}",
            "version": "1",
            "requirements": [{"record": f"REQ-{index + 1}", "dimensions": ["verification"]}],
        }
        inputs[platform] = composition.MemberInputs(trust, ready, {MODEL_FILE: documents})
        fixtures[platform] = fixture
        selections.append(
            {
                "id": platform,
                "run": run_id,
                "attempt": 1,
                "target": {"context": "native-ci", "model_revision": A, "revision": revision},
            }
        )
    prepared = composition.validate_definition(definition, inputs, WHEN)
    selection = {"schema_version": "0.1.0", "definition_digest": prepared.digest, "members": selections}
    return Example(definition, inputs, router, selection, fixtures)


def member_reports(result: composition.CompositionResult) -> dict[str, Any]:
    return {member["id"]: member for member in result.report["members"]}


def test_four_approved_native_producers_admit_and_gate_independently() -> None:
    fixture = example()
    fixture.definition["constraints"] = [
        {"kind": kind, "members": list(PLATFORMS)} for kind in ("same_revision", "same_model")
    ]
    fixture.selection["definition_digest"] = fixture.prepare().digest
    before = copy.deepcopy((fixture.definition, fixture.inputs, fixture.selection))
    result = fixture.evaluate()
    assert result.report["complete"] is True
    assert result.report["result"] == "satisfied", result.report
    assert set(result.admissions) == set(PLATFORMS)
    reports = member_reports(result)
    assert set(reports) == set(PLATFORMS)
    for index, platform in enumerate(PLATFORMS):
        admitted = result.admissions[platform]
        assert admitted.receipt["status"] == "admitted"
        assert admitted.receipt["origin"]["workflow_path"] == producer(index)["workflow"]["path"]
        assert admitted.receipt["origin"]["run_id"] == 700 + index
        assert admitted.state is not None
        assert admitted.state["namespace"] == "acme-native"
        assert admitted.state["context"] == "native-ci"
        assert reports[platform]["gate"]["result"] == "satisfied"
    assert fixture.source.downloads == [("acme/native", 800 + index) for index in range(4)]
    assert (fixture.definition, fixture.inputs, fixture.selection) == before


@pytest.mark.parametrize("identifier", ["linux\n", "../linux", "linux/amd64", "-linux", "linux amd64", "a" * 65])
def test_member_ids_cannot_escape_or_ambiguate_retained_member_paths(identifier: str) -> None:
    fixture = example()
    fixture.definition["members"][0]["id"] = identifier
    with pytest.raises(ValueError):
        composition.validate_definition_document(fixture.definition)


@pytest.mark.parametrize("bad", ["empty", "duplicate", "absolute-reference", "unknown-field"])
def test_invalid_definitions_are_rejected_before_input_resolution(bad: str) -> None:
    fixture = example()
    if bad == "empty":
        fixture.definition["members"].clear()
    elif bad == "duplicate":
        fixture.definition["members"].append(copy.deepcopy(fixture.definition["members"][0]))
    elif bad == "absolute-reference":
        fixture.definition["members"][0]["trust_policy"] = "/trusted/producer.json"
    else:
        fixture.definition["optional_members"] = [PLATFORMS[0]]
    with pytest.raises(ValueError):
        composition.validate_definition_document(fixture.definition)


@pytest.mark.parametrize(
    "bad", ["missing", "duplicate", "extra", "bool-run", "zero-attempt", "platform", "wrong-model"]
)
def test_invalid_or_incomplete_selection_is_rejected_before_any_source_read(bad: str) -> None:
    fixture = example()
    members = fixture.selection["members"]
    if bad == "missing":
        members.pop()
    elif bad == "duplicate":
        members.append(copy.deepcopy(members[0]))
    elif bad == "extra":
        members.append({**copy.deepcopy(members[0]), "id": "foreign-platform"})
    elif bad == "bool-run":
        members[0]["run"] = True
    elif bad == "zero-attempt":
        members[0]["attempt"] = 0
    elif bad == "platform":
        members[0]["target"]["platform"] = "darwin/arm64"
    else:
        members[0]["target"]["model_revision"] = "f" * 40
    with pytest.raises(ValueError):
        fixture.evaluate()
    assert fixture.source.calls == []
    assert fixture.source.downloads == []


@pytest.mark.parametrize("change", ["producer", "readiness", "model", "members"])
def test_changed_approved_definition_cannot_reuse_a_selection_for_an_older_contract(change: str) -> None:
    fixture = example()
    first, second = PLATFORMS[:2]
    if change == "producer":
        fixture.inputs[first] = composition.MemberInputs(
            copy.deepcopy(fixture.inputs[second].trust_policy),
            fixture.inputs[first].readiness_policy,
            fixture.inputs[first].documents,
        )
    elif change == "readiness":
        fixture.inputs[first].readiness_policy["requirements"] = [{"record": "REQ-2", "dimensions": ["verification"]}]
    elif change == "model":
        document = fixture.inputs[first].documents[MODEL_FILE]
        document["records"][-1]["acceptance_criteria"] = ["A different reviewed behavior."]
        fixture.inputs[first].trust_policy["model"]["digest"] = admission.config_digest(fixture.inputs[first].documents)
    else:
        fixture.definition["members"].pop()
        fixture.inputs.pop(PLATFORMS[-1])
        fixture.selection["members"].pop()
    with pytest.raises(ValueError):
        fixture.evaluate()
    assert fixture.source.calls == []


@pytest.mark.parametrize(
    "bad", ["unknown-record", "duplicate-qualified-check", "foreign-state", "unapproved-model", "unknown-constraint"]
)
def test_all_member_inputs_are_preflighted_before_fetching_an_earlier_valid_member(bad: str) -> None:
    fixture = example()
    last = fixture.inputs[PLATFORMS[-1]]
    if bad == "unknown-record":
        last.readiness_policy["requirements"][0]["record"] = "REQ-missing"
    elif bad == "duplicate-qualified-check":
        last.readiness_policy["requirements"].append({"record": "acme-native:REQ-4", "dimensions": ["verification"]})
    elif bad == "foreign-state":
        last.documents["state.json"] = {"exacly_state": "0.2.0", "namespace": "other"}
    elif bad == "unapproved-model":
        last.documents[MODEL_FILE]["records"][-1]["acceptance_criteria"] = ["Local criteria changed."]
    else:
        fixture.definition["constraints"] = [{"kind": "same_revision", "members": [PLATFORMS[0], "missing"]}]
    with pytest.raises(ValueError):
        fixture.evaluate()
    assert fixture.source.calls == []
    assert fixture.source.downloads == []


@pytest.mark.parametrize("outcome", ["failure", "skipped", "incomplete"])
def test_admission_success_is_not_aggregate_readiness(outcome: str) -> None:
    selected = PLATFORMS[1]
    reports = {}
    if outcome != "failure":
        report_case = case(1).split("::")[1]
        reports[selected] = (
            (
                f'<testsuite><testcase file="checks/test_native.py" name="{report_case}">'
                "<skipped/></testcase></testsuite>"
            ).encode()
            if outcome == "skipped"
            else b'<testsuite><testcase file="checks/test_native.py" name="unrelated"/></testsuite>'
        )
    fixture = example(outcomes={selected: "failure"} if outcome == "failure" else {}, reports=reports)
    result = fixture.evaluate()
    assert result.report["complete"] is True
    assert result.report["result"] == "blocked"
    assert result.admissions[selected].receipt["status"] == "admitted"
    assert member_reports(result)[selected]["gate"]["result"] == (
        "not_satisfied" if outcome == "failure" else "unknown"
    )
    assert len(result.admissions) == 4
    assert all(
        member_reports(result)[platform]["gate"]["result"] == "satisfied"
        for platform in PLATFORMS
        if platform != selected
    )


def test_rejected_or_unavailable_member_blocks_without_hiding_other_member_results() -> None:
    fixture = example()
    rejected = PLATFORMS[0]
    fixture.source.unavailable.add("/repos/acme/native/actions/runs/700/attempts/1")
    result = fixture.evaluate()
    assert result.report["result"] == "blocked"
    assert result.admissions[rejected].receipt["status"] == "rejected"
    assert len(result.report["members"]) == 4
    assert len(fixture.source.downloads) == 3
    assert all(result.admissions[platform].receipt["status"] == "admitted" for platform in PLATFORMS[1:])


def test_another_members_run_cannot_supply_the_required_approved_producer() -> None:
    fixture = example()
    fixture.selection["members"][0]["run"] = fixture.selection["members"][1]["run"]
    result = fixture.evaluate()
    assert result.report["result"] == "blocked"
    assert result.admissions[PLATFORMS[0]].receipt["status"] == "rejected"
    assert len(result.report["members"]) == 4


def test_wrong_implementation_revision_blocks_the_selected_member() -> None:
    fixture = example()
    fixture.selection["members"][1]["target"]["revision"] = "f" * 40
    result = fixture.evaluate()
    assert result.report["result"] == "blocked"
    assert result.admissions[PLATFORMS[1]].receipt["status"] == "rejected"


def test_member_states_keep_their_own_namespace_and_context() -> None:
    fixture = example(
        repositories=("acme/native", "acme/native", "acme/service", "acme/service"),
        revisions=(B, B, "f" * 40, "f" * 40),
    )
    fixture.selection["members"][2]["target"]["context"] = "service-native"
    result = fixture.evaluate()
    assert result.report["result"] == "satisfied", result.report
    native = result.admissions[PLATFORMS[0]].state
    service = result.admissions[PLATFORMS[2]].state
    assert native is not None and service is not None
    assert native["namespace"] == "acme-native"
    assert service["namespace"] == "acme-service"
    assert service["context"] == "service-native"


@pytest.mark.parametrize("kind", ["same_revision", "same_model"])
def test_explicit_consistency_groups_do_not_conflate_different_repositories(kind: str) -> None:
    fixture = example(repositories=("acme/native", "acme/native", "acme/service", "acme/service"))
    fixture.definition["constraints"] = [{"kind": kind, "members": list(PLATFORMS)}]
    with pytest.raises(ValueError):
        fixture.selection["definition_digest"] = fixture.prepare().digest
        fixture.evaluate()
    assert fixture.source.calls == []


def test_scoped_groups_allow_independent_revisions_in_other_repositories() -> None:
    fixture = example(
        repositories=("acme/native", "acme/native", "acme/service", "acme/service"),
        revisions=(B, B, "f" * 40, "f" * 40),
    )
    fixture.definition["constraints"] = [
        {"kind": "same_revision", "members": list(PLATFORMS[:2])},
        {"kind": "same_revision", "members": list(PLATFORMS[2:])},
    ]
    fixture.selection["definition_digest"] = fixture.prepare().digest
    assert fixture.evaluate().report["result"] == "satisfied"


def test_mismatched_selected_revision_under_an_equality_constraint_fails_before_fetch() -> None:
    fixture = example()
    fixture.definition["constraints"] = [{"kind": "same_revision", "members": list(PLATFORMS)}]
    fixture.selection["definition_digest"] = fixture.prepare().digest
    fixture.selection["members"][1]["target"]["revision"] = "f" * 40
    with pytest.raises(ValueError):
        fixture.evaluate()
    assert fixture.source.calls == []


@pytest.mark.parametrize("changed", ["namespace", "criteria", "revision", "file"])
def test_model_equality_binds_complete_approved_model_identity(changed: str) -> None:
    fixture = example()
    selected = fixture.inputs[PLATFORMS[-1]]
    if changed == "namespace":
        selected.documents[MODEL_FILE]["model"]["namespace"] = "another-approved-model"
    elif changed == "criteria":
        selected.documents[MODEL_FILE]["records"][-1]["acceptance_criteria"] = ["Other reviewed criteria."]
    elif changed == "revision":
        selected.trust_policy["model"]["revision"] = "f" * 40
    else:
        selected.documents[".quality/renamed.yaml"] = selected.documents.pop(MODEL_FILE)
        selected.trust_policy["model"]["files"] = [".quality/renamed.yaml"]
    selected.trust_policy["model"]["digest"] = admission.config_digest(selected.documents)
    fixture.definition["constraints"] = [{"kind": "same_model", "members": list(PLATFORMS)}]
    with pytest.raises(ValueError):
        fixture.prepare()
    assert fixture.source.calls == []


def test_failed_producer_cannot_turn_a_passing_test_fragment_into_aggregate_success() -> None:
    selected = PLATFORMS[2]
    report = (
        f'<testsuite><testcase file="checks/test_native.py" name="{case(2).split("::")[1]}"/></testsuite>'
    ).encode()
    fixture = example(outcomes={selected: "failure"}, reports={selected: report})
    result = fixture.evaluate()
    assert result.report["result"] == "blocked"
    assert result.admissions[selected].receipt["status"] == "admitted"
    assert member_reports(result)[selected]["gate"]["result"] == "unknown"


def test_every_new_evaluation_rechecks_live_metadata_and_downloads_all_selected_artifacts() -> None:
    fixture = example()
    prepared = fixture.prepare()
    assert composition.evaluate(fixture.source, prepared, fixture.selection, WHEN).report["result"] == "satisfied"
    first_calls = len(fixture.source.calls)
    assert len(fixture.source.downloads) == 4
    fixture.source.pages["repos/acme/native/actions/artifacts/800"]["expired"] = True
    result = composition.evaluate(fixture.source, prepared, fixture.selection, WHEN)
    assert result.report["result"] == "blocked"
    assert result.admissions[PLATFORMS[0]].receipt["status"] == "rejected"
    assert len(fixture.source.calls) > first_calls
    assert len(fixture.source.downloads) == 7


def test_interrupted_member_retention_never_returns_success_and_next_invocation_admits_afresh() -> None:
    fixture = example()
    prepared = fixture.prepare()
    completed_members = []

    def retain(member: dict[str, Any], admitted: admission.AdmissionResult) -> None:
        assert admitted.receipt["status"] == "admitted"
        completed_members.append(member["id"])
        raise OSError("output destination became unavailable")

    with pytest.raises(OSError, match="destination became unavailable"):
        composition.evaluate(fixture.source, prepared, fixture.selection, WHEN, on_member=retain)
    assert completed_members == [PLATFORMS[0]]
    assert fixture.source.downloads == [("acme/native", 800)]
    retried = composition.evaluate(fixture.source, prepared, fixture.selection, WHEN)
    assert retried.report["complete"] is True
    assert retried.report["result"] == "satisfied"
    assert fixture.source.downloads == [("acme/native", 800), *[("acme/native", 800 + i) for i in range(4)]]


def test_retention_callback_cannot_rewrite_returned_admission_or_aggregate_decisions() -> None:
    fixture = example()

    def mutate(member: dict[str, Any], admitted: admission.AdmissionResult) -> None:
        member["status"] = "blocked"
        admitted.receipt["status"] = "rejected"
        assert admitted.state is not None
        admitted.state.clear()

    result = composition.evaluate(fixture.source, fixture.prepare(), fixture.selection, WHEN, on_member=mutate)
    assert result.report["result"] == "satisfied"
    assert all(member["status"] == "satisfied" for member in result.report["members"])
    for admitted in result.admissions.values():
        assert admitted.receipt["status"] == "admitted"
        assert admitted.state is not None
        assert admitted.state["namespace"] == "acme-native"


def test_prepared_contract_snapshots_caller_owned_policies_and_model_documents() -> None:
    fixture = example()
    prepared = fixture.prepare()
    fixture.definition["members"].clear()
    fixture.inputs[PLATFORMS[0]].readiness_policy["requirements"].clear()
    fixture.inputs[PLATFORMS[0]].trust_policy["repository"]["id"] = 99999
    fixture.inputs[PLATFORMS[0]].documents.clear()
    result = composition.evaluate(fixture.source, prepared, fixture.selection, WHEN)
    assert result.report["result"] == "satisfied"
    assert len(result.report["members"]) == 4


def test_exposed_prepared_data_cannot_silently_remove_required_members() -> None:
    fixture = example()
    prepared = fixture.prepare()
    prepared.definition["members"].pop()
    try:
        result = composition.evaluate(fixture.source, prepared, fixture.selection, WHEN)
    except ValueError:
        assert fixture.source.calls == []
    else:
        assert set(member_reports(result)) == set(PLATFORMS)
        assert result.report["result"] == "satisfied"


@pytest.mark.parametrize("date", ["2026-10-26", None])
def test_invalid_evaluation_date_is_rejected_before_source_reads(date: Any) -> None:
    fixture = example()
    with pytest.raises(ValueError):
        composition.evaluate(fixture.source, fixture.prepare(), fixture.selection, date)
    assert fixture.source.calls == []


def test_reports_do_not_upgrade_native_trust_claims() -> None:
    fixture = example()
    result = fixture.evaluate()
    for admitted in result.admissions.values():
        assert admitted.receipt["claims"]["test_honesty_verified"] is False
        assert admitted.receipt["claims"]["coverage_verified"] is False
        assert admitted.receipt["claims"]["self_authenticating"] is False
    encoded = json.dumps(result.report)
    assert "native-release" in encoded
    assert all(platform in encoded for platform in PLATFORMS)
