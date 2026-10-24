"""Command lines.

exacly-validate PATH... [--revision REV] [--date YYYY-MM-DD] [--json]
exacly-refresh PATH... --context CONTEXT --state FILE [--junit FILE...] [--distribution] [run options]
exacly show|trace|impact|gaps [-m PATH]... [--json] ...; exacly mcp [-m PATH]...
exacly view plan [--base REV] | site [--out DIR]
exacly validate|refresh ... run the commands above
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

from exacly_validate import mcp, query, views
from exacly_validate.core import is_state, validate_documents
from exacly_validate.refresh import RefreshError, Run, read_junit, refresh

SUFFIXES = {".yaml", ".yml", ".json"}
DEFAULT_MODEL = Path("spec/exacly")


def wanted(file: Path, folder: Path) -> bool:
    """Whether a file under a folder given is a model document: YAML or JSON, not in a hidden folder."""
    return file.suffix in SUFFIXES and not any(p.startswith(".") for p in file.relative_to(folder).parts)


def load(paths: list[Path]) -> dict[str, Any]:
    """Each file given, and every YAML or JSON file under each folder given, by path."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(f for f in path.rglob("*") if f.is_file() and wanted(f, path))
        else:
            files.append(path)
    return {str(f): read(f) for f in files}


def read(file: Path) -> Any:
    """One document, parsed. Raises QueryError, saying which file and where, when it can't be read or parsed."""
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise query.QueryError(f"{file} can't be read: {error}") from error
    return parse(text, str(file))


class Loader(yaml.SafeLoader):
    """PyYAML's safe loader, but a scalar that matches a type without being one, such as the unquoted date
    2026-13-24, is a ConstructorError that says where, not a bare ValueError."""

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except ValueError as error:
            kind = node.tag.rsplit(":", 1)[-1]
            problem = f"{node.value!r} is not a valid {kind}: {error}"
            raise yaml.constructor.ConstructorError(None, None, problem, node.start_mark) from error


def parse(text: str, name: str) -> Any:
    try:
        return yaml.load(text, Loader)
    except yaml.YAMLError as error:
        where = getattr(error, "problem_mark", None)
        at = f" at line {where.line + 1}, column {where.column + 1}" if where else ""
        raise query.QueryError(f"{name} is not valid YAML{at}: {getattr(error, 'problem', None) or error}") from error


def load_at(revision: str, paths: list[Path]) -> dict[str, Any]:
    """As `load`, but the files as they were at a Git revision. Paths may be relative or absolute, inside the
    repository that holds them; documents are named by their path in that repository."""

    def git(where: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(where), *args], check=True, capture_output=True, encoding="utf-8"
        ).stdout

    documents = {}
    for path in paths:
        path = path.resolve()
        top = Path(git(path if path.is_dir() else path.parent, "rev-parse", "--show-toplevel").strip()).resolve()
        try:
            inside = path.relative_to(top)
        except ValueError as error:
            raise query.QueryError(f"{path} is not in the Git repository at {top}") from error
        # NUL-separated, so Git doesn't quote names with non-ASCII or special characters.
        for name in git(top, "ls-tree", "-r", "-z", "--name-only", revision, "--", str(inside)).split("\0"):
            if not name:
                continue
            file = Path(name)
            if file == inside or wanted(file, inside):
                documents[name] = parse(git(top, "show", f"{revision}:{name}"), f"{name} at {revision}")
    return documents


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
    try:
        documents = load(args.paths)
    except query.QueryError as error:
        print(f"exacly-validate: {error}", file=sys.stderr)
        return 1
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
    try:
        documents = {name: doc for name, doc in load(args.paths).items() if not is_state(doc)}
    except query.QueryError as error:
        print(f"exacly-refresh: {error}", file=sys.stderr)
        return 1
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


def label(node: dict[str, Any]) -> str:
    """One line naming a record, with its status in each context."""
    text = f"{node['id']} {node['type']}: {node['name']}"
    if node["lifecycle"] != "active":
        text += f" ({node['lifecycle']})"
    status = node.get("status") or {}
    if status:
        text += (
            "  ["
            + "; ".join(
                f"{context}: " + ", ".join(f"{d} {s}" for d, s in sorted(dims.items()))
                for context, dims in sorted(status.items())
            )
            + "]"
        )
    return text


def tree_lines(node: dict[str, Any], depth: int = 0) -> list[str]:
    via = node.get("via")
    via = f"{via['type']} {'->' if via['direction'] == 'out' else '<-'} " if via else ""
    line = "  " * depth + via + label(node) + (" (above)" if node.get("repeated") else "")
    return [line, *(x for child in node.get("children", []) for x in tree_lines(child, depth + 1))]


def finding_line(f: dict[str, Any]) -> str:
    where = f" ({f['document']})" if f.get("document") else ""
    return f"  {f['severity']} {f['rule']} {f.get('record') or ''}: {f['message']}{where}".replace(" :", ":")


def show_text(answer: dict[str, Any]) -> str:
    r = answer["record"]
    lines = [label({**r, "status": None}), r["description"]]
    lines.append(f"Owner {r['owner']}, reviewed {r['last_reviewed_at'] or 'never'}, in {answer['document']}")
    details = {
        k: v
        for k, v in r.items()
        if k not in {"id", "type", "name", "lifecycle", "description", "owner", "last_reviewed_at", "evidence"}
    }
    if details:
        lines += ["", yaml.safe_dump(details, sort_keys=False, allow_unicode=True, width=100).rstrip()]
    lines += ["", "Relationships"]
    for rel in answer["relationships"]:
        arrow = "->" if rel["direction"] == "out" else "<-"
        role = f" ({rel['role']})" if rel.get("role") else ""
        lines.append(f"  {rel['type']}{role} {arrow} {label(rel['record'])}")
    if not answer["relationships"]:
        lines.append("  none")
    lines += ["", "State"]
    for context, observed in sorted(answer["state"].items()):
        lines.append(f"  {context}, observed against {observed['config_revision'][:12]}:")
        for a in observed.get("assessments", []):
            reason = f": {a['reason']}" if a.get("reason") else ""
            cited = "; ".join(f"{e['claim']} ({e['locator']})" for e in a["evidence"])
            lines.append(f"    {a['dimension']} {a['state']}{reason}" + (f" - {cited}" if cited else ""))
        for work in observed.get("pending", []):
            lines.append(f"    pending {work['task']} since {work['opened_at']}")
    if not answer["state"]:
        lines.append("  not observed")
    lines += [
        "",
        "Findings",
        *(finding_line(f) for f in answer["findings"]),
        *([] if answer["findings"] else ["  none"]),
    ]
    return "\n".join(lines)


def trace_text(answer: dict[str, Any]) -> str:
    return "\n".join(["Why", *tree_lines(answer["why"]), "", "How", *tree_lines(answer["how"])])


def impact_text(answer: dict[str, Any]) -> str:
    lines = []
    if "records" in answer:
        for kind, sign in (("added", "+"), ("changed", "~"), ("removed", "-")):
            for r in answer["records"][kind]:
                fields = f" ({', '.join(r['fields'])})" if r.get("fields") else ""
                lines.append(f"{sign} {r['id']} {r['type']}: {r['name']}{fields}")
        lines += [f"+ {rel}" for rel in answer["relationships"]["added"]]
        lines += [f"~ {r['relationship']} ({', '.join(r['fields'])})" for r in answer["relationships"]["changed"]]
        lines += [f"- {rel}" for rel in answer["relationships"]["removed"]]
        if not lines:
            return "No changes to the model."
        lines.append("")
        answer = answer["impact"]
    else:
        lines += ["Changed", *(f"  {label(r)}" for r in answer["changed"]), ""]
    lines.append("Affected")
    lines += [f"  {label(r)}\n    {views.via(r['path'], ' -> ')}" for r in answer["affected"]] or ["  nothing else"]
    lines += ["", "Outcomes: " + (", ".join(answer["outcomes"]) or "none reached"), "", "Check again"]
    for r in answer["recheck"]:
        tests = r["observe"].get("tests")
        how = (
            f"{tests['workflow']}" + (f" cases {', '.join(tests['cases'])}" if tests.get("cases") else "")
            if tests
            else "no tests binding"
        )
        lines.append(f"  {r['id']} {r['name']}: {how}")
    if not answer["recheck"]:
        lines.append("  no Requirement")
    return "\n".join(lines)


def gaps_text(answer: dict[str, Any]) -> str:
    contexts = ", ".join(
        f"{c} (serial {s['serial']}, config {s['config_revision'][:12]})" for c, s in sorted(answer["contexts"].items())
    )
    lines = [f"Result: {answer['result']}", f"State: {contexts or 'none'}", ""]
    for kind in ("errors", "warnings", "waived"):
        lines += [f"{kind.capitalize()} ({len(answer[kind])})", *(finding_line(f) for f in answer[kind]), ""]
    titles = {
        "unverified": "Requirements not verified in any context",
        "unreleased": "Requirements not released in any context",
        "unobserved": "Requirements with no observation binding",
        "proposed_measures": "Measures still proposed",
    }
    for kind, title in titles.items():
        items = answer["coverage"][kind]
        lines.append(f"{title} ({len(items)})")
        lines += [f"  {label(r)}" for r in items]
        lines.append("")
    lines.append(f"Pending work ({len(answer['pending'])})")
    lines += [f"  {w['context']}: {w['record']} {w['task']} since {w['opened_at']}" for w in answer["pending"]]
    return "\n".join(lines).rstrip()


def load_base(revision: str, paths: list[Path], model: query.Model) -> query.Model:
    """The model at `paths` as it was at a Git revision; empty when it didn't exist yet."""
    try:
        documents = load_at(revision, paths)
    except subprocess.CalledProcessError as error:
        raise query.QueryError(f"git failed: {error.stderr.strip()}") from error
    return query.load(documents) if documents else query.empty(model.graph.namespace)


def ask(
    command: str,
    paths: list[Path],
    record: str | None = None,
    records: Iterable[str] = (),
    base: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Answer one question about the model at `paths` (show, trace, impact or gaps), as data and as text.
    Impact takes `records`, or a Git revision as `base`. Raises QueryError when it can't answer."""
    model = query.load(load(paths))
    if command in ("show", "trace"):
        if not record:
            raise query.QueryError(f"{command} needs a record ID")
        answer = query.show(model, record) if command == "show" else query.trace(model, record)
        return answer, (show_text if command == "show" else trace_text)(answer)
    if command == "impact":
        records = list(records)
        if bool(base) == bool(records):
            raise query.QueryError("impact takes record IDs or a base revision, not both")
        answer = query.change_impact(load_base(base, paths, model), model) if base else query.impact(model, records)
        return answer, impact_text(answer)
    if command == "gaps":
        answer = query.gaps(model)
        return answer, gaps_text(answer)
    raise query.QueryError(f"unknown question {command}")


def view_main(args: argparse.Namespace) -> int:
    paths = args.model or [DEFAULT_MODEL]
    model = query.load(load(paths))
    if args.kind == "plan":
        base = args.base or "main"
        print(views.plan(query.change_impact(load_base(base, paths, model), model), query.gaps(model), base), end="")
        return 0
    out = args.out or Path("reports/site")
    written = views.site(model, out)
    print(f"Wrote {len(written)} pages to {out}; open {out / 'index.html'}")
    return 0


def exacly_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("validate", "refresh"):
        return main(argv[1:]) if argv[0] == "validate" else refresh_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="exacly",
        description="Ask an Exacly model about its records, from its config and state. "
        "validate and refresh run exacly-validate and exacly-refresh.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-m",
        "--model",
        action="append",
        type=Path,
        metavar="PATH",
        help="config and state documents, or folders holding them (default: spec/exacly)",
    )
    common.add_argument("--json", action="store_true", help="print the answer as JSON")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("validate", help="validate the model (exacly-validate)")
    commands.add_parser("refresh", help="write state from observations (exacly-refresh)")
    show = commands.add_parser("show", parents=[common], help="one record: relationships, state and findings")
    show.add_argument("record")
    trace = commands.add_parser("trace", parents=[common], help="why a record exists, and how it is realized")
    trace.add_argument("record")
    impact = commands.add_parser("impact", parents=[common], help="what a change to records affects")
    impact.add_argument("records", nargs="*", metavar="RECORD")
    impact.add_argument("--base", metavar="REV", help="compare with the model at this Git revision instead")
    commands.add_parser("gaps", parents=[common], help="findings, and what is not yet verified, released or observed")
    commands.add_parser(
        "mcp", parents=[common], help="answer these questions for coding agents, as an MCP server on stdio"
    )
    view = commands.add_parser("view", parents=[common], help="section 10 views: a pull request plan, or a site")
    view.add_argument("kind", choices=["plan", "site"], help="plan: Markdown for a pull request; site: HTML pages")
    view.add_argument("--base", metavar="REV", help="plan: the Git revision the change is against (default: main)")
    view.add_argument("--out", type=Path, metavar="DIR", help="site: the folder to write (default: reports/site)")
    args = parser.parse_args(argv)
    if args.command == "view":
        try:
            return view_main(args)
        except query.QueryError as error:
            print(f"exacly: {error}", file=sys.stderr)
            return 1
    if args.command == "mcp":
        mcp.serve(mcp.Server(args.model or [DEFAULT_MODEL], ask, metadata.version("exacly-validate")))
        return 0
    try:
        answer, text = ask(
            args.command,
            args.model or [DEFAULT_MODEL],
            record=getattr(args, "record", None),
            records=getattr(args, "records", ()),
            base=getattr(args, "base", None),
        )
    except query.QueryError as error:
        print(f"exacly: {error}", file=sys.stderr)
        return 1
    print(json.dumps(answer, indent=2, default=str) if args.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
