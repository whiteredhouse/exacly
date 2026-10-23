"""Command line: exacly-validate PATH... [--revision REV] [--date YYYY-MM-DD] [--json]."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from exacly_validate.core import validate_documents

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


if __name__ == "__main__":
    sys.exit(main())
