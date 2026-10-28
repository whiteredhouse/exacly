"""Build the landing page and its model browsers from the checked-out source and recorded state."""

from __future__ import annotations

import argparse
import html
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from exacly import query, views
from exacly.cli import load

MARKER = "<!-- PROJECT_EVIDENCE -->"
PROJECT_RECORDS = ("REQ-013", "REQ-018")
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
        + '<details class="project-receipt"><summary>Observation context and revisions</summary>'
        + metadata
        + "".join(evidence_metadata)
        + "</details>"
    )
    return (
        f'<details class="project-observation" data-dimension="{escape(dimension)}"{opened}>'
        f"<summary>Recorded {escape(dimension)}: {escape(words(assessment['state']))}</summary>{body}</details>"
    )


def project_evidence(model: query.Model) -> str:
    """Render two useful records from the real model; summaries never substitute for assessment identity."""
    records = []
    for rid in PROJECT_RECORDS:
        shown = query.show(model, rid)
        record = shown["record"]
        observed = shown["state"].get("main", {})
        statuses = observed.get("status", {})
        summary = "".join(
            f"<div><dt>{escape(words(dimension))}</dt>"
            f"<dd>{escape(words(statuses.get(dimension, 'not_observed')))}</dd></div>"
            for dimension in DIMENSIONS
        )
        assessments = observed.get("assessments", [])
        detail = "".join(
            observation(assessment)
            for dimension in DIMENSIONS
            for assessment in assessments
            if assessment["dimension"] == dimension
        )
        for dimension in DIMENSIONS:
            if not any(assessment["dimension"] == dimension for assessment in assessments):
                detail += f'<p class="project-observation">Recorded {escape(dimension)}: Not observed.</p>'
        records.append(
            f'<article class="project-record" data-record="{rid}" data-ui>'
            f'<h3 class="project-record-title"><a href="./project/records/{views.page_name(rid)}">'
            f"<code>{rid}</code> {escape(record['name'])}</a></h3>"
            f"<p>Lifecycle: {escape(record['lifecycle'])}</p>"
            f'<dl class="project-statuses">{summary}</dl>{detail}</article>'
        )
    state = model.states.get("main")
    if state:
        note = (
            f'Main state written <time datetime="{escape(state["written_at"])}">'
            f"{escape(state['written_at'])}</time>; serial {escape(state['serial'])}; "
            f"config revision <code>{escape(state['config_revision'])}</code>. "
            "Each observation retains its own subject and time below the summary."
        )
    else:
        note = "No main state is recorded. Verification and delivery remain unobserved."
    return "".join(records) + f'<p class="project-state-note">{note}</p>'


def model_archive(source: Path, out: Path) -> None:
    """Package canonical model documents, preserving their bytes and relative paths."""
    with tarfile.open(out, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if not path.is_file() or path.suffix not in {".yaml", ".yml", ".json"}:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            content = path.read_bytes()
            entry = tarfile.TarInfo((Path("project-model") / relative).as_posix())
            entry.mode, entry.size = 0o644, len(content)
            archive.addfile(entry, io.BytesIO(content))


def read_model(root: Path, source: Path) -> query.Model:
    documents = {Path(name).relative_to(root).as_posix(): doc for name, doc in load([source]).items()}
    return query.load(documents)


def build(root: Path, out: Path) -> Path:
    """Build into a fresh directory outside the checkout; never refresh or edit observations."""
    root, out = root.resolve(), out.resolve()
    if out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError("The output directory must be outside the source checkout and its ancestors")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("The output directory must be absent or empty")
    source = root / "docs/site"
    index = (source / "index.html").read_text()
    if index.count(MARKER) != 1:
        raise ValueError(f"The landing page must contain exactly one {MARKER} marker")
    invoice_source, project_source = root / "spec/examples/0.2/invoice-reminders", root / "spec/exacly"
    invoice = read_model(root, invoice_source)
    project = read_model(root, project_source)
    proof = project_evidence(project)
    shutil.copytree(source, out, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    invoice_out = out / "example"
    views.site(invoice, invoice_out)
    for document in sorted(invoice_source.glob("*.yaml")):
        shutil.copyfile(document, invoice_out / document.name)
    views.site(project, out / "project")
    model_archive(project_source, out / "project-model.tar.gz")
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
