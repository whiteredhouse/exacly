"""Checks the manual-qa extension: observations people commit and a target manifest in, per-platform verification
out, as in issue #19's acceptance cases.

Run with `mise run test`, or from the repository root: uv run pytest python/tests
"""

import contextlib
import hashlib
import io
import json
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from exacly import manual_qa, query, validate_documents
from exacly.cli import exacly_main, load
from exacly.refresh import RefreshError, refresh
from test_refresh import RUN, WHEN, config, requirement, valid_state

REVISION = "4f1c2e9"
AT = "2026-10-26T09:00:00Z"
CRITERIA = [
    "A photo taken in airplane mode uploads within one minute of turning airplane mode off.",
    "A photo is uploaded once, even if the connection drops during the upload.",
    "Videos taken offline upload the same way.",
]
BINDING = {"path": "qa/observations/*.yaml", "platforms": ["ios", "android"]}
APP = {"version": "1.4.0", "digest": "sha256:9b2d"}
BACKEND = {"revision": "b7f3e21"}
TARGET = {
    "context": "production",
    "model_revision": REVISION,
    "release": "1.4",
    "builds": {"app": APP, "backend": BACKEND},
    "due": {"REQ-1": [1, 2]},
}


def manual_requirement(tests: dict[str, Any] | None = None) -> dict[str, Any]:
    record = requirement("REQ-1", tests)
    record["acceptance_criteria"] = CRITERIA
    record["verification_method"] = "Manual run on an iPhone and an Android phone with release builds."
    record["observe"] = {**record.get("observe", {}), manual_qa.SOURCE: BINDING}
    return record


def documents(record: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = config(record or manual_requirement())
    doc["model"]["extensions"] = [manual_qa.EXTENSION]
    return {"config": doc}


@pytest.fixture
def root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "qa" / "observations").mkdir(parents=True)
        yield Path(tmp)


def observation(
    root: Path,
    name: str,
    platform: str | None,
    results: dict[int, Any],
    at: str = "2026-10-25T14:12:00Z",
    **fields: Any,
) -> dict[str, Any]:
    """Write an observation and its retained output under qa/observations; plain results are passed or failed."""
    log = root / "qa" / "observations" / f"{name}.log"
    log.write_text(f"console output of {name}\n")
    doc: dict[str, Any] = {
        "observed_by": "jane-tester",
        "observed_at": at,
        "record": "REQ-1",
        "release": "1.4",
        "model_revision": REVISION,
        **({"platform": platform} if platform else {}),
        "device": "iPhone 15, iOS 26.1" if platform == "ios" else "Pixel 9, Android 16",
        "builds": {"app": APP, "backend": BACKEND},
        "results": {
            n: r if isinstance(r, dict) else {"result": r, **({"reason": f"{r} on {name}"} if r != "passed" else {})}
            for n, r in results.items()
        },
        "output": {
            "file": f"qa/observations/{name}.log",
            "digest": "sha256:" + hashlib.sha256(log.read_bytes()).hexdigest(),
        },
    }
    doc.update(fields)
    (root / "qa" / "observations" / f"{name}.yaml").write_text(yaml.safe_dump(doc))
    return doc


def observe(
    root: Path, target: dict[str, Any] | None = None, docs: dict[str, Any] | None = None, **kwargs: Any
) -> tuple[dict[str, Any], list[str]]:
    state, notes = manual_qa.observe(docs or documents(), target or TARGET, REVISION, AT, root=root, **kwargs)
    return valid_state(state), notes


def by_platform(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["x-platform"]: a for a in state["records"]["REQ-1"]["assessments"] if a.get("source") == manual_qa.SOURCE}


def status(state: dict[str, Any], docs: dict[str, Any] | None = None) -> dict[str, Any]:
    return query.load({**(docs or documents()), "state": state}, WHEN.date(), REVISION).status("REQ-1")


def test_a_failure_on_one_platform_keeps_the_record_unverified(root: Path) -> None:  # Case 1
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    observation(root, "android", "android", {1: "passed", 2: "failed"})
    state, notes = observe(root)
    assert notes == []
    platforms = by_platform(state)
    assert platforms["ios"]["state"] == "inconclusive" and platforms["ios"]["x-release-scope"] == "passed"
    assert platforms["android"]["state"] == "failed"
    assert platforms["android"]["reason"] == "Criterion 2 failed on android: failed on android"
    assert platforms["android"]["x-criteria"] == {"1": "passed", "2": "failed"}
    assert status(state) == {"production": {"verification": "failed"}}
    model = query.load({**documents(), "state": state}, WHEN.date(), REVISION)
    assert [a["x-platform"] for a in query.show(model, "REQ-1")["state"]["production"]["assessments"]] == [
        "ios",
        "android",
    ]
    assert [r["id"] for r in query.gaps(model)["coverage"]["unverified"]] == ["REQ-1"]
    report = validate_documents({**documents(), "state": state}, WHEN.date(), REVISION).as_dict()
    assert report["errors"] == []


@pytest.mark.parametrize(
    ("builds", "named"),
    [
        ({"app": {**APP, "version": "1.4.1"}, "backend": BACKEND}, "app 1.4.1, backend b7f3e21"),  # Case 2
        ({"app": APP, "backend": {"revision": "c0ffee1"}}, "app 1.4.0, backend c0ffee1"),  # Case 3
    ],
)
def test_new_builds_leave_each_platform_not_checked(root: Path, builds: dict[str, Any], named: str) -> None:
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    earlier, _ = observe(root)
    state, notes = observe(root, {**TARGET, "builds": builds}, previous=earlier)
    assert notes == []
    for platform, a in by_platform(state).items():
        assert a["state"] == "not_checked" and a["evidence"] == []
        assert a["reason"] == f"No observation on {platform} for release 1.4 with {named}."
    assert state["serial"] == earlier["serial"] + 1


def test_a_later_run_that_leaves_a_failed_criterion_out_keeps_the_failure(root: Path) -> None:  # Case 4
    observation(root, "android-1", "android", {1: "passed", 2: "failed"}, at="2026-10-25T10:00:00Z")
    observation(root, "android-2", "android", {1: "passed"}, at="2026-10-25T16:00:00Z")
    android = by_platform(observe(root)[0])["android"]
    assert android["state"] == "failed" and android["x-criteria"] == {"1": "passed", "2": "failed"}
    assert [e["locator"] for e in android["evidence"]] == [
        "qa/observations/android-1.log",
        "qa/observations/android-2.log",
    ]


def test_the_later_of_two_runs_on_one_day_wins(root: Path) -> None:  # Case 5
    observation(root, "android-1", "android", {1: "passed", 2: "failed"}, at="2026-10-25T10:00:00Z")
    observation(root, "android-2", "android", {2: "passed"}, at="2026-10-25T16:00:00Z")
    android = by_platform(observe(root)[0])["android"]
    assert android["x-criteria"] == {"1": "passed", "2": "passed"}
    assert android["state"] == "inconclusive"


def test_two_results_for_one_criterion_at_one_time_are_an_error(root: Path) -> None:  # Case 6
    observation(root, "android-1", "android", {2: "failed"})
    observation(root, "android-2", "android", {2: "passed"})
    with pytest.raises(
        RefreshError, match=re.escape("android-1.yaml and qa/observations/android-2.yaml both give criterion 2")
    ):
        observe(root)


def test_a_passed_release_scope_is_inconclusive_until_every_criterion_passed(root: Path) -> None:  # Case 7
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    observation(root, "android", "android", {1: "passed", 2: "passed"})
    state, _ = observe(root)
    for platform, a in by_platform(state).items():
        assert a["state"] == "inconclusive" and a["x-release-scope"] == "passed"
        assert a["reason"] == f"Criteria 1 and 2 passed for release 1.4 on {platform}; criterion 3 not covered."
    assert status(state) == {"production": {"verification": "inconclusive"}}
    model = query.load({**documents(), "state": state}, WHEN.date(), REVISION)
    gaps = query.gaps(model)
    assert [r["id"] for r in gaps["coverage"]["unverified"]] == ["REQ-1"]
    assert [o["message"] for o in gaps["observers"]] == [
        "release 1.4 scope passed on ios, but the record is not verified",
        "release 1.4 scope passed on android, but the record is not verified",
    ]


def test_a_new_release_needs_its_own_observations(root: Path) -> None:  # Case 8
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    release = {**TARGET, "release": "1.5", "due": {"REQ-1": [1, 2, 3]}}
    platforms = by_platform(observe(root, release)[0])
    assert {p: a["state"] for p, a in platforms.items()} == {"ios": "not_checked", "android": "not_checked"}
    for platform in ("ios", "android"):
        observation(root, f"{platform}-1.5", platform, {1: "passed", 2: "passed", 3: "passed"}, release="1.5")
    platforms = by_platform(observe(root, release)[0])
    assert {p: a["state"] for p, a in platforms.items()} == {"ios": "passed", "android": "passed"}
    observation(root, "android-1.5b", "android", {3: "passed"}, at="2026-10-25T18:00:00Z", release="1.5")
    observation(root, "ios-1.5b", "ios", {1: "passed", 2: "passed"}, at="2026-10-25T18:00:00Z", release="1.5")
    assert all(a["state"] == "passed" for a in by_platform(observe(root, release)[0]).values())


def test_an_observation_that_leaves_out_a_target_build_is_not_used(root: Path) -> None:  # Case 9
    observation(root, "ios", "ios", {1: "passed", 2: "passed"}, builds={"app": APP})
    state, notes = observe(root)
    assert notes == [
        "REQ-1: qa/observations/ios.yaml is not used: names the builds app, but the target assesses app, backend"
    ]
    ios = by_platform(state)["ios"]
    assert ios["state"] == "not_checked" and ios["x-unused"][0]["file"] == "qa/observations/ios.yaml"
    assert "x-unused" not in by_platform(state)["android"]


@pytest.mark.parametrize(
    ("criteria", "used"),
    [(None, False), ({"REQ-1": ["An older first criterion.", *CRITERIA[1:]]}, False), ({"REQ-1": CRITERIA}, True)],
)
def test_an_observation_at_another_revision_counts_only_when_the_criteria_are_unchanged(
    root: Path, criteria: dict[str, list[str]] | None, used: bool
) -> None:  # Case 10
    observation(root, "ios", "ios", {1: "passed", 2: "passed"}, model_revision="1a2b3c4")
    state, notes = observe(root, criteria_at=lambda revision: criteria if revision == "1a2b3c4" else None)
    assert (by_platform(state)["ios"]["state"] == "inconclusive") is used
    expected = "REQ-1: qa/observations/ios.yaml is not used: stale: REQ-1's criteria at 1a2b3c4 differ from the current"
    assert notes == ([] if used else [expected + " ones"])


@pytest.mark.parametrize(("change", "why"), [("delete", "is missing"), ("edit", "doesn't match its digest")])
def test_an_observation_without_its_retained_output_is_not_used(root: Path, change: str, why: str) -> None:  # 11
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    log = root / "qa" / "observations" / "ios.log"
    log.unlink() if change == "delete" else log.write_text("edited\n")
    state, notes = observe(root)
    assert notes == [f"REQ-1: qa/observations/ios.yaml is not used: unusable: its output qa/observations/ios.log {why}"]
    assert by_platform(state)["ios"]["state"] == "not_checked"


def test_a_failure_without_a_reason_is_rejected(root: Path) -> None:  # Case 12
    observation(root, "android", "android", {1: "passed", 2: {"result": "failed"}})
    state, notes = observe(root)
    assert len(notes) == 1 and "android.yaml is not used: not a valid observation: /results/2: 'reason'" in notes[0]
    assert by_platform(state)["android"]["state"] == "not_checked"


def test_tests_and_manual_observers_keep_each_others_results(root: Path) -> None:  # Case 13
    docs = documents(manual_requirement({"workflow": RUN.workflow, "job": RUN.job}))
    observation(root, "ios", "ios", {1: "passed", 2: "failed"})
    run = RUN.__class__(**{**RUN.__dict__, "context": "production", "revision": REVISION})
    tested = refresh(docs, {}, run)
    manual, _ = observe(root, docs=docs, previous=tested)
    sources = [a["source"] for a in manual["records"]["REQ-1"]["assessments"]]
    assert sources == ["tests", manual_qa.SOURCE, manual_qa.SOURCE]
    again = valid_state(refresh(docs, {}, run, previous=manual))
    assert [a["source"] for a in again["records"]["REQ-1"]["assessments"]] == [
        manual_qa.SOURCE,
        manual_qa.SOURCE,
        "tests",
    ]
    assert again["records"]["REQ-1"]["assessments"][:2] == manual["records"]["REQ-1"]["assessments"][1:]


def test_observations_beside_the_model_folder_are_not_read_as_config(root: Path) -> None:  # Case 14
    observation(root, "ios", "ios", {1: "passed"})
    (root / "qa" / "targets").mkdir()
    (root / "qa" / "targets" / "production.yaml").write_text(yaml.safe_dump(TARGET))
    folder = root / "spec" / "exacly"
    folder.mkdir(parents=True)
    (folder / "model.yaml").write_text(yaml.safe_dump(documents()["config"]))
    assert list(load([folder])) == [str(folder / "model.yaml")]


def test_the_cli_writes_state_and_names_unused_observations(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    observation(root, "android", "android", {1: "passed"}, builds={"app": APP})
    (root / "qa" / "targets").mkdir()
    (root / "qa" / "targets" / "production.yaml").write_text(yaml.safe_dump(TARGET))
    (root / "model.json").write_text(json.dumps(documents()["config"]))
    monkeypatch.chdir(root)
    out, err = io.StringIO(), io.StringIO()
    args = ["observe", "manual-qa", "model.json", "--target", "qa/targets/production.yaml"]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        assert exacly_main([*args, "--state", "state/production.json", "--revision", REVISION]) == 0
    assert out.getvalue().startswith(f"Wrote state/production.json: production serial 1 at {REVISION}: ")
    assert "manual-qa:observations 1 inconclusive, 1 not_checked" in out.getvalue()
    assert "qa/observations/android.yaml is not used: names the builds app" in err.getvalue()
    state = valid_state(json.loads((root / "state" / "production.json").read_text()))
    assert by_platform(state)["ios"]["evidence"][0]["applies_to"] == "production"
    with contextlib.redirect_stdout(io.StringIO()) as gaps_out:
        assert exacly_main(["gaps", "-m", "model.json", "-m", "state/production.json"]) == 0
    assert "From extension observers (2)" in gaps_out.getvalue()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({**TARGET, "due": {"REQ-9": [1]}}, "REQ-9, which is not a record"),
        ({**TARGET, "due": {"REQ-1": [4]}}, "criterion 4 for REQ-1 are beyond its 3"),
        ({**TARGET, "builds": {}}, "the target manifest is not valid: /builds"),
        ({**TARGET, "model_revision": "1a2b3c4"}, "numbered at 1a2b3c4, where its criteria differ"),
    ],
)
def test_a_target_that_does_not_fit_the_model_is_refused(root: Path, target: dict[str, Any], message: str) -> None:
    with pytest.raises(RefreshError, match=message):
        observe(root, target)


def test_a_record_whose_binding_left_loses_its_manual_results(root: Path) -> None:
    observation(root, "ios", "ios", {1: "passed", 2: "passed"})
    earlier, _ = observe(root)
    record = manual_requirement({"workflow": RUN.workflow, "job": RUN.job})
    del record["observe"][manual_qa.SOURCE]
    state, _ = observe(root, docs=documents(record), previous=earlier)
    assert "REQ-1" not in state["records"]


def test_without_platforms_a_binding_gets_one_assessment(root: Path) -> None:
    record = manual_requirement()
    record["observe"][manual_qa.SOURCE] = {"path": "qa/observations/*.yaml"}
    observation(root, "run", None, {1: "passed", 2: "passed", 3: "passed"})
    state, _ = observe(root, docs=documents(record))
    [a] = state["records"]["REQ-1"]["assessments"]
    assert a["state"] == "passed" and "x-platform" not in a and "binding" not in a


def test_a_declared_extension_checks_its_bindings() -> None:
    record = manual_requirement()
    record["observe"][manual_qa.SOURCE] = {"path": "qa/*.yaml", "platform": "ios"}
    report = validate_documents(documents(record), WHEN.date()).as_dict()
    assert [(e["rule"], e["record"]) for e in report["errors"]] == [("11.extension-binding", "REQ-1")]
    undeclared = documents(record)
    del undeclared["config"]["model"]["extensions"]
    assert validate_documents(undeclared, WHEN.date()).as_dict()["errors"] == []
