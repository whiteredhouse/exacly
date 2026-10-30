"""JUnit is a bounded projection of native readiness, with selectors independent of mutable evidence."""

import copy
import fnmatch
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import pytest
from exacly import policy, query
from exacly.refresh import case_id, read_junit
from test_applicability import WHEN, B, documents, target
from test_policy import POLICY


def report(docs: dict[str, Any] | None = None, **selection: Any) -> dict[str, Any]:
    return policy.evaluate(query.load(documents() if docs is None else docs, WHEN), POLICY, target(**selection))


def render(value: dict[str, Any], *, namespace: str = "demo", report_id: str = "production") -> ET.Element:
    return ET.fromstring(policy.as_junit(value, namespace=namespace, report_id=report_id))


def properties(element: ET.Element) -> dict[str, str]:
    return {entry.attrib["name"]: entry.attrib["value"] for entry in element.findall("properties/property")}


def test_satisfied_cases_are_standard_junit_and_refresh_readable_without_mutation(tmp_path: Path) -> None:
    value = report()
    before = copy.deepcopy(value)
    xml = policy.as_junit(value, namespace="demo", report_id="production")
    root = ET.fromstring(xml)
    suite = root.find("testsuite")
    assert suite is not None
    counts = {"tests": "2", "failures": "0", "errors": "0", "skipped": "0"}
    assert root.attrib == counts
    assert all(suite.attrib[key] == count for key, count in counts.items())
    cases = list(root.iter("testcase"))
    assert len(cases) == 2
    assert len({(case.attrib["classname"], case.attrib["name"]) for case in cases}) == 2
    assert not root.findall(".//failure") and not root.findall(".//error") and not root.findall(".//skipped")
    expected = {
        "exacly.gate/v1/demo/main-readiness/production/REQ-1/delivery",
        "exacly.gate/v1/demo/main-readiness/production/REQ-1/verification",
    }
    assert {case_id(case) for case in cases} == expected
    path = tmp_path / "gate.xml"
    path.write_text(xml)
    assert read_junit([path]) == dict.fromkeys(expected, "passed")
    assert value == before


def test_unknown_and_known_negative_both_block_with_distinct_native_diagnostics(tmp_path: Path) -> None:
    docs = documents()
    verification = docs["state"]["records"]["REQ-1"]["assessments"][0]
    verification.update(state="failed", reason="Invoice total assertion failed")
    docs["state"]["records"]["REQ-1"]["assessments"] = [verification]
    value = report(docs)
    root = render(value)
    failures = root.findall(".//failure")
    assert root.attrib == {"tests": "2", "failures": "2", "errors": "0", "skipped": "0"}
    assert {failure.attrib["type"] for failure in failures} == {
        "exacly.readiness.unknown",
        "exacly.readiness.not_satisfied",
    }
    assert any("Invoice total assertion failed" in (failure.text or "") for failure in failures)
    assert not root.findall(".//skipped")
    for case in root.iter("testcase"):
        native = next(check for check in value["checks"] if check["dimension"] == properties(case)["exacly.dimension"])
        assert properties(case)["exacly.readiness.result"] == native["result"]
        assert case.find("failure").text == native["reason"]  # type: ignore[union-attr]
    path = tmp_path / "gate.xml"
    path.write_bytes(ET.tostring(root))
    assert set(read_junit([path]).values()) == {"failed"}


def test_nonconformance_retains_blocking_cases_and_bounded_validation_findings() -> None:
    docs = documents()
    docs["config"]["relationships"] = [item for item in docs["config"]["relationships"] if item["type"] != "implements"]
    value = report(docs)
    assert value["result"] == "unknown" and value["findings"]["errors"]
    root = render(value)
    diagnostics = root.findtext("testsuite/system-out")
    assert diagnostics and value["findings"]["errors"][0]["message"] in diagnostics
    assert len(diagnostics.encode()) <= policy.JUNIT_MAX_DIAGNOSTIC_BYTES
    assert all(case.find("failure") is not None for case in root.iter("testcase"))


def test_selectors_stay_stable_when_revisions_dates_versions_and_results_change() -> None:
    first, second = report(), report(revision=B)
    second["as_of"] = "2026-10-30"
    second["policy"]["version"] = "another-version"
    a, b = render(first), render(second)
    assert {case_id(case) for case in a.iter("testcase")} == {case_id(case) for case in b.iter("testcase")}
    props = properties(b.find("testsuite"))  # type: ignore[arg-type]
    assert props["exacly.policy.version"] == "another-version"
    assert props["exacly.as_of"] == "2026-10-30"
    assert props["exacly.target.revision"] == B
    assert props["exacly.target.digest"] != properties(a.find("testsuite"))["exacly.target.digest"]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("first", "second"),
    [("a/b", "a%2Fb"), ("a:b", "a/b"), ("\x00", "\ufffd"), ("\ud800", "\ufffd"), ("é", "e\u0301")],
)
def test_original_identity_components_never_collide(first: str, second: str) -> None:
    value = report()
    a = render(value, namespace=first)
    b = render(value, namespace=second)
    assert {case_id(case) for case in a.iter("testcase")}.isdisjoint(case_id(case) for case in b.iter("testcase"))
    assert {case_id(case) for case in render(value, report_id=first).iter("testcase")}.isdisjoint(
        case_id(case) for case in render(value, report_id=second).iter("testcase")
    )


def test_binding_selectors_encode_globs_separators_and_unicode() -> None:
    value = report()
    value["policy"]["name"] = "release/[v1]?*%"
    for check in value["checks"]:
        check["record"] = "REQ/[*?]é"
    root = render(value, namespace="billing/ø", report_id="production [eu]")
    identities = [case_id(case) for case in root.iter("testcase")]
    assert len(set(identities)) == 2
    assert all(not any(char in identity for char in "*?[]") for identity in identities)
    assert all(fnmatch.fnmatchcase(identity, identity) for identity in identities)
    assert (
        "/billing%2F%C3%B8/release%2F%5Bv1%5D%3F%2A%25/production%20%5Beu%5D/REQ%2F%5B%2A%3F%5D%C3%A9/" in identities[0]
    )


def test_long_valid_selectors_are_not_truncated_by_diagnostic_limits() -> None:
    value = report()
    part = "é" * 500
    value["policy"]["name"] = part
    root = render(value, namespace=part, report_id=part)
    identities = [case_id(case) for case in root.iter("testcase")]
    prefix = f"exacly.gate/v1/{quote(part, safe='')}/{quote(part, safe='')}/{quote(part, safe='')}"
    assert all(len(identity) > policy.JUNIT_MAX_DIAGNOSTIC_BYTES for identity in identities)
    assert set(identities) == {f"{prefix}/REQ-1/delivery", f"{prefix}/REQ-1/verification"}


def test_xml_escaping_invalid_characters_and_long_diagnostics_are_safe() -> None:
    value = report(revision=B)
    for check in value["checks"]:
        check["record"] = 'REQ<&"\x00\ud800'
        check["reason"] = 'bad <value> & "quoted"\x00\ud800 ' + "é" * 10000
    xml = policy.as_junit(value, namespace="demo\x01", report_id="production")
    root = ET.fromstring(xml)
    assert "\x00" not in xml and "\x01" not in xml and "\ud800" not in xml
    assert "&lt;value&gt;" in xml and "&amp;" in xml
    for failure in root.iter("failure"):
        assert len((failure.text or "").encode()) <= policy.JUNIT_MAX_DIAGNOSTIC_BYTES
        assert len(failure.attrib["message"].encode()) <= 512
        assert "[truncated]" in (failure.text or "")
        assert 'bad <value> & "quoted"' in (failure.text or "")


def test_metadata_keeps_identities_but_not_arbitrary_payloads_or_url_secrets() -> None:
    value = report(revision=B)
    value["target"]["builds"] = {"backend": {"credential": "arbitrary-build-secret"}}
    for check in value["checks"]:
        check["reason"] = (
            "See https://user:password@private.test/result?token=secret and https://ci.test/run?token=secret#private"
        )
        check["evidence"] = [
            {"locator": "https://user:password@private.test/run"},
            {"locator": "file:///private/operator.txt"},
            {"locator": "https://ci.test:bad/run"},
            *[{"locator": f"https://ci.test/run/{index}?token=secret#private"} for index in range(10)],
        ]
        check["assessments"] = [{"do_not_export": "arbitrary-assessment-secret"}]
    xml = policy.as_junit(value, namespace="demo", report_id="production")
    root = ET.fromstring(xml)
    for secret in (
        "password",
        "token=",
        "#private",
        "arbitrary-build-secret",
        "arbitrary-assessment-secret",
        "/private/",
    ):
        assert secret not in xml
    suite_props = properties(root.find("testsuite"))  # type: ignore[arg-type]
    assert suite_props["exacly.report.kind"] == "readiness-evaluation"
    assert suite_props["exacly.namespace"] == "demo"
    assert suite_props["exacly.target.model_revision"] == value["target"]["model_revision"]
    assert suite_props["exacly.native_report.digest"].startswith("sha256:")
    assert suite_props["exacly.target.digest"].startswith("sha256:")
    assert not any(key.startswith("exacly.target.builds") for key in suite_props)
    for case in root.iter("testcase"):
        refs = [reference for key, reference in properties(case).items() if key.startswith("exacly.evidence.")]
        assert refs == [f"https://ci.test/run/{index}" for index in range(policy.JUNIT_MAX_REFERENCES)]


def test_delivery_relative_references_are_retained_without_queries_or_unsafe_paths() -> None:
    value = report()
    delivery = next(check for check in value["checks"] if check["dimension"] == "delivery")
    delivery["evidence"] = [
        {"locator": "python/src/service.py?token=secret#private"},
        {"locator": "../private/operator.txt"},
        {"locator": "%2e%2e%2fprivate/operator.txt"},
        {"locator": "/private/operator.txt"},
        {"locator": "//user:password@host/path"},
        {"locator": "https://ci.test/bad\x01path"},
        {"locator": "https://ci.test/bad\x85path"},
        {"locator": "file:///private/operator.txt"},
    ]
    root = render(value)
    case = next(case for case in root.iter("testcase") if properties(case)["exacly.dimension"] == "delivery")
    assert {key: val for key, val in properties(case).items() if key.startswith("exacly.evidence.")} == {
        "exacly.evidence.1": "python/src/service.py"
    }


@pytest.mark.parametrize("excluded_by", ["expired", "wrong_target"])
def test_unknown_cases_locate_excluded_evidence_without_treating_it_as_support(excluded_by: str) -> None:
    docs = documents()
    evidence = docs["state"]["records"]["REQ-1"]["assessments"][0]["evidence"][0]
    evidence["locator"] = "https://ci.test/verification/123?token=secret#private"
    if excluded_by == "expired":
        evidence["expires_at"] = "2026-10-25"
    value = report(docs, **({"revision": B} if excluded_by == "wrong_target" else {}))
    native = next(check for check in value["checks"] if check["dimension"] == "verification")
    assert native["result"] == "unknown" and not native["evidence"] and native["excluded"]
    before = copy.deepcopy(value)
    root = render(value)
    case = next(case for case in root.iter("testcase") if properties(case)["exacly.dimension"] == "verification")
    metadata = properties(case)
    assert metadata["exacly.excluded_evidence.1"] == "https://ci.test/verification/123"
    assert metadata["exacly.excluded_evidence.1.reason"] == native["excluded"][0]["reason"]
    assert not any(key.startswith("exacly.evidence.") for key in metadata)
    assert metadata["exacly.readiness.result"] == "unknown"
    failure = case.find("failure")
    assert failure is not None and failure.attrib["type"] == "exacly.readiness.unknown"
    assert {case_id(case) for case in root.iter("testcase")} == {
        case_id(case) for case in render(report()).iter("testcase")
    }
    assert value == before


def test_excluded_references_and_reasons_have_separate_privacy_and_size_bounds() -> None:
    value = report(revision=B)
    verification = next(check for check in value["checks"] if check["dimension"] == "verification")
    verification["evidence"] = [{"locator": "https://ci.test/applicable"}]
    verification["excluded"] = [
        {
            "reason": "Mismatch https://user:password@private.test/run?token=secret " + "é" * 1000,
            "assessment": {
                "evidence": [
                    {"locator": "https://user:password@private.test/run"},
                    {"locator": "file:///private/operator.txt"},
                    {"locator": "https://ci.test/bad\x00path"},
                    {"locator": "https://ci.test/excluded/0?token=duplicate"},
                    *[{"locator": f"https://ci.test/excluded/{index}?token=secret#private"} for index in range(10)],
                ]
            },
        }
    ]
    root = render(value)
    case = next(case for case in root.iter("testcase") if properties(case)["exacly.dimension"] == "verification")
    metadata = properties(case)
    assert metadata["exacly.evidence.1"] == "https://ci.test/applicable"
    assert {
        key: val
        for key, val in metadata.items()
        if key.startswith("exacly.excluded_evidence.") and not key.endswith(".reason")
    } == {
        f"exacly.excluded_evidence.{index + 1}": f"https://ci.test/excluded/{index}"
        for index in range(policy.JUNIT_MAX_REFERENCES)
    }
    for key, reason in metadata.items():
        if key.startswith("exacly.excluded_evidence.") and key.endswith(".reason"):
            assert len(reason.encode()) <= 512
            assert "[truncated]" in reason
            assert all(secret not in reason for secret in ("password", "token=", "#private"))
    assert all(
        secret not in json.dumps(metadata)
        for secret in ("password", "token=", "#private", "/private/operator.txt", "bad\\u0000path")
    )
    assert metadata["exacly.readiness.result"] == "unknown"


@pytest.mark.parametrize("bad", [None, {}, {"policy": {}, "target": {}}, {"policy": [], "target": {}}])
def test_malformed_or_empty_native_reports_do_not_become_passing_suites(bad: Any) -> None:
    with pytest.raises(ValueError):
        policy.as_junit(bad, namespace="demo", report_id="production")


def test_duplicate_checks_unsupported_results_and_aggregate_mismatch_are_rejected() -> None:
    value = report()
    updates: tuple[dict[str, Any], ...] = (
        {"checks": [value["checks"][0], value["checks"][0]]},
        {"checks": [{**value["checks"][0], "result": "skipped"}]},
        {"checks": [{**value["checks"][0], "result": []}]},
        {"result": "unknown"},
        {"checks": []},
    )
    for update in updates:
        with pytest.raises(ValueError):
            render({**value, **update})


def test_identity_check_count_and_final_byte_limits_reject_instead_of_dropping_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = report()
    with pytest.raises(ValueError, match="identity component"):
        render(value, report_id="x" * (policy.JUNIT_MAX_IDENTITY_BYTES + 1))
    with pytest.raises(ValueError, match="bounded set"):
        render({**value, "checks": [value["checks"][0]] * (policy.JUNIT_MAX_CHECKS + 1)})
    size = len(policy.as_junit(value, namespace="demo", report_id="production").encode())
    monkeypatch.setattr(policy, "JUNIT_MAX_BYTES", size - 1)
    with pytest.raises(ValueError, match="report exceeds"):
        render(value)


def test_native_report_digest_changes_when_referenced_evidence_changes() -> None:
    first = report()
    second = copy.deepcopy(first)
    second["checks"][0]["evidence"][0]["locator"] = "https://ci.test/new"
    a = properties(render(first).find("testsuite"))  # type: ignore[arg-type]
    b = properties(render(second).find("testsuite"))  # type: ignore[arg-type]
    assert a["exacly.native_report.digest"] != b["exacly.native_report.digest"]
    assert a["exacly.target.digest"] == b["exacly.target.digest"]
    assert json.loads(json.dumps(first)) == first
