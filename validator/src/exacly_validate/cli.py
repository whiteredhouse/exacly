"""Command lines.

exacly-validate PATH... [--revision REV] [--date YYYY-MM-DD] [--json]
exacly-refresh PATH... --context CONTEXT --state FILE [--junit FILE...] [--distribution] [run options]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from exacly_validate.core import is_state, validate_documents
from exacly_validate.refresh import RefreshError, Run, read_junit, refresh

SUFFIXES = {".yaml", ".yml", ".json"}


def load(paths: list[Path]) -> dict[str, Any]:
    """Each file given, and every YAML or JSON file under each folder given, by path."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(
                f
                for f in path.rglob("*")
                if f.suffix in SUFFIXES
                and f.is_file()
                and not any(p.startswith(".") for p in f.relative_to(path).parts)
            )
        else:
            files.append(path)
    return {str(f): yaml.safe_load(f.read_text()) for f in files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exacly-validate",
        description="Validate an Exacly model and print the section 11 validation report. "
        "Exits 1 when the model does not conform.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="a 0.1 product map; or 0.2 config and state documents, or folders holding them, such as spec/exacly",
    )
    parser.add_argument(
        "--revision", help="config revision being validated, such as a Git commit; state from another one is reported"
    )
    parser.add_argument(
        "--date", type=dt.date.fromisoformat, default=dt.date.today(), help="report date (default: today)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    documents = load(args.paths)
    if not documents:
        parser.error("no YAML or JSON documents found")
    report = validate_documents(documents, args.date, args.revision)
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.as_text())
    return 1 if report.errors else 0


def github_defaults() -> dict[str, str | None]:
    """Run details GitHub Actions provides in its environment, when running there."""
    env = os.environ
    server, repo, run_id, sha = (
        env.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SHA")
    )
    workflow_ref = env.get("GITHUB_WORKFLOW_REF")  # owner/repo/.github/workflows/spec.yml@refs/heads/main
    return {
        "revision": sha,
        "workflow": workflow_ref.split("@")[0].removeprefix(f"{repo}/") if workflow_ref and repo else None,
        "job": env.get("GITHUB_JOB"),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None,
        "source_url": f"{server}/{repo}/blob/{sha}" if server and repo and sha else None,
    }


def refresh_main(argv: list[str] | None = None) -> int:
    defaults = github_defaults()
    parser = argparse.ArgumentParser(
        prog="exacly-refresh",
        description="Observe what a model's bindings name and write the state document for one context. "
        "In GitHub Actions, the run options default to the current run.",
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="config documents, or folders holding them")
    parser.add_argument("--context", required=True, help="the context observed, such as main or production")
    parser.add_argument(
        "--state", required=True, type=Path, help="state document to write; an earlier one is read first"
    )
    parser.add_argument("--junit", nargs="*", default=[], type=Path, help="JUnit XML test reports from the run")
    parser.add_argument(
        "--distribution",
        action="store_true",
        help="the context is how the product is distributed: an implementer present at the revision counts as released",
    )
    parser.add_argument(
        "--revision", default=defaults["revision"], help="config revision observed, such as a Git commit"
    )
    parser.add_argument("--workflow", default=defaults["workflow"], help="workflow path of the run, for bindings")
    parser.add_argument("--job", default=defaults["job"], help="job of the run, for bindings")
    parser.add_argument("--job-status", help="the job's status so far, for bindings without cases (success, failure)")
    parser.add_argument("--run-url", default=defaults["run_url"], help="link to the run, cited as evidence")
    parser.add_argument("--source-url", default=defaults["source_url"], help="base link to files at the revision")
    parser.add_argument("--root", type=Path, default=Path(), help="repository root that references resolve against")
    args = parser.parse_args(argv)
    if not args.revision:
        parser.error("--revision is required outside GitHub Actions")
    documents = {name: doc for name, doc in load(args.paths).items() if not is_state(doc)}
    previous = json.loads(args.state.read_text()) if args.state.exists() else None
    run = Run(
        revision=args.revision,
        context=args.context,
        observed_at=dt.datetime.now(dt.UTC).replace(microsecond=0),
        workflow=args.workflow,
        job=args.job,
        job_status=args.job_status,
        url=args.run_url,
        source_url=args.source_url,
    )
    try:
        state = refresh(documents, read_junit(args.junit), run, previous, args.distribution, args.root)
    except RefreshError as error:
        print(f"exacly-refresh: {error}", file=sys.stderr)
        return 1
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2) + "\n")
    counts = Counter(f"{a['dimension']} {a['state']}" for r in state["records"].values() for a in r["assessments"])
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no assessments"
    print(f"Wrote {args.state}: {args.context} serial {state['serial']} at {args.revision}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
