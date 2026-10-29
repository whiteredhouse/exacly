"""The small fictional demo must stay distinct from Exacly's real recorded project state."""

import html
import importlib
import json
import re
import shutil
import sys
import tarfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from exacly import query
from exacly.cli import load

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    BUILDER = importlib.import_module("ci.build_site")
finally:
    sys.path.pop(0)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    for relative in (
        "spec/exacly",
        "spec/examples/0.2/invoice-reminders",
        "ci/fixtures/invoice-reminders",
        "docs/site",
    ):
        shutil.copytree(ROOT / relative, root / relative)
    return root


def contents(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.urls.extend(value for key, value in attrs if key in {"href", "src"} and value)


def test_build_preserves_canonical_sources_and_packages_the_published_demo(source: Path, tmp_path: Path) -> None:
    before = contents(source)
    out = BUILDER.build(source, tmp_path / "site")
    assert contents(source) == before
    assert (out / ".nojekyll").is_file()
    assert BUILDER.MARKER not in (out / "index.html").read_text()
    assert not (out / "project-model.tar.gz").exists()
    demo = out / "example"
    expected = {
        "invoice-model/" + relative: (demo / relative).read_bytes()
        for relative in ("model.yaml", "billing.yaml", "sample-junit.xml", "DEMO.md", "state/demo.json")
    }
    with tarfile.open(out / "invoice-model.tar.gz") as archive:
        assert set(archive.getnames()) == set(expected)
        for member in archive:
            assert member.isfile()
            stream = archive.extractfile(member)
            assert stream is not None and stream.read() == expected[member.name]
    assert (demo / "sample-junit.xml").read_bytes() == before["ci/fixtures/invoice-reminders/sample-junit.xml"]
    for page in out.rglob("*.html"):
        parser = References()
        text = page.read_text()
        assert str(source) not in text and str(tmp_path / "site") not in text
        parser.feed(text)
        for value in parser.urls:
            url = urlsplit(value)
            if url.scheme or url.netloc or not url.path:
                continue
            assert not url.path.startswith("/"), (page, value)
            path = (page.parent / unquote(url.path)).resolve()
            assert path.is_relative_to(out)
            assert (path / "index.html" if path.is_dir() else path).is_file(), (page, value)


def test_invoice_demo_is_active_verified_and_explicitly_fictional_with_unknown_delivery(
    source: Path, tmp_path: Path
) -> None:
    out = BUILDER.build(source, tmp_path / "site")
    model = query.load(load([out / "example"]))
    shown = query.show(model, "REQ-001")
    assert not model.report.errors
    assert shown["record"]["lifecycle"] == "active"
    observed = shown["state"]["demo"]
    assert observed["status"]["verification"] == "passed"
    assert all(assessment["dimension"] == "verification" for assessment in observed["assessments"])
    assert "delivery" not in observed["status"] or observed["status"]["delivery"] != "released"
    canonical = yaml.safe_load((source / "spec/examples/0.2/invoice-reminders/billing.yaml").read_text())
    assert all(record["lifecycle"] == "proposed" for record in canonical["records"])
    for page in (out / "example").rglob("*.html"):
        assert "demonstration" in page.read_text().lower()
        assert "DEMO.md" in page.read_text()
    page = (out / "index.html").read_text()
    assert "demonstration" in page.lower()
    assert "Not observed" in page
    parser = References()
    parser.feed(page)
    assert "./example/records/REQ-001.html" in parser.urls
    assert all(not url.startswith(("./project", "project/")) for url in parser.urls)
    assert "REQ-013" not in page and "REQ-018" not in page


def test_asset_urls_stay_stable_until_their_content_changes(source: Path, tmp_path: Path) -> None:
    def assets(out: Path) -> dict[str, str]:
        parser = References()
        parser.feed((out / "index.html").read_text())
        found = {}
        for name in ("style.css", "site.js"):
            stem, suffix = name.rsplit(".", 1)
            urls = [url for url in parser.urls if re.fullmatch(rf"\./{stem}\.[0-9a-f]{{12}}\.{suffix}", url)]
            assert len(urls) == 1
            assert "./" + name not in parser.urls
            assert (out / urls[0]).read_bytes() == (source / "docs/site" / name).read_bytes()
            assert (out / name).read_bytes() == (out / urls[0]).read_bytes()
            found[name] = urls[0]
        return found

    before = assets(BUILDER.build(source, tmp_path / "first"))
    assert assets(BUILDER.build(source, tmp_path / "repeated")) == before
    css = source / "docs/site/style.css"
    css.write_text(css.read_text() + "\n/* Changed stylesheet content. */\n")
    after_css = assets(BUILDER.build(source, tmp_path / "changed-css"))
    assert after_css["style.css"] != before["style.css"]
    assert after_css["site.js"] == before["site.js"]
    script = source / "docs/site/site.js"
    script.write_text(script.read_text() + "\n// Changed script content.\n")
    after_script = assets(BUILDER.build(source, tmp_path / "changed-script"))
    assert after_script["style.css"] == after_css["style.css"]
    assert after_script["site.js"] != after_css["site.js"]


def test_demo_summary_preserves_its_observation_identity_and_real_project_browser_keeps_real_state(
    source: Path, tmp_path: Path
) -> None:
    out = BUILDER.build(source, tmp_path / "site")
    page = html.unescape((out / "index.html").read_text())
    records = dict(re.findall(r'<article[^>]+data-record="([^"]+)"[^>]*>(.*?)</article>', page))
    assert set(records) == {"REQ-001"}
    assessment = json.loads((out / "example/state/demo.json").read_text())["records"]["REQ-001"]["assessments"][0]
    record = records["REQ-001"]
    assert assessment["context"] in record and assessment["at"] in record
    for value in assessment["subject"].values():
        rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        assert rendered in record
    for evidence in assessment["evidence"]:
        assert evidence["claim"] in record and evidence["locator"] in record
    assert 'data-dimension="delivery"' not in record
    real_state = json.loads((source / "spec/exacly/state/main.json").read_text())
    real_page = html.unescape((out / "project/records/REQ-013.html").read_text())
    assert real_state["config_revision"][:12] in real_page
    for actual in real_state["records"]["REQ-013"]["assessments"]:
        assert actual["at"] in real_page
        for evidence in actual["evidence"]:
            assert evidence["claim"] in real_page and evidence["locator"] in real_page


def test_demo_verification_is_derived_from_report_results(source: Path, tmp_path: Path) -> None:
    report = source / "ci/fixtures/invoice-reminders/sample-junit.xml"
    tree = ET.parse(report)
    case = tree.find(".//testcase")
    assert case is not None
    ET.SubElement(case, "failure", {"message": "Illustrative failed reminder check"})
    for suite in tree.getroot().iter():
        if suite.tag in {"testsuites", "testsuite"}:
            suite.set("failures", "1")
    tree.write(report, encoding="utf-8", xml_declaration=True)
    out = BUILDER.build(source, tmp_path / "site")
    shown = query.show(query.load(load([out / "example"])), "REQ-001")
    assert shown["state"]["demo"]["status"]["verification"] == "failed"
    assert "Failed" in (out / "index.html").read_text()
    assert all(a["dimension"] != "delivery" for a in shown["state"]["demo"]["assessments"])


def test_demo_proof_escapes_text_and_does_not_link_non_https_evidence(source: Path, tmp_path: Path) -> None:
    out = BUILDER.build(source, tmp_path / "site")
    model = query.load(load([out / "example"]))
    assessment: dict[str, Any] = model.states["demo"]["records"]["REQ-001"]["assessments"][0]
    evidence = assessment["evidence"][0]
    evidence["claim"] = '<script>bad</script> & "claim"'
    evidence["locator"] = "javascript:alert(1)"
    page = BUILDER.demo_evidence(model)
    assert "<script>bad</script>" not in page
    assert "&lt;script&gt;bad&lt;/script&gt; &amp; &quot;claim&quot;" in page
    parser = References()
    parser.feed(page)
    assert all(not value.startswith("javascript:") for value in parser.urls)


def test_build_refuses_to_overwrite_inputs_or_unrelated_output(source: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the source"):
        BUILDER.build(source, source / "docs/site")
    out = tmp_path / "site"
    out.mkdir()
    (out / "keep.txt").write_text("existing file")
    with pytest.raises(ValueError, match="absent or empty"):
        BUILDER.build(source, out)
    assert (out / "keep.txt").read_text() == "existing file"
