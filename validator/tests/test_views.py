"""Checks the section 10 views: the pull request plan and the site, over the invoice reminders example.

Run with `mise run test`, or from the repository root: uv run pytest validator/tests
"""

import contextlib
import copy
import io
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest
from exacly_validate import query, views
from exacly_validate.cli import exacly_main
from test_query import STATE, TestChanges, documents, model


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.links += [value for name, value in attrs if tag == "a" and name == "href" and value]


def links(page: str) -> list[str]:
    parser = Links()
    parser.feed(page)
    return parser.links


class TestPlan:
    def test_changes_affected_records_and_what_to_check(self) -> None:
        change = query.change_impact(model(), TestChanges().edited())
        text = views.plan(change, query.gaps(model()), "main")
        assert text.startswith(views.MARKER + "\n### Exacly plan\n")
        assert "Against `main`: 1 changed, 1 removed, 1 relationship added, changed or removed." in text
        assert "| ~ | `INF-001` | Infrastructure |" in text and "`description`" in text
        assert "| - | `INF-002` | Infrastructure |" in text
        assert "| - | | relationship | `APP-001 --depends_on--> INF-002` | |" in text
        assert "- `APP-001` Reminder service, its relationships changed" in text
        assert "- `REQ-001` Remind before due date: the job's result in `ci.yml`" in text
        assert "Validation: **conforms**, 0 errors," in text

    def test_a_change_to_a_relationship_alone(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        next(r for r in billing["relationships"] if r["type"] == "stores")["role"] = "replica"
        changed = query.load(docs)
        text = views.plan(query.change_impact(model(), changed), query.gaps(changed), "main")
        assert "Against `main`: no records changed, 1 relationship added, changed or removed." in text
        assert "| ~ | | relationship | `INF-003 --stores--> INFO-001` | `role` |" in text
        assert "- `INFO-001` Invoice, its relationships changed" in text

    def test_no_change(self) -> None:
        text = views.plan(query.change_impact(model(), model()), query.gaps(model()), "abc123")
        assert "No change to the model since `abc123`." in text

    def test_errors_are_listed(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        # An active Outcome that no Measure evaluates breaks rule 7.1.
        billing["records"][0]["lifecycle"] = "active"
        billing["relationships"] = [r for r in billing["relationships"] if r["type"] != "evaluates"]
        broken = query.load(docs)
        text = views.plan(query.change_impact(model(), broken), query.gaps(broken), "main")
        assert "Validation: **does not conform**, 1 error, " in text
        assert "- 7.1 `OUT-001`: no eligible Measure evaluates this Outcome" in text

    def test_pipes_in_names_do_not_break_the_table(self) -> None:
        assert views.cell("a | b\nc") == "a \\| b c"


class TestSite:
    def test_an_index_and_a_page_per_record_with_working_links(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            written = views.site(model(), out)
            assert written[0] == out / "index.html"
            assert {p.name for p in written[1:]} == {f"{rid}.html" for rid in model().records}
            for path in written:
                page = path.read_text()
                assert page.startswith("<!doctype html>") and "<script" not in page
                for href in links(page):
                    if not href.startswith(("http://", "https://")):
                        assert (path.parent / href).resolve().exists(), f"{path.name} links to missing {href}"

    def test_the_index_shows_coverage_and_state(self) -> None:
        page = views.index_page(model())
        assert "<h1>Freelancer invoicing</h1>" in page
        assert "production (serial 3, config 0b6ad3b)" in page
        assert '<span class="chip bad">production: verification failed</span>' in page
        assert "<h2>Capabilities</h2>" in page and "<h2>Information</h2>" in page

    def test_a_record_page(self) -> None:
        page = views.record_page(model(), "REQ-001")
        assert "<h1><code>REQ-001</code> Remind before due date</h1>" in page
        assert "Failed: tests/reminders::test_duplicate." in page
        assert '<a href="https://ci.example/runs/7">1 of 4 selected test cases failed</a>' in page
        assert STATE["pending"][0]["task"] in page
        assert '<a href="PROC-001.html">' in page  # Why it exists
        assert '<a href="APP-001.html">' in page  # How it is realized

    def test_text_is_escaped(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        billing["records"][0]["name"] = "<script>alert(1)</script>"
        page = views.record_page(query.load(docs), "OUT-001")
        assert "<script>" not in page and "&lt;script&gt;" in page

    def test_record_ids_cannot_write_outside_the_records_folder(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        out_001 = next(r for r in billing["records"] if r["id"] == "OUT-001")
        billing["records"] += [{**copy.deepcopy(out_001), "id": rid} for rid in ("../../outside", "../index", "a/b")]
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "site"
            written = views.site(query.load(docs), out)
            assert all(path.parent == out / "records" for path in written[1:])
            assert len(set(written)) == len(written) == len(query.load(docs).records) + 1
            assert sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob("*") if p.is_file()) == sorted(
                p.relative_to(folder).as_posix() for p in written
            )
            assert "<h1>Freelancer invoicing</h1>" in (out / "index.html").read_text()
            index = (out / "index.html").read_text()
            for href in links(index):
                if href.startswith("records/"):
                    assert (out / href).exists(), href
            assert f'href="records/{views.page_name("../index")}"' in index

    def test_distinct_ids_get_distinct_pages(self) -> None:
        encoded = views.page_name("a/b")
        assert encoded.startswith("_") and views.page_name(encoded.removesuffix(".html")) != encoded
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        out_001 = next(r for r in billing["records"] if r["id"] == "OUT-001")
        clash = encoded.removesuffix(".html").lstrip("_")  # The name an encoded ID got before.
        billing["records"] += [{**copy.deepcopy(out_001), "id": rid} for rid in ("a/b", clash)]
        with tempfile.TemporaryDirectory() as folder:
            written = views.site(query.load(docs), Path(folder))
            assert len({p.name for p in written}) == len(written) == len(query.load(docs).records) + 1

    def test_ids_that_differ_only_in_case_are_an_error(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        out_001 = next(r for r in billing["records"] if r["id"] == "OUT-001")
        billing["records"].append({**copy.deepcopy(out_001), "id": "out-001"})
        with tempfile.TemporaryDirectory() as folder, pytest.raises(query.QueryError, match="OUT-001, out-001"):
            views.site(query.load(docs), Path(folder))

    def test_only_web_evidence_is_a_link(self) -> None:
        docs = documents()
        evidence = docs["state/production.json"]["records"]["REQ-001"]["assessments"][0]["evidence"]
        evidence += [
            {**evidence[0], "locator": "javascript:alert(document.domain)", "claim": "Script"},
            {**evidence[0], "locator": " JavaScript:alert(1)", "claim": "Spaced"},
            {**evidence[0], "locator": "validator/tests/test_views.py", "claim": "A file"},
            {**evidence[0], "locator": "https://[invalid", "claim": "Malformed"},
        ]
        page = views.record_page(query.load(docs), "REQ-001")
        assert all(href.startswith("https://") or ":" not in href for href in links(page)), links(page)
        assert "Script <code>javascript:alert(document.domain)</code>" in page
        assert "A file <code>validator/tests/test_views.py</code>" in page
        assert "Malformed <code>https://[invalid</code>" in page
        assert '<a href="https://ci.example/runs/7">' in page

    def test_regenerating_removes_pages_of_deleted_records(self) -> None:
        docs = documents()
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            views.site(query.load(docs), out)
            (out / "notes.html").write_text("kept")
            billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
            billing["records"] = [r for r in billing["records"] if r["id"] != "OUT-001"]
            written = views.site(query.load(docs), out)
            assert not (out / "records" / "OUT-001.html").exists()
            assert sorted((out / "records").iterdir()) == sorted(written[1:])
            assert (out / "notes.html").read_text() == "kept"

    def test_a_case_only_rename_keeps_the_new_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        docs = documents()
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            views.site(query.load(docs), out)
            billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
            for record in billing["records"]:
                if record["id"] == "OUT-001":
                    record["id"] = "out-001"
            renamed = query.load(docs)
            # A file system that ignores case keeps the old name when the new one is written, so the old page
            # must be gone before anything is written.
            write_text = Path.write_text

            def write(path: Path, text: str) -> int:
                assert not (out / "records" / "OUT-001.html").exists(), f"{path.name} written before cleanup"
                return write_text(path, text)

            monkeypatch.setattr(Path, "write_text", write)
            views.site(renamed, out)
            assert sorted(
                p.name for p in (out / "records").iterdir() if "001" in p.name and "OUT" in p.name.upper()
            ) == ["out-001.html"]


def test_the_view_command() -> None:
    from test_query import EXAMPLE

    with tempfile.TemporaryDirectory() as folder:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert exacly_main(["view", "site", "-m", str(EXAMPLE), "--out", folder]) == 0
        assert (Path(folder) / "records" / "REQ-001.html").exists()
        assert out.getvalue().startswith(f"Wrote {len(model().records) + 1} pages to {folder}")
