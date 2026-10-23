"""Command line: exacly-validate MAP [--date YYYY-MM-DD] [--json]."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

from exacly_validate.core import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exacly-validate",
        description="Validate an Exacly product map and print the section 11 validation report. "
        "Exits 1 when the map does not conform.",
    )
    parser.add_argument("map", type=Path, help="product map, YAML or JSON")
    parser.add_argument(
        "--date", type=dt.date.fromisoformat, default=dt.date.today(), help="report date (default: today)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = validate(yaml.safe_load(args.map.read_text()), args.date)
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.as_text())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
