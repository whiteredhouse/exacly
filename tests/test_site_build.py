"""The published examples and evidence must come from canonical inputs without changing their identity."""

import html
import importlib.util
import json
import re
import shutil
import tarfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_site", ROOT / "ci/build_site.py")
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    for relative in ("spec/exacly", "spec/examples/0.2/invoice-reminders"):
        shutil.copytree(ROOT / relative, root / relative)
    site = root / "docs/site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><body><!-- PROJECT_EVIDENCE -->"
        '<a href="./example/model.yaml">Boundary</a><a href="./example/billing.yaml">Records</a>'
        '<a href="./example/">Example</a><a href="./project/">Project</a>'
        '<a href="./project-model.tar.gz">Download</a></body></html>'
    )
    (site / ".nojekyll").touch()
    return root


def contents(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.urls.extend(value for key, value in attrs if key in {"href", "src"} and value)


def test_build_keeps_sources_and_downloads_exact_and_generates_both_browsers(source: Path, tmp_path: Path) -> None:
    before = contents(source)
    out = BUILDER.build(source, tmp_path / "site")
    assert contents(source) == before
    assert (out / ".nojekyll").is_file()
    assert BUILDER.MARKER not in (out / "index.html").read_text()
    for name in ("model.yaml", "billing.yaml"):
        assert (out / "example" / name).read_bytes() == before[f"spec/examples/0.2/invoice-reminders/{name}"]
    for relative in ("example/index.html", "example/records/REQ-001.html", "project/records/REQ-013.html"):
        assert (out / relative).is_file()
    for page in out.rglob("*.html"):
        parser = References()
        text = page.read_text()
        assert str(source) not in text
        parser.feed(text)
        for value in parser.urls:
            url = urlsplit(value)
            if url.scheme or url.netloc or not url.path:
                continue
            path = (page.parent / unquote(url.path)).resolve()
            assert path.is_relative_to(out)
            assert (path / "index.html" if path.is_dir() else path).is_file(), (page, value)
    with tarfile.open(out / "project-model.tar.gz") as archive:
        expected = {
            "project-model/" + name.removeprefix("spec/exacly/"): body
            for name, body in before.items()
            if name.startswith("spec/exacly/") and Path(name).suffix in {".yaml", ".yml", ".json"}
        }
        assert set(archive.getnames()) == set(expected)
        for member in archive:
            assert member.isfile()
            stream = archive.extractfile(member)
            assert stream is not None and stream.read() == expected[member.name]


def test_proof_preserves_each_assessments_subject_time_and_evidence(source: Path, tmp_path: Path) -> None:
    state = json.loads((source / "spec/exacly/state/main.json").read_text())
    authored = {
        record["id"]: record
        for document in (source / "spec/exacly").glob("*.yaml")
        for record in yaml.safe_load(document.read_text()).get("records", [])
    }
    page = (BUILDER.build(source, tmp_path / "site") / "index.html").read_text()
    records = dict(re.findall(r'<article[^>]+data-record="([^"]+)"[^>]*>(.*?)</article>', page))
    assert set(records) == {"REQ-013", "REQ-018"}
    assert state["written_at"] in page and state["config_revision"] in page
    for rid, record in records.items():
        observations = re.findall(r'<details[^>]+data-dimension="([^"]+)"[^>]*>(.*?)</details>', record)
        for dimension in BUILDER.DIMENSIONS:
            expected = [a for a in state["records"][rid]["assessments"] if a["dimension"] == dimension]
            details = [detail for found, detail in observations if found == dimension]
            assert len(details) == len(expected)
            for assessment, detail in zip(expected, details, strict=True):
                detail = html.unescape(detail)
                assert assessment["at"] in detail
                assert assessment["context"] in detail
                for value in assessment["subject"].values():
                    rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
                    assert rendered in detail
                for evidence in assessment["evidence"]:
                    assert evidence["claim"] in detail and evidence["locator"] in detail
                    if "observed_at" in evidence:
                        assert evidence["observed_at"] in detail
        assert f"Lifecycle: {authored[rid]['lifecycle']}" in record


def test_missing_delivery_is_unknown_without_inventing_an_assessment(source: Path, tmp_path: Path) -> None:
    path = source / "spec/exacly/state/main.json"
    state = json.loads(path.read_text())
    state["records"]["REQ-018"]["assessments"] = [
        a for a in state["records"]["REQ-018"]["assessments"] if a["dimension"] != "delivery"
    ]
    path.write_text(json.dumps(state))
    page = (BUILDER.build(source, tmp_path / "site") / "index.html").read_text()
    records = dict(re.findall(r'<article[^>]+data-record="([^"]+)"[^>]*>(.*?)</article>', page))
    assert "Recorded delivery: Not observed." in records["REQ-018"]
    assert not any(
        dimension == "delivery"
        for dimension, _ in re.findall(
            r'<details[^>]+data-dimension="([^"]+)"[^>]*>(.*?)</details>', records["REQ-018"]
        )
    )


def test_proof_escapes_text_and_does_not_link_non_https_evidence(source: Path, tmp_path: Path) -> None:
    path = source / "spec/exacly/state/main.json"
    state: dict[str, Any] = json.loads(path.read_text())
    evidence = state["records"]["REQ-013"]["assessments"][0]["evidence"][0]
    evidence["claim"] = '<script>bad</script> & "claim"'
    evidence["locator"] = "javascript:alert(1)"
    path.write_text(json.dumps(state))
    page = (BUILDER.build(source, tmp_path / "site") / "index.html").read_text()
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
