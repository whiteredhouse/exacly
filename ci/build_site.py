"""Build the small invoice demonstration and a separate view of Exacly's recorded project state."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from exacly import query, views
from exacly.cli import load

from ci.invoice_demo import prepare_demo

MARKER = "<!-- DEMO_EVIDENCE -->"
DIMENSIONS = ("verification", "delivery")


def escape(value: Any) -> str:
    return html.escape(str(value))


def words(value: Any) -> str:
    return str(value).replace("_", " ").capitalize()


def identity_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def observation(assessment: dict[str, Any]) -> str:
    """Keep each raw assessment's subject, time and evidence together, without retargeting it."""
    dimension = assessment["dimension"]
    opened = " open" if dimension == "verification" else ""
    observed_at = f'<p>Assessed at <time datetime="{escape(assessment["at"])}">{escape(assessment["at"])}</time></p>'
    metadata = (
        f"<p>Source: {escape(assessment.get('source', 'Not recorded'))}. "
        f"Assessment context: <code>{escape(assessment['context'])}</code></p>"
    )
    subject = assessment.get("subject")
    if subject:
        for key, value in subject.items():
            label = "Subject " + key.replace("_", " ")
            metadata += f"<p>{escape(label)}: <code>{escape(identity_value(value))}</code></p>"
    else:
        metadata += "<p>Subject identity not recorded.</p>"
    claims, evidence_metadata = [], []
    for evidence in assessment.get("evidence", []):
        locator = str(evidence.get("locator", ""))
        claim = escape(evidence.get("claim", "Evidence"))
        url = urlsplit(locator)
        if url.scheme == "https" and url.netloc:
            claim = f'<a href="{escape(locator)}">{claim}</a>'
        claims.append(f"<p>Evidence: {claim}</p>")
        if locator and not (url.scheme == "https" and url.netloc):
            evidence_metadata.append(f"<p>Locator: <code>{escape(locator)}</code></p>")
        for key in ("observed_at", "applies_to"):
            if key in evidence:
                label = "Evidence " + key.replace("_", " ")
                evidence_metadata.append(
                    f"<p>{escape(label)}: <code>{escape(identity_value(evidence[key]))}</code></p>"
                )
    body = (
        "".join(claims)
        + observed_at
        + '<details class="demo-receipt"><summary>Observation context and revisions</summary>'
        + metadata
        + "".join(evidence_metadata)
        + "</details>"
    )
    return (
        f'<details class="demo-observation" data-dimension="{escape(dimension)}"{opened}>'
        f"<summary>Recorded {escape(dimension)}: {escape(words(assessment['state']))}</summary>{body}</details>"
    )


def demo_evidence(model: query.Model) -> str:
    """Show the single invoice requirement, preserving the demonstration assessment's identity."""
    rid = "REQ-001"
    shown = query.show(model, rid)
    record = shown["record"]
    observed = shown["state"].get("demo", {})
    statuses = observed.get("status", {})
    summary = f"<div><dt>Lifecycle</dt><dd>{escape(words(record['lifecycle']))}</dd></div>"
    summary += "".join(
        f"<div><dt>{escape(words(dimension))}</dt>"
        f"<dd>{escape(words(statuses.get(dimension, 'not_observed')))}</dd></div>"
        for dimension in DIMENSIONS
    )
    details = "".join(observation(a) for a in observed.get("assessments", []) if a["dimension"] in DIMENSIONS)
    return (
        f'<article class="demo-record" data-record="{rid}" data-ui>'
        f'<h3 class="demo-record-title"><a href="./example/records/{views.page_name(rid)}">'
        f"{escape(record['name'])}</a></h3>"
        f"<p><code>{rid}</code> · Demonstration state</p>"
        f'<dl class="demo-statuses">{summary}</dl>{details}</article>'
    )


def model_archive(source: Path, out: Path) -> None:
    """Package the prepared demonstration, including its explanation and illustrative report."""
    with tarfile.open(out, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if not path.is_file() or path.suffix not in {".yaml", ".yml", ".json", ".xml", ".md"}:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            content = path.read_bytes()
            entry = tarfile.TarInfo((Path("invoice-model") / relative).as_posix())
            entry.mode, entry.size = 0o644, len(content)
            archive.addfile(entry, io.BytesIO(content))


def read_model(root: Path, source: Path) -> query.Model:
    base = root if source.is_relative_to(root) else source
    documents = {Path(name).relative_to(base).as_posix(): doc for name, doc in load([source]).items()}
    return query.load(documents)


def version_assets(index: str, out: Path) -> str:
    """Give changed CSS and JavaScript new URLs so fresh HTML cannot reuse older cached assets."""
    for name in ("style.css", "site.js"):
        reference = f'"./{name}"'
        if index.count(reference) != 1:
            raise ValueError(f"The landing page must reference {name} exactly once")
        path = out / name
        content = path.read_bytes()
        version = hashlib.sha256(content).hexdigest()[:12]
        versioned = f"{path.stem}.{version}{path.suffix}"
        (out / versioned).write_bytes(content)
        index = index.replace(reference, f'"./{versioned}"')
    return index


def build(root: Path, out: Path) -> Path:
    """Build outside the checkout; derive a labelled demo without editing canonical inputs or real state."""
    root, out = root.resolve(), out.resolve()
    if out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError("The output directory must be outside the source checkout and its ancestors")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("The output directory must be absent or empty")
    source = root / "docs/site"
    index = (source / "index.html").read_text()
    if index.count(MARKER) != 1:
        raise ValueError(f"The landing page must contain exactly one {MARKER} marker")
    project = read_model(root, root / "spec/exacly")
    shutil.copytree(source, out, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    index = version_assets(index, out)
    invoice_out = out / "example"
    with tempfile.TemporaryDirectory(prefix="exacly-invoice-demo-") as temporary:
        demo = prepare_demo(root, Path(temporary) / "model")
        invoice = read_model(root, demo)
        proof = demo_evidence(invoice)
        shutil.copytree(demo, invoice_out, dirs_exist_ok=True)
        pages = views.site(invoice, invoice_out)
        for page in pages:
            prefix = "../" if page.parent.name == "records" else "./"
            notice = (
                "<aside><p><strong>Invoice demonstration.</strong> "
                "Illustrative test results; delivery is not observed. "
                f'<a href="{prefix}DEMO.md">Read the demo notes</a> · '
                f'<a href="{prefix}sample-junit.xml">Sample test report</a></p></aside>'
            )
            page.write_text(page.read_text().replace("<body>", "<body>" + notice, 1))
        model_archive(demo, out / "invoice-model.tar.gz")
    views.site(project, out / "project")
    (out / "index.html").write_text(index.replace(MARKER, proof))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="fresh output directory outside the checkout")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(f"Built {build(root, args.out)}")


if __name__ == "__main__":
    main()
