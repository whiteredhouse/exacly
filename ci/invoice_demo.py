"""Stage the existing invoice example with clearly fictional, tooling-derived demonstration state."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path

import yaml
from exacly.cli import load
from exacly.core import ERROR, validate_documents
from exacly.refresh import Run, read_junit, refresh

DEMO_CONTEXT = "demo"
DEMO_REVISION = "invoice-reminders-demo-v1"
DEMO_AT = dt.datetime(2026, 10, 28, 12, tzinfo=dt.UTC)
DEMO_HEADER = "# Fictional demonstration derived from the existing invoice-reminders example. See DEMO.md.\n"


def prepare_demo(root: Path, out: Path) -> Path:
    """Copy canonical intent, make the reminder requirement active, and observe an illustrative report.

    Canonical source documents stay unchanged. The report is a fixture, never a claim
    that this repository runs reminder-service tests or observes a real deployment.
    """
    root, out = root.resolve(), out.resolve()
    if out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError("The demo directory must be outside the source checkout and its ancestors")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("The demo directory must be absent or empty")
    source = root / "spec/examples/0.2/invoice-reminders"
    fixtures = root / "ci/fixtures/invoice-reminders"
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        if "model" in document:
            document["model"]["environment"] = DEMO_CONTEXT
        for record in document.get("records", []):
            if record["id"] == "REQ-001":
                record["lifecycle"] = "active"
                record["last_reviewed_at"] = DEMO_AT.date().isoformat()
        (out / path.name).write_text(DEMO_HEADER + yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    for name in ("sample-junit.xml", "DEMO.md"):
        shutil.copyfile(fixtures / name, out / name)
    documents = load([out])
    run = Run(
        revision=DEMO_REVISION,
        context=DEMO_CONTEXT,
        observed_at=DEMO_AT,
        workflow=".github/workflows/ci.yml",
        job_status="success",
        url="sample-junit.xml",
    )
    state = refresh(documents, read_junit([out / "sample-junit.xml"]), run)
    report = validate_documents({**documents, "state/demo.json": state}, DEMO_AT.date(), DEMO_REVISION)
    errors = [finding for finding in report.findings if finding.severity == ERROR]
    if errors:
        raise ValueError(f"Invalid invoice demonstration: {errors}")
    (out / "state").mkdir()
    (out / "state/demo.json").write_text(json.dumps(state, indent=2) + "\n")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="fresh demo directory outside the checkout")
    args = parser.parse_args()
    print(f"Prepared {prepare_demo(Path(__file__).resolve().parents[1], args.out)}")


if __name__ == "__main__":
    main()
