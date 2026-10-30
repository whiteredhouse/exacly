"""Composition JUnit keeps every required verdict and the actual independent native gates."""

from __future__ import annotations

import copy
import fnmatch
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import pytest
from exacly import composition, policy
from exacly.refresh import case_id, read_junit
from test_composition import PLATFORMS, case, example

Doc = dict[str, Any]


@pytest.fixture(scope="module")
def admitted_report() -> Doc:
    return example().evaluate().report


@pytest.fixture(scope="module")
def blocked_report() -> Doc:
    skipped = (
        f'<testsuite><testcase file="checks/test_native.py" name="{case(1).split("::")[1]}">'
        "<skipped/></testcase></testsuite>"
    ).encode()
    fixture = example(outcomes={PLATFORMS[0]: "failure"}, reports={PLATFORMS[1]: skipped})
    fixture.source.unavailable.add("/repos/acme/native/actions/runs/702/attempts/1")
    return fixture.evaluate().report


def render(report: Doc) -> ET.Element:
    return ET.fromstring(composition.as_junit(report))


def properties(element: ET.Element) -> dict[str, str]:
    return {entry.attrib["name"]: entry.attrib["value"] for entry in element.findall("properties/property")}


def verdict_id(report: Doc, identifier: str) -> str:
    return f"exacly.composition/v1/{quote(report['definition']['name'], safe='')}/{identifier}/verdict"


def expected_native(report: Doc) -> dict[str, ET.Element]:
    result: dict[str, ET.Element] = {}
    for member in report["members"]:
        if member["gate"] is not None:
            native = ET.fromstring(
                policy.as_junit(
                    member["gate"],
                    namespace=member["model"]["namespace"],
                    report_id=f"{report['definition']['name']}/{member['id']}",
                )
            )
            result.update((case_id(item), item) for item in native.iter("testcase"))
    return result


def test_standard_junit_preserves_native_checks_and_every_required_verdict_without_mutation(
    admitted_report: Doc, tmp_path: Path
) -> None:
    before = copy.deepcopy(admitted_report)
    xml = composition.as_junit(admitted_report)
    assert xml == composition.as_junit(json.loads(json.dumps(admitted_report, sort_keys=True)))
    root = ET.fromstring(xml)
    cases = {case_id(item): item for item in root.iter("testcase")}
    native = expected_native(admitted_report)
    verdicts = {verdict_id(admitted_report, identifier) for identifier in PLATFORMS}
    assert set(cases) == set(native) | verdicts
    assert root.attrib == {"tests": "8", "failures": "0", "errors": "0", "skipped": "0"}
    assert len({(item.attrib["classname"], item.attrib["name"]) for item in cases.values()}) == 8
    assert not root.findall(".//failure") and not root.findall(".//skipped")
    for identifier, native_case in native.items():
        assert cases[identifier].attrib == native_case.attrib
        assert properties(cases[identifier]) == properties(native_case)
    for identifier in PLATFORMS:
        metadata = properties(cases[verdict_id(admitted_report, identifier)])
        assert metadata["exacly.composition.member"] == identifier
        assert metadata["exacly.composition.status"] == "satisfied"
        assert metadata["exacly.admission.status"] == "admitted"
    path = tmp_path / "composition.xml"
    path.write_text(xml)
    assert read_junit([path]) == dict.fromkeys(cases, "passed")
    assert admitted_report == before


def test_mixed_negative_unknown_and_rejected_members_all_block_without_inventing_gate_cases(
    blocked_report: Doc, tmp_path: Path
) -> None:
    xml = composition.as_junit(blocked_report)
    root = ET.fromstring(xml)
    cases = {case_id(item): item for item in root.iter("testcase")}
    native = expected_native(blocked_report)
    assert root.attrib == {"tests": "7", "failures": "5", "errors": "0", "skipped": "0"}
    assert set(cases) == set(native) | {verdict_id(blocked_report, identifier) for identifier in PLATFORMS}
    assert not root.findall(".//skipped")
    failure_types = {item.attrib["type"] for item in root.iter("failure")}
    assert {"exacly.readiness.unknown", "exacly.readiness.not_satisfied"} <= failure_types
    for member in blocked_report["members"]:
        verdict = cases[verdict_id(blocked_report, member["id"])]
        assert (verdict.find("failure") is not None) == (member["status"] == "blocked")
        assert properties(verdict)["exacly.admission.status"] == member["admission"]["status"]
    rejected = blocked_report["members"][2]
    assert rejected["admission"]["status"] == "rejected" and rejected["gate"] is None
    assert not any(quote(f"native-release/{PLATFORMS[2]}", safe="") in identifier for identifier in native)
    path = tmp_path / "composition.xml"
    path.write_text(xml)
    assert list(read_junit([path]).values()).count("failed") == 5


def test_rejected_member_requires_an_explicit_null_gate(blocked_report: Doc) -> None:
    report = copy.deepcopy(blocked_report)
    report["members"][2].pop("gate")
    with pytest.raises(ValueError, match="explicit native gate"):
        composition.as_junit(report)


def test_actual_gate_serializer_is_reused_for_each_admitted_member(
    admitted_report: Doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = policy.as_junit
    calls: list[tuple[Doc, str, str]] = []

    def spy(report: Doc, *, namespace: str, report_id: str) -> str:
        calls.append((report, namespace, report_id))
        return original(report, namespace=namespace, report_id=report_id)

    monkeypatch.setattr(policy, "as_junit", spy)
    render(admitted_report)
    assert calls == [
        (member["gate"], member["model"]["namespace"], f"native-release/{member['id']}")
        for member in admitted_report["members"]
    ]


def test_selection_input_order_does_not_make_a_valid_completed_report_unrenderable() -> None:
    fixture = example()
    fixture.selection["members"].reverse()
    report = fixture.evaluate().report
    assert [member["id"] for member in report["members"]] == list(PLATFORMS)
    assert render(report).attrib["failures"] == "0"


def test_selectors_ignore_mutable_versions_dates_revisions_and_digests(admitted_report: Doc) -> None:
    changed = copy.deepcopy(admitted_report)
    changed["as_of"] = "2026-10-30"
    changed["definition"].update(version="next", digest="sha256:" + "1" * 64)
    changed["definition_digest"] = changed["definition"]["digest"]
    changed["selection_digest"] = "sha256:" + "2" * 64
    for member in changed["members"]:
        member["trust_policy"].update(version="next", digest="sha256:" + "3" * 64)
        member["readiness_policy"].update(version="next", digest="sha256:" + "4" * 64)
        member["model"].update(revision="c" * 40, digest="sha256:" + "5" * 64)
        member["selection"]["target"].update(model_revision="c" * 40, revision="d" * 40)
        member["admission"].update(
            policy=copy.deepcopy(member["trust_policy"]),
            target=copy.deepcopy(member["selection"]["target"]),
            as_of=changed["as_of"],
            digest="sha256:" + "6" * 64,
        )
        member["admission"]["origin"]["workflow_revision"] = "d" * 40
        member["gate"].update(
            policy={"name": member["readiness_policy"]["name"], "version": "next"},
            target=copy.deepcopy(member["selection"]["target"]),
            as_of=changed["as_of"],
        )
    first, second = render(admitted_report), render(changed)
    assert {case_id(item) for item in first.iter("testcase")} == {case_id(item) for item in second.iter("testcase")}
    assert composition.as_junit(admitted_report) != composition.as_junit(changed)
    assert any(properties(suite).get("exacly.policy.version") == "next" for suite in second.findall("testsuite"))


@pytest.mark.parametrize(
    ("first", "second"),
    [("a/b", "a%2Fb"), ("a:b", "a/b"), ("\x00", "\ufffd"), ("\ud800", "\ufffd"), ("é", "e\u0301")],
)
def test_original_composition_slot_identities_never_collide(admitted_report: Doc, first: str, second: str) -> None:
    a, b = copy.deepcopy(admitted_report), copy.deepcopy(admitted_report)
    a["definition"]["name"], b["definition"]["name"] = first, second
    assert {case_id(item) for item in render(a).iter("testcase")}.isdisjoint(
        case_id(item) for item in render(b).iter("testcase")
    )


def test_globs_separators_and_unicode_are_literal_in_all_binding_selectors(admitted_report: Doc) -> None:
    report = copy.deepcopy(admitted_report)
    report["definition"]["name"] = "release/[v1]?*%é"
    identities = {case_id(item) for item in render(report).iter("testcase")}
    assert len(identities) == 8
    assert all(not any(char in identifier for char in "*?[]") for identifier in identities)
    assert all(fnmatch.fnmatchcase(identifier, identifier) for identifier in identities)
    assert verdict_id(report, PLATFORMS[0]) in identities


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "complete-missing",
        "wrong-operation",
        "wrong-version",
        "required-empty",
        "required-duplicate",
        "required-extra",
        "missing-member",
        "duplicate-member",
        "extra-member",
        "aggregate-decision",
        "member-decision",
        "admitted-no-gate",
        "rejected-with-gate",
        "selection-id",
        "selection-target",
        "model-revision",
        "gate-policy",
        "trust-policy",
        "gate-target",
        "receipt-target",
        "gate-date",
        "receipt-date",
        "receipt-run",
        "receipt-attempt",
        "receipt-repository",
        "receipt-workflow-revision",
        "definition-digest",
        "selection-digest",
        "evidence-layout",
    ],
)
def test_incomplete_or_inconsistent_reports_are_rejected(admitted_report: Doc, mutation: str) -> None:
    report = copy.deepcopy(admitted_report)
    member = report["members"][0]
    if mutation == "incomplete":
        report["complete"] = False
    elif mutation == "complete-missing":
        report.pop("complete")
    elif mutation == "wrong-operation":
        report["operation"] = "validate-composition-definition"
    elif mutation == "wrong-version":
        report["schema_version"] = "999.0.0"
    elif mutation == "required-empty":
        report["required_members"] = []
    elif mutation == "required-duplicate":
        report["required_members"].append(PLATFORMS[0])
    elif mutation == "required-extra":
        report["required_members"].append("missing")
    elif mutation == "missing-member":
        report["members"].pop()
    elif mutation == "duplicate-member":
        report["members"].append(copy.deepcopy(member))
    elif mutation == "extra-member":
        report["members"].append({**copy.deepcopy(member), "id": "foreign"})
    elif mutation == "aggregate-decision":
        report["result"] = "blocked"
    elif mutation == "member-decision":
        member["status"] = "blocked"
    elif mutation == "admitted-no-gate":
        member["gate"] = None
    elif mutation == "rejected-with-gate":
        member["admission"]["status"] = "rejected"
    elif mutation == "selection-id":
        member["selection"]["id"] = "foreign"
    elif mutation == "selection-target":
        member["selection"]["target"]["revision"] = "e" * 40
    elif mutation == "model-revision":
        member["model"]["revision"] = "e" * 40
    elif mutation == "gate-policy":
        member["gate"]["policy"]["name"] = "foreign"
    elif mutation == "trust-policy":
        member["admission"]["policy"]["name"] = "foreign"
    elif mutation == "gate-target":
        member["gate"]["target"]["revision"] = "e" * 40
    elif mutation == "receipt-target":
        member["admission"]["target"]["revision"] = "e" * 40
    elif mutation == "gate-date":
        member["gate"]["as_of"] = "2026-01-01"
    elif mutation == "receipt-date":
        member["admission"]["as_of"] = "2026-01-01"
    elif mutation == "receipt-run":
        member["admission"]["origin"]["run_id"] += 1
    elif mutation == "receipt-attempt":
        member["admission"]["origin"]["run_attempt"] += 1
    elif mutation == "receipt-repository":
        member["admission"]["origin"]["repository"] = "foreign/repo"
    elif mutation == "receipt-workflow-revision":
        member["admission"]["origin"]["workflow_revision"] = "e" * 40
    elif mutation == "definition-digest":
        report["definition_digest"] = "sha256:" + "f" * 64
    elif mutation == "selection-digest":
        report["selection_digest"] = "not-a-content-identity"
    else:
        member["evidence"]["receipt"] = "../private/receipt.json"
    with pytest.raises(ValueError):
        composition.as_junit(report)


@pytest.mark.parametrize("malformed", [None, [], {}, {"complete": True}])
def test_malformed_reports_raise_value_error(malformed: Any) -> None:
    with pytest.raises(ValueError):
        composition.as_junit(malformed)


def test_unsafe_diagnostics_are_sanitized_and_private_receipt_payloads_not_exported(blocked_report: Doc) -> None:
    report = copy.deepcopy(blocked_report)
    rejected = report["members"][2]
    rejected["reason"] = 'Rejected <input> & "value"\x00\ud800 ' + "é" * 10000
    rejected["admission"]["checks"][-1]["reason"] = (
        "See https://user:password@private.test/run?token=secret#fragment and https://ci.test/run?token=secret#fragment"
    )
    rejected["admission"]["private_credentials"] = "receipt-private-secret"
    xml = composition.as_junit(report)
    root = ET.fromstring(xml)
    for secret in ("\x00", "\ud800", "password", "token=", "#fragment", "receipt-private-secret"):
        assert secret not in xml
    for failure in root.iter("failure"):
        assert len((failure.text or "").encode()) <= policy.JUNIT_MAX_DIAGNOSTIC_BYTES
        assert len(failure.attrib["message"].encode()) <= 512
    verdict = next(item for item in root.iter("testcase") if case_id(item) == verdict_id(report, PLATFORMS[2]))
    assert verdict.find("failure") is not None
    assert "&lt;input&gt;" in xml and "&amp;" in xml
    assert "[truncated]" in (verdict.findtext("failure") or "")


def test_aggregate_native_prerequisite_and_output_size_limits_are_enforced(
    admitted_report: Doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verdicts remain mandatory without consuming the native prerequisite budget.
    monkeypatch.setattr(composition, "JUNIT_MAX_CHECKS", 4)
    assert render(admitted_report).attrib["tests"] == "8"
    monkeypatch.setattr(composition, "JUNIT_MAX_CHECKS", 3)
    with pytest.raises(ValueError):
        composition.as_junit(admitted_report)
    monkeypatch.setattr(composition, "JUNIT_MAX_CHECKS", policy.JUNIT_MAX_CHECKS)
    monkeypatch.setattr(composition, "JUNIT_MAX_BYTES", 128)
    with pytest.raises(ValueError):
        composition.as_junit(admitted_report)


@pytest.mark.parametrize("malformed", ["aggregate", "duplicate", "identity"])
def test_native_gate_inconsistencies_keep_the_native_serializer_validation(
    admitted_report: Doc, malformed: str
) -> None:
    report = copy.deepcopy(admitted_report)
    gate = report["members"][0]["gate"]
    if malformed == "aggregate":
        gate["checks"][0]["result"] = "unknown"
    elif malformed == "duplicate":
        gate["checks"].append(copy.deepcopy(gate["checks"][0]))
    else:
        report["members"][0]["model"]["namespace"] = "n" * (policy.JUNIT_MAX_IDENTITY_BYTES + 1)
    with pytest.raises(ValueError):
        composition.as_junit(report)


@pytest.mark.parametrize(
    "mutation",
    [
        "admitted-false",
        "admitted-truncated",
        "admitted-stage-order",
        "admitted-nonboolean",
        "rejected-all-passed",
        "rejected-bad-stage",
        "rejected-previous-false",
    ],
)
def test_native_admission_prefix_and_terminal_outcome_must_be_consistent(
    admitted_report: Doc, blocked_report: Doc, mutation: str
) -> None:
    report = copy.deepcopy(blocked_report if mutation.startswith("rejected") else admitted_report)
    member = report["members"][2 if mutation.startswith("rejected") else 0]
    checks = member["admission"]["checks"]
    if mutation == "admitted-false":
        checks[0]["passed"] = False
    elif mutation == "admitted-truncated":
        checks.pop()
    elif mutation == "admitted-stage-order":
        checks[0], checks[1] = checks[1], checks[0]
    elif mutation == "admitted-nonboolean":
        checks[0]["passed"] = 1
    elif mutation == "rejected-all-passed":
        checks[-1]["passed"] = True
    elif mutation == "rejected-bad-stage":
        checks[-1]["code"] = "unrecognized"
    else:
        assert len(checks) > 1
        checks[0]["passed"] = False
    with pytest.raises(ValueError):
        composition.as_junit(report)
