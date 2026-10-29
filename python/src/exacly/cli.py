"""Command lines.

exacly validate PATH... [--revision REV] [--date YYYY-MM-DD] [--json]
exacly refresh PATH... --context CONTEXT --state FILE [--junit FILE...] [--distribution] [run options]
exacly observe manual-qa PATH... --target FILE --state FILE [--revision REV] [--root DIR]
exacly admit github-actions --trust-policy FILE --target FILE --run ID --attempt N --date YYYY-MM-DD --out DIR
exacly reconcile github-actions --dry-run --plan FILE --action ID --policy FILE --trust-policy FILE --target FILE ...
exacly complete github-actions --plan FILE --action ID --policy FILE --trust-policy FILE --target FILE ...
exacly completions show|recover --ledger FILE --completion ID [--state FILE] [--json]
exacly executions register --ledger FILE --plan FILE --action ID --state FILE --task TASK --opened-at ISO ...
exacly executions show --ledger FILE --binding ID [--json]
exacly executions inspect --ledger FILE [--lock-timeout SECONDS] [--json]
exacly executions backup|recover|upgrade --ledger FILE --out NEWDIR [--lock-timeout SECONDS] [--json]
exacly executions index --ledger FILE --out NEWDIR [--dry-run] [--lock-timeout SECONDS] [--json]
exacly executions activate --ledger FILE --out NEWDIR [--dry-run] [--lock-timeout SECONDS] [--json]
exacly executions audit --ledger FILE [--lock-timeout SECONDS] [--json]
exacly isolation inspect --rootfs DIR [--json]
exacly isolate --runtime-policy FILE [--inputs FILE] --out NEWDIR [--timeout SECONDS] [--json] -- COMMAND...
exacly plan [-m PATH]... --policy FILE --target FILE --date YYYY-MM-DD [--json] [--out FILE]
exacly show|trace|impact|gaps [-m PATH]... [--json] ...; exacly mcp [-m PATH]...
exacly view plan [--base REV] | site [--out DIR]
exacly-validate and exacly-refresh are the first two under their names before 0.2.0, kept for one release
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import jsonschema
import yaml

from exacly import manual_qa, mcp, planning, policy, query, state_writer, views
from exacly.applicability import Target
from exacly.core import binding_words, bindings, identity, is_state, validate_documents
from exacly.refresh import RefreshError, Run, check_distribution_identity, read_junit, refresh, unattributed

if TYPE_CHECKING:
    from exacly.admission import AdmissionResult, Source

SUFFIXES = {".yaml", ".yml", ".json"}
DEFAULT_MODEL = Path("spec/exacly")


def wanted(file: Path, folder: Path) -> bool:
    """Whether a file under a folder given is a model document: YAML or JSON, not in a hidden folder."""
    return file.suffix in SUFFIXES and not any(p.startswith(".") for p in file.relative_to(folder).parts)


def is_pattern(path: Path) -> bool:
    return any(c in str(path) for c in "*?[")


def static_part(path: Path) -> Path:
    """The folder a pattern such as spec/*/model.yaml starts in: its parts before the first one with a wildcard."""
    parts = path.parts
    first = next(i for i, part in enumerate(parts) if is_pattern(Path(part)))
    return Path(*parts[:first]) if first else Path(".")


def matches(name: str, pattern: str) -> bool:
    """Whether a path matches a pattern part by part, as `glob` does: * and ? don't cross a /, and a hidden
    name, starting with a dot, matches only a part that starts with a dot too."""
    names, patterns = PurePosixPath(name).parts, PurePosixPath(pattern).parts
    return len(names) == len(patterns) and all(
        fnmatchcase(n, p) and (p.startswith(".") or not n.startswith(".")) for n, p in zip(names, patterns, strict=True)
    )


def load(paths: list[Path]) -> dict[str, Any]:
    """Each file given, every file a pattern such as spec/exacly/*.yaml matches, and every YAML or JSON file under
    each folder given, by path."""
    files: list[Path] = []
    for path in paths:
        if is_pattern(path):
            found = sorted(Path(f) for f in glob.glob(str(path)) if Path(f).is_file())
            if not found:
                raise query.QueryError(f"{path} matches no files")
            files += found
        elif path.is_dir():
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
    repository that holds them; documents are named by their path in that repository. A pattern is matched
    against the files at that revision, so a file the change deleted is still compared."""

    def git(where: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(where), *args], check=True, capture_output=True, encoding="utf-8"
        ).stdout

    documents = {}
    for given in paths:
        pattern = is_pattern(given)
        if pattern:
            # Resolve the folder the pattern starts in, so that it compares with the repository's real path through
            # symbolic links, such as macOS's /var for /private/var.
            path = Path(os.path.normpath(Path.cwd() / given))
            folder = static_part(path)
            path = folder.resolve() / path.relative_to(folder)
        else:
            path = given.resolve()
        start = static_part(path) if pattern else path if path.is_dir() else path.parent
        while not start.is_dir():  # A folder the change deleted, which held files at the revision.
            start = start.parent
        top = Path(git(start, "rev-parse", "--show-toplevel").strip()).resolve()
        try:
            inside = path.relative_to(top)
        except ValueError as error:
            raise query.QueryError(f"{path} is not in the Git repository at {top}") from error
        listed = static_part(inside) if pattern else inside
        # NUL-separated, so Git doesn't quote names with non-ASCII or special characters.
        for name in git(top, "ls-tree", "-r", "-z", "--name-only", revision, "--", str(listed)).split("\0"):
            if not name:
                continue
            file = Path(name)
            if matches(name, inside.as_posix()) if pattern else file == inside or wanted(file, inside):
                documents[name] = parse(git(top, "show", f"{revision}:{name}"), f"{name} at {revision}")
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exacly validate",
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
        print(f"exacly validate: {error}", file=sys.stderr)
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
        prog="exacly refresh",
        description="Observe what a model's bindings name and write the state document for one context. "
        "In GitHub Actions, the run options default to the current run.",
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="config documents, or folders holding them")
    parser.add_argument("--context", required=True, help="the context observed, such as main or production")
    parser.add_argument(
        "--state", required=True, type=Path, help="state document to write; an earlier one is read first"
    )
    parser.add_argument(
        "--lock-timeout",
        type=nonnegative_seconds,
        default=state_writer.DEFAULT_LOCK_TIMEOUT,
        help="maximum state publication lock wait in seconds (default: 30)",
    )
    parser.add_argument("--junit", nargs="*", default=[], type=Path, help="JUnit XML test reports from the run")
    parser.add_argument(
        "--distribution",
        action="store_true",
        help="the context is how the product is distributed: an implementer present at the revision counts as released",
    )
    parser.add_argument(
        "--revision", default=defaults["revision"], help="implementation revision observed, such as a Git commit"
    )
    parser.add_argument(
        "--model-revision", help="revision defining the observed criteria and bindings (default: --revision)"
    )
    parser.add_argument("--workflow", default=defaults["workflow"], help="workflow path of the run, for bindings")
    parser.add_argument("--job", default=defaults["job"], help="job of the run, for bindings")
    parser.add_argument("--job-status", help="the job's status so far, for bindings without cases (success, failure)")
    parser.add_argument("--run-url", default=defaults["run_url"], help="link to the run, cited as evidence")
    parser.add_argument("--source-url", help="base link to implementation files (default: GitHub blob at --revision)")
    parser.add_argument("--root", type=Path, default=Path(), help="repository root that references resolve against")
    args = parser.parse_args(argv)
    if args.revision is None:
        parser.error("--revision is required outside GitHub Actions")
    source_url = args.source_url
    if source_url is None:
        server, repo = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
        source_url = f"{server}/{repo}/blob/{args.revision}" if server and repo else None
    try:
        run = Run(
            revision=args.revision,
            model_revision=args.model_revision,
            context=args.context,
            observed_at=dt.datetime.now(dt.UTC).replace(microsecond=0),
            workflow=args.workflow,
            job=args.job,
            job_status=args.job_status,
            url=args.run_url,
            source_url=source_url,
        )
        if args.distribution:
            check_distribution_identity(run)
        documents = {name: doc for name, doc in load(args.paths).items() if not is_state(doc)}
    except (query.QueryError, RefreshError) as error:
        print(f"exacly refresh: {error}", file=sys.stderr)
        return 1
    try:
        snapshot = state_writer.read(args.state)
        state = refresh(documents, read_junit(args.junit), run, snapshot.document, args.distribution, args.root)
        state_writer.publish(snapshot, state, lock_timeout=args.lock_timeout)
    except (RefreshError, state_writer.StateWriteError, OSError, ET.ParseError) as error:
        print(f"exacly refresh: {error}", file=sys.stderr)
        return 1
    counts = Counter(f"{a['dimension']} {a['state']}" for r in state["records"].values() for a in r["assessments"])
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no assessments"
    observed = run.revision
    if run.config_revision != run.revision:
        observed = f"implementation {run.revision} (model {run.config_revision})"
    print(f"Wrote {args.state}: {args.context} serial {state['serial']} at {observed}: {summary}")
    unknown = unattributed(state)
    if unknown:
        print(
            f"exacly refresh: kept assessments that name no source, so their origin is unknown: {', '.join(unknown)}",
            file=sys.stderr,
        )
    return 0


def criteria_at(paths: list[Path]) -> Callable[[str], dict[str, list[str]] | None]:
    """Each record's acceptance criteria in the config at a Git revision, or None when it can't be read there."""

    def at(revision: str) -> dict[str, list[str]] | None:
        try:
            documents = load_at(revision, paths)
        except (subprocess.CalledProcessError, OSError, query.QueryError):
            return None
        return {
            r["id"]: list(r.get("acceptance_criteria", []))
            for doc in documents.values()
            if isinstance(doc, dict) and not is_state(doc)
            for r in doc.get("records", [])
            if isinstance(r, dict) and "id" in r
        }

    return at


def observe_main(argv: list[str]) -> int:
    defaults = github_defaults()
    parser = argparse.ArgumentParser(
        prog="exacly observe",
        description="Observe one extension source and write the state document for the context it names, replacing "
        "only that source's assessments.",
    )
    parser.add_argument("source", choices=[manual_qa.EXTENSION], help="manual-qa: observations people commit")
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="config documents, or folders holding them")
    parser.add_argument("--target", required=True, type=Path, help="the target manifest: context, release and builds")
    parser.add_argument(
        "--state", required=True, type=Path, help="state document to write; an earlier one is read first"
    )
    parser.add_argument(
        "--lock-timeout",
        type=nonnegative_seconds,
        default=state_writer.DEFAULT_LOCK_TIMEOUT,
        help="maximum state publication lock wait in seconds (default: 30)",
    )
    parser.add_argument(
        "--revision", default=defaults["revision"], help="config revision observed, such as a Git commit"
    )
    parser.add_argument("--root", type=Path, default=Path(), help="repository root that observation paths start at")
    args = parser.parse_args(argv)
    if not args.revision:
        parser.error("--revision is required outside GitHub Actions")
    try:
        documents = {name: doc for name, doc in load(args.paths).items() if not is_state(doc)}
        target = manual_qa.read(args.target)
    except (query.QueryError, OSError, ValueError, yaml.YAMLError) as error:
        print(f"exacly observe: {error}", file=sys.stderr)
        return 1
    written_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        snapshot = state_writer.read(args.state)
        state, notes = manual_qa.observe(
            documents, target, args.revision, written_at, snapshot.document, args.root, criteria_at(args.paths)
        )
        state_writer.publish(snapshot, state, lock_timeout=args.lock_timeout)
    except (RefreshError, state_writer.StateWriteError, OSError) as error:
        print(f"exacly observe: {error}", file=sys.stderr)
        return 1
    counts = Counter(
        a["state"]
        for r in state["records"].values()
        for a in r.get("assessments", [])
        if a.get("source") == manual_qa.SOURCE
    )
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no assessments"
    where = f"{state['context']} serial {state['serial']} at {args.revision}"
    print(f"Wrote {args.state}: {where}: {manual_qa.SOURCE} {summary}")
    for note in notes:
        print(f"exacly observe: {note}", file=sys.stderr)
    return 0


def source_words(assessment: dict[str, Any]) -> str:
    """The source and binding that gave an assessment, such as " from tests (.github/workflows/ci.yml test)"."""
    if "source" not in assessment:
        return ""
    binding = binding_words(assessment.get("binding", {}))
    return f" from {assessment['source']}" + (f" ({binding})" if binding else "")


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
            lines.append(
                f"    {a['dimension']} {a['state']}{source_words(a)}{reason}" + (f" - {cited}" if cited else "")
            )
        for m in observed.get("missing", []):
            lines.append(f"    {m['dimension']} has no result{source_words(m)}, which is bound to it")
        if len(observed.get("assessments", [])) > len(observed.get("status", {})) or observed.get("missing"):
            combined = ", ".join(f"{d} {s}" for d, s in sorted(observed["status"].items()))
            lines.append(f"    combined: {combined}")
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
        tests = bindings(r, "tests")
        how = "; ".join(
            " ".join(identity("tests", b).values()) + (f" cases {', '.join(b['cases'])}" if b.get("cases") else "")
            for b in tests
        )
        lines.append(f"  {r['id']} {r['name']}: {how or 'no tests binding'}")
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
    observers = answer.get("observers", [])
    if observers:
        lines.append(f"From extension observers ({len(observers)})")
        lines += [f"  {o['record']} in {o['context']}, {o['source']}: {o['message']}" for o in observers]
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


def write_plan(out: Path, paths: list[Path], inputs: Iterable[Path], text: str) -> None:
    """Save a derived artifact outside its inputs and model folders, replacing it atomically.

    A glob's static prefix is treated as a model folder too: a later recursive model read must not
    accidentally load a saved plan. Resolving paths also protects inputs accessed through symlink aliases.
    """
    try:
        destination = out.resolve()
        # Atomic replacement replaces the final path entry, including a dangling symlink, rather than
        # following it. Guard that location as well as the symlink's resolved target.
        location = out.parent.resolve() / out.name
    except RuntimeError as error:
        raise ValueError(f"--out cannot be resolved: {error}") from error
    destinations = (destination, location)
    if destination in {p.resolve() for p in inputs}:
        raise ValueError("--out cannot overwrite a model, state, policy or target input")
    for path in paths:
        folder = static_part(path).resolve() if is_pattern(path) else path.resolve() if path.is_dir() else None
        if folder is not None and any(p.is_relative_to(folder) for p in destinations):
            raise ValueError("--out must be outside model folders and model glob prefixes")
    # A glob can traverse a symlink beyond its static prefix. Guard the real parent of each selected file.
    for path in paths:
        if is_pattern(path):
            for name in glob.glob(str(path)):
                if Path(name).is_file() and any(p.is_relative_to(Path(name).resolve().parent) for p in destinations):
                    raise ValueError("--out must be outside model folders and model glob prefixes")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=out.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(text)
        os.replace(temporary, out)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def positive_integer(value: str) -> int:
    """A run identifier or attempt must be an explicitly positive integer."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_seconds(value: str) -> float:
    """A bounded lock wait must be finite and explicitly nonnegative."""
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be finite nonnegative seconds") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("must be finite nonnegative seconds")
    return seconds


def positive_seconds(value: str) -> float:
    """One isolated invocation requires a finite positive time limit."""
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be finite positive seconds") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be finite positive seconds")
    return seconds


def isolation_document(path: Path, limit: int) -> Any:
    """Read bounded strict JSON without accepting duplicate keys or nonfinite values."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError(f"nonfinite JSON value: {value}")

    with path.open("rb") as stream:
        encoded = stream.read(limit + 1)
    if len(encoded) > limit:
        raise ValueError(f"{path} exceeds the permitted JSON document size")
    return json.loads(encoded.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)


def isolation_inputs(path: Path | None) -> dict[str, bytes]:
    """Decode explicitly supplied text snapshots; never discover or open candidate host paths."""
    if path is None:
        return {}
    from exacly import isolation

    document = isolation_document(path, 16 * 1024 * 1024)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "files"}
        or document["schema_version"] != "0.1.0"
        or not isinstance(document["files"], dict)
    ):
        raise ValueError("inputs must be a version 0.1.0 JSON document with an explicit files object")
    if len(document["files"]) > isolation.MAX_INPUT_FILES:
        raise ValueError("input snapshot exceeds the permitted file count")
    snapshots: dict[str, bytes] = {}
    total = 0
    for name, text in document["files"].items():
        if not isinstance(text, str):
            raise ValueError("input snapshot file values must be UTF-8 text strings")
        content = text.encode("utf-8")
        total += len(content)
        if total > isolation.MAX_INPUT_BYTES:
            raise ValueError("input snapshot exceeds the permitted byte count")
        snapshots[name] = content
    return snapshots


class GitHubCredentialsError(RuntimeError):
    """Credential setup failed before a live evidence read, rather than evidence being rejected."""


class LazyGitHubSource:
    """A retained completion can replay without credentials or a GitHub client."""

    def __init__(self) -> None:
        self._source: Source | None = None
        self._token: str | None = None

    def _client(self) -> Source:
        if self._source is None:
            from exacly import github_evidence

            token = os.environ.get("GH_TOKEN")
            if not token or not token.strip():
                token = os.environ.get("GITHUB_TOKEN")
            if not token or not token.strip():
                raise GitHubCredentialsError(
                    "set GH_TOKEN or GITHUB_TOKEN to authenticate GitHub Actions evidence retrieval"
                )
            self._token = token
            try:
                self._source = github_evidence.GitHubClient(token)
            except github_evidence.GitHubError:
                raise GitHubCredentialsError("GitHub credentials are invalid") from None
        return self._source

    def get_json(self, path: str) -> dict[str, Any]:
        return self._client().get_json(path)

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        return self._client().download_artifact(repository, artifact_id)

    def redact(self, error: Exception) -> str:
        return str(error).replace(self._token, "[redacted]") if self._token else str(error)


def write_admission(out: Path, inputs: Iterable[Path], trust_policy: dict[str, Any], result: AdmissionResult) -> None:
    """Publish one complete admission bundle in a new directory, preserving every input.

    The caller must validate trust_policy through admission.admit before invoking this helper. Model file
    declarations are remote Git paths; their corresponding local folders are excluded from output placement.
    """
    if os.path.lexists(out):
        raise ValueError("--out must be a new directory, not an existing path or symlink")
    try:
        destination = out.resolve()
        protected = {path.resolve() for path in inputs}
        folders = [Path(name).parent.resolve() for name in trust_policy["model"]["files"]]
    except RuntimeError as error:
        raise ValueError(f"--out or its protected input paths cannot be resolved: {error}") from error
    if destination in protected:
        raise ValueError("--out cannot overwrite the trust policy or target")
    if any(destination.is_relative_to(folder) for folder in folders):
        raise ValueError("--out must be outside model folders")
    if result.receipt.get("status") not in {"admitted", "rejected"}:
        raise ValueError("admission receipt has an invalid status")
    if result.receipt["status"] == "admitted" and (
        result.archive is None or result.report is None or result.state is None
    ):
        raise ValueError("admitted evidence must include its archive, report and state")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}-", dir=out.parent) as temporary:
        folder = Path(temporary)
        folder.joinpath("receipt.json").write_text(
            json.dumps(result.receipt, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        if result.receipt["status"] == "admitted":
            assert result.archive is not None and result.report is not None and result.state is not None
            folder.joinpath("archive.zip").write_bytes(result.archive)
            folder.joinpath("junit.xml").write_bytes(result.report)
            folder.joinpath("state.json").write_text(
                json.dumps(result.state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
            )
        # Recheck immediately before publication so an ordinary repeat invocation never replaces a bundle.
        if os.path.lexists(out):
            raise ValueError("--out must be a new directory, not an existing path or symlink")
        os.rename(folder, out)


def write_reconciliation(
    out: Path, inputs: Iterable[Path], trust_policy: dict[str, Any], report: dict[str, Any]
) -> None:
    """Save a preview outside its inputs and the validated trust policy's declared model folders.

    A declared parent remains protected when it does not exist locally. No default model folder is assumed:
    this command evaluates the remote model selected by the trust policy.
    """
    try:
        inputs = list(inputs)
        if out.exists() and any(path.exists() and out.samefile(path) for path in inputs):
            raise ValueError("--out cannot overwrite an input or its file alias")
        destinations = (out.resolve(), out.parent.resolve() / out.name)
        folders = [Path(name).parent.resolve() for name in trust_policy["model"]["files"]]
        if any(path.is_relative_to(folder) for path in destinations for folder in folders):
            raise ValueError("--out must be outside the trust policy's declared model folders")
        write_plan(out, [], inputs, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    except RuntimeError as error:
        raise ValueError(f"--out or its protected input paths cannot be resolved: {error}") from error


def execution_ledger_paths(ledger: Path) -> list[Path]:
    """The database and SQLite auxiliary paths that must remain outside input and model locations."""
    return [ledger, *(ledger.with_name(ledger.name + suffix) for suffix in ("-journal", "-wal", "-shm"))]


def protect_execution_copy(ledger: Path, out: Path) -> None:
    """Maintenance copies always use a new directory disjoint from the source and SQLite sidecars."""
    if os.path.lexists(out):
        raise ValueError("--out must be a new directory, not an existing path or symlink")
    try:
        destinations = (out.resolve(), out.parent.resolve() / out.name)
        for source in execution_ledger_paths(ledger):
            locations = (source.resolve(), source.parent.resolve() / source.name)
            if any(
                source_path.is_relative_to(destination) or destination.is_relative_to(source_path)
                for source_path in locations
                for destination in destinations
            ):
                raise ValueError("--out cannot overlap the source execution ledger or its auxiliary files")
    except RuntimeError as error:
        raise ValueError(f"execution copy paths cannot be resolved: {error}") from error


def protect_execution_ledger(ledger: Path, inputs: Iterable[Path], trust_policy: dict[str, Any]) -> None:
    """Reject unsafe registration destinations before the store can create or change any file."""
    try:
        names = trust_policy["model"]["files"]
    except (KeyError, TypeError):
        raise ValueError("trust policy must declare model files before ledger registration") from None
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise ValueError("trust policy must declare model files before ledger registration")
    try:
        inputs = list(inputs)
        protected = {location for path in inputs for location in (path.resolve(), path.parent.resolve() / path.name)}
        folders = [Path(name).parent.resolve() for name in names]
        for path in execution_ledger_paths(ledger):
            locations = (path.resolve(), path.parent.resolve() / path.name)
            if any(location in protected for location in locations) or (
                path.exists() and any(source.exists() and path.samefile(source) for source in inputs)
            ):
                raise ValueError(
                    "the execution ledger and its auxiliary files cannot overwrite inputs or their aliases"
                )
            if any(location.is_relative_to(folder) for location in locations for folder in folders):
                raise ValueError("the execution ledger must be outside the trust policy's declared model folders")
    except RuntimeError as error:
        raise ValueError(f"execution ledger or protected input paths cannot be resolved: {error}") from error


def protect_completion_paths(
    state: Path, ledger: Path, inputs: list[Path], trust_policy: dict[str, Any], out: Path | None
) -> None:
    """Check all completion destinations before the authorized state transition can start."""
    protect_execution_ledger(ledger, [state, *inputs], trust_policy)
    try:
        state_locations = (state.resolve(), state.parent.resolve() / state.name)
        protected = {location for path in inputs for location in (path.resolve(), path.parent.resolve() / path.name)}
        if any(location in protected for location in state_locations) or (
            state.exists() and any(path.exists() and state.samefile(path) for path in inputs)
        ):
            raise ValueError("--state cannot overwrite a plan, policy, trust policy or target input or its alias")
        if out is None:
            return
        if out.is_dir():
            raise ValueError("--out must name a report file, not a directory")
        destinations = (out.resolve(), out.parent.resolve() / out.name)
        report_inputs = [state, *inputs, *execution_ledger_paths(ledger)]
        protected.update(
            location for path in report_inputs for location in (path.resolve(), path.parent.resolve() / path.name)
        )
        if any(location in protected for location in destinations) or (
            out.exists() and any(path.exists() and out.samefile(path) for path in report_inputs)
        ):
            raise ValueError("--out cannot overwrite a completion input, state, ledger or its file alias")
        for path in execution_ledger_paths(ledger):
            for location in (path.resolve(), path.parent.resolve() / path.name):
                if any(destination.is_relative_to(location) for destination in destinations):
                    raise ValueError("--out cannot overlap the execution ledger or its auxiliary files")
        folders = [Path(name).parent.resolve() for name in trust_policy["model"]["files"]]
        if any(location.is_relative_to(folder) for location in destinations for folder in folders):
            raise ValueError("--out must be outside the trust policy's declared model folders")
        parent = out.parent.resolve()
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            raise ValueError("--out parent must be a directory")
    except RuntimeError as error:
        raise ValueError(f"completion paths cannot be resolved: {error}") from error


def exacly_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("validate", "refresh"):
        return main(argv[1:]) if argv[0] == "validate" else refresh_main(argv[1:])
    if argv and argv[0] == "observe":
        return observe_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="exacly",
        description="Validate an Exacly model, write its state, and ask it about its records.",
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
    commands.add_parser("validate", help="validate the model and print the section 11 report")
    commands.add_parser("refresh", help="write state for one context from what the bindings observe")
    commands.add_parser("observe", help="write state from an extension source, such as manual-qa observations")
    isolation_parser = commands.add_parser("isolation", help="inspect an explicitly selected runtime root filesystem")
    isolation_commands = isolation_parser.add_subparsers(dest="isolation_command", required=True)
    isolation_inspect = isolation_commands.add_parser(
        "inspect",
        help="calculate root filesystem content identity without approving or executing it",
        description="Calculate root filesystem content identity. The digest does not establish operator approval.",
    )
    isolation_inspect.add_argument(
        "--rootfs", required=True, type=Path, help="detached trusted runtime root filesystem"
    )
    isolation_inspect.add_argument("--json", action="store_true", help="print the content identity as JSON")
    isolate = commands.add_parser(
        "isolate",
        help="explicitly run one command within the approved isolation boundary",
        description=(
            "Run one command using an explicitly approved Linux runtime policy and immutable input snapshots. "
            "Candidate logs and artifacts are retained as untrusted output in a new directory. "
            "Isolation setup failure prevents candidate execution."
        ),
    )
    isolate.add_argument("--runtime-policy", required=True, type=Path, help="reviewed runtime policy JSON")
    isolate.add_argument("--inputs", type=Path, help="explicit versioned UTF-8 file snapshot JSON (default: no inputs)")
    isolate.add_argument("--out", required=True, type=Path, help="new private output bundle directory")
    isolate.add_argument(
        "--timeout",
        type=positive_seconds,
        help="finite positive invocation limit in seconds (default: 30; maximum: 3600)",
    )
    isolate.add_argument("--json", action="store_true", help="print the invocation report as JSON")
    isolate.add_argument("candidate", nargs=argparse.REMAINDER, metavar="COMMAND", help="candidate argv after --")
    execution_parser = commands.add_parser(
        "executions", help="register and inspect explicit task/action/output bindings"
    )
    execution_commands = execution_parser.add_subparsers(dest="execution_command", required=True)
    register = execution_commands.add_parser("register", help="explicitly register an operator-approved binding")
    register.add_argument(
        "--ledger", required=True, type=Path, help="private local execution ledger outside model folders"
    )
    register.add_argument("--plan", required=True, type=Path, help="saved verification plan YAML/JSON")
    register.add_argument("--action", required=True, help="action ID from the saved plan")
    register.add_argument("--state", required=True, type=Path, help="current state containing the exact pending item")
    register.add_argument("--task", required=True, help="exact pending task identifier")
    register.add_argument("--opened-at", required=True, help="exact pending opening timestamp with timezone")
    register.add_argument("--policy", required=True, type=Path, help="readiness policy YAML/JSON")
    register.add_argument(
        "--trust-policy", required=True, type=Path, help="approved producer and model policy YAML/JSON"
    )
    register.add_argument("--target", required=True, type=Path, help="operator-approved output implementation target")
    register.add_argument(
        "--date", required=True, type=dt.date.fromisoformat, help="explicit evaluation date YYYY-MM-DD"
    )
    register.add_argument(
        "--adopt-verification",
        action="store_true",
        help="explicitly adopt an unscoped legacy item as verification work",
    )
    register.add_argument("--supersedes", help="exact active binding ID to supersede with retained history")
    register.add_argument("--json", action="store_true", help="print the registered binding as JSON")
    register.add_argument(
        "--lock-timeout", type=nonnegative_seconds, help="maximum ledger lock wait in seconds (default: 30)"
    )
    execution_show = execution_commands.add_parser(
        "show", help="inspect an existing binding without changing the ledger"
    )
    execution_show.add_argument("--ledger", required=True, type=Path, help="existing private execution ledger")
    execution_show.add_argument("--binding", required=True, help="registered binding ID")
    execution_show.add_argument("--json", action="store_true", help="print the binding and current status as JSON")
    execution_show.add_argument(
        "--lock-timeout", type=nonnegative_seconds, help="maximum ledger lock wait in seconds (default: 30)"
    )
    for operation in ("inspect", "backup", "recover", "upgrade"):
        maintenance_parser = execution_commands.add_parser(
            operation,
            help={
                "inspect": "diagnose an existing ledger without changing it",
                "backup": "save a consistent ledger backup in a new directory",
                "recover": "recover into a new copy while preserving the source files",
                "upgrade": "upgrade into a new copy while preserving the original ledger",
            }[operation],
        )
        maintenance_parser.add_argument("--ledger", required=True, type=Path, help="existing private execution ledger")
        if operation != "inspect":
            maintenance_parser.add_argument("--out", required=True, type=Path, help="new private destination directory")
        maintenance_parser.add_argument(
            "--lock-timeout", type=nonnegative_seconds, help="maximum ledger lock wait in seconds (default: 30)"
        )
        maintenance_parser.add_argument("--json", action="store_true", help="print the maintenance report as JSON")
    for operation in ("index", "activate", "audit"):
        history_parser = execution_commands.add_parser(
            operation,
            help={
                "index": "create an audit-only indexed history copy without operational cutover",
                "activate": "create or refresh an operational v4 copy for explicit canonical selection",
                "audit": "fully revalidate an indexed history without changing it",
            }[operation],
            description={
                "index": (
                    "Create an audit-only indexed history copy. The source ledger is preserved; "
                    "the copy cannot be used by ordinary execution or completion commands, "
                    "and this command performs no operational cutover."
                ),
                "activate": (
                    "Fully validate a v2, v3 or v4 ledger and create an operational v4 copy, "
                    "including an explicit refresh of a known stale validator profile. The source is preserved. "
                    "Select one canonical ledger path explicitly before using the copy; no automatic cutover occurs."
                ),
                "audit": "Freshly validate the complete indexed history and its retained evidence without changing it.",
            }[operation],
        )
        history_parser.add_argument("--ledger", required=True, type=Path, help="existing private execution ledger")
        if operation != "audit":
            history_parser.add_argument("--out", required=True, type=Path, help="new private history-copy directory")
            history_parser.add_argument(
                "--dry-run", action="store_true", help="validate the source and destination without creating a copy"
            )
        history_parser.add_argument(
            "--lock-timeout", type=nonnegative_seconds, help="maximum ledger lock wait in seconds (default: 30)"
        )
        history_parser.add_argument("--json", action="store_true", help="print the history report as JSON")
    admit = commands.add_parser("admit", help="admit verification evidence from an authorized live source")
    admit.add_argument("source", choices=["github-actions"])
    admit.add_argument("--trust-policy", required=True, type=Path, help="trusted producer and model policy YAML/JSON")
    admit.add_argument("--target", required=True, type=Path, help="explicit implementation target YAML/JSON")
    admit.add_argument("--run", required=True, type=positive_integer, help="GitHub Actions run ID")
    admit.add_argument("--attempt", required=True, type=positive_integer, help="exact run attempt")
    admit.add_argument("--date", required=True, type=dt.date.fromisoformat, help="explicit evaluation date YYYY-MM-DD")
    admit.add_argument("--out", required=True, type=Path, help="new bundle directory outside model folders")
    admit.add_argument("--json", action="store_true", help="print the admission receipt as JSON")
    reconcile = commands.add_parser("reconcile", help="preview completion using fresh evidence without changing work")
    reconcile.add_argument("source", choices=["github-actions"])
    reconcile.add_argument(
        "--dry-run", required=True, action="store_true", help="explicitly request a read-only preview"
    )
    reconcile.add_argument("--plan", required=True, type=Path, help="saved verification plan YAML/JSON")
    reconcile.add_argument("--action", required=True, help="action ID from the saved plan")
    reconcile.add_argument("--policy", required=True, type=Path, help="readiness policy YAML/JSON")
    reconcile.add_argument(
        "--trust-policy", required=True, type=Path, help="trusted producer and model policy YAML/JSON"
    )
    reconcile.add_argument("--target", required=True, type=Path, help="output implementation target YAML/JSON")
    reconcile.add_argument("--run", required=True, type=positive_integer, help="GitHub Actions run ID")
    reconcile.add_argument("--attempt", required=True, type=positive_integer, help="exact run attempt")
    reconcile.add_argument(
        "--date", required=True, type=dt.date.fromisoformat, help="explicit evaluation date YYYY-MM-DD"
    )
    reconcile.add_argument("--json", action="store_true", help="print the reconciliation preview as JSON")
    reconcile.add_argument("--out", type=Path, help="save JSON outside declared model folders, preserving input files")
    reconcile.add_argument("--ledger", type=Path, help="existing private ledger for a registered binding")
    reconcile.add_argument("--binding", help="registered binding ID; requires --ledger and --state")
    reconcile.add_argument("--state", type=Path, help="current state to check against the registered pending item")
    reconcile.add_argument(
        "--lock-timeout", type=nonnegative_seconds, help="maximum registered-binding ledger lock wait in seconds"
    )
    complete = commands.add_parser(
        "complete", help="explicitly authorize local completion of one registered verification item"
    )
    complete.add_argument("source", choices=["github-actions"])
    complete.add_argument("--plan", required=True, type=Path, help="saved verification plan YAML/JSON")
    complete.add_argument("--action", required=True, help="action ID from the saved plan")
    complete.add_argument("--policy", required=True, type=Path, help="readiness policy YAML/JSON")
    complete.add_argument("--trust-policy", required=True, type=Path, help="approved producer and model policy")
    complete.add_argument("--target", required=True, type=Path, help="registered output implementation target")
    complete.add_argument("--run", required=True, type=positive_integer, help="GitHub Actions run ID")
    complete.add_argument("--attempt", required=True, type=positive_integer, help="exact run attempt")
    complete.add_argument("--date", required=True, type=dt.date.fromisoformat, help="evaluation date YYYY-MM-DD")
    complete.add_argument("--ledger", required=True, type=Path, help="existing private version 2 or 4 execution ledger")
    complete.add_argument("--binding", required=True, help="registered binding ID")
    complete.add_argument(
        "--state", required=True, type=Path, help="local state whose selected pending item to complete"
    )
    complete.add_argument(
        "--lock-timeout", type=nonnegative_seconds, help="maximum shared ledger and state lock wait (default: 30)"
    )
    complete.add_argument("--json", action="store_true", help="print the completion report as JSON")
    complete.add_argument("--out", type=Path, help="save JSON outside declared model folders, preserving input files")
    completions = commands.add_parser("completions", help="inspect or recover retained local completion attempts")
    completion_commands = completions.add_subparsers(dest="completion_command", required=True)
    for operation in ("show", "recover"):
        completion_parser = completion_commands.add_parser(
            operation,
            help="inspect a retained completion" if operation == "show" else "resolve one prepared local completion",
        )
        completion_parser.add_argument("--ledger", required=True, type=Path, help="existing private execution ledger")
        completion_parser.add_argument("--completion", required=True, help="retained completion ID")
        if operation == "recover":
            completion_parser.add_argument("--state", required=True, type=Path, help="original local state path")
        completion_parser.add_argument(
            "--lock-timeout", type=nonnegative_seconds, help="maximum shared ledger and state lock wait (default: 30)"
        )
        completion_parser.add_argument("--json", action="store_true", help="print the completion report as JSON")
    assess = commands.add_parser(
        "assess", parents=[common], help="assess a record for an explicit implementation target"
    )
    assess.add_argument("record")
    assess.add_argument("--target", required=True, type=Path, help="target YAML/JSON outside the model folder")
    assess.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    gate = commands.add_parser("gate", parents=[common], help="evaluate read-only readiness prerequisites")
    gate.add_argument("--policy", required=True, type=Path, help="policy YAML/JSON outside the model folder")
    gate.add_argument("--target", required=True, type=Path, help="explicit implementation target YAML/JSON")
    gate.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    plan = commands.add_parser("plan", parents=[common], help="propose read-only verification work for a target")
    plan.add_argument("--policy", required=True, type=Path, help="verification-only readiness policy YAML/JSON")
    plan.add_argument("--target", required=True, type=Path, help="explicit implementation target YAML/JSON")
    plan.add_argument("--date", required=True, type=dt.date.fromisoformat, help="explicit evaluation date YYYY-MM-DD")
    plan.add_argument("--out", type=Path, help="save JSON outside model folders, without changing input files")
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
    if args.command == "isolation":
        from exacly import isolation

        try:
            digest = isolation.rootfs_digest(args.rootfs)
            identity_report = {
                "schema_version": "0.1.0",
                "operation": "inspect-rootfs",
                "rootfs": str(args.rootfs.resolve()),
                "digest": digest,
                "scope": "content_identity",
            }
        except (ValueError, OSError) as error:
            print(f"exacly isolation: {error}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(identity_report, indent=2, sort_keys=True))
        else:
            print(f"Root filesystem: {identity_report['rootfs']}\nContent digest: {digest}")
            print("Content identity only; operator approval is supplied separately.")
        return 0
    if args.command == "isolate":
        from exacly import isolation

        try:
            candidate = args.candidate[1:] if args.candidate[:1] == ["--"] else args.candidate
            if not candidate:
                raise ValueError("supply one candidate command after --")
            runtime = isolation.parse_policy(isolation_document(args.runtime_policy, 64 * 1024))
            snapshots = isolation_inputs(args.inputs)
            options = {} if args.timeout is None else {"timeout": args.timeout}
            report = isolation.run(runtime, candidate, snapshots, args.out, **options)
            if (
                not isinstance(report, dict)
                or report.get("schema_version") != "0.1.0"
                or report.get("backend") != "linux-bubblewrap-v1"
                or report.get("status") not in {"completed", "candidate_failed", "timed_out", "output_limit"}
                or type(report.get("exit_code")) is not int
                or (report["status"] == "completed" and report["exit_code"] != 0)
                or (report["status"] == "candidate_failed" and report["exit_code"] == 0)
                or not isinstance(report.get("isolation"), dict)
                or report["isolation"].get("verified") is not True
            ):
                raise ValueError("isolation runtime returned an invalid report")
            rendered = json.dumps(report, indent=2, sort_keys=True) if args.json else isolation.as_text(report)
        except (ValueError, OSError) as error:
            print(f"exacly isolate: {error}", file=sys.stderr)
            return 2
        print(rendered)
        return 0 if report["status"] == "completed" else 1
    if args.command == "executions":
        if args.execution_command in {"index", "activate", "audit"}:
            from exacly import history_maintenance

            try:
                options = {} if args.lock_timeout is None else {"lock_timeout": args.lock_timeout}
                if args.execution_command == "index":
                    protect_execution_copy(args.ledger, args.out)
                    answer = history_maintenance.convert(args.ledger, args.out, dry_run=args.dry_run, **options)
                    expected_statuses = {"ready" if args.dry_run else "indexed"}
                elif args.execution_command == "activate":
                    protect_execution_copy(args.ledger, args.out)
                    answer = history_maintenance.activate(args.ledger, args.out, dry_run=args.dry_run, **options)
                    expected_statuses = {"ready" if args.dry_run else "activated"}
                else:
                    answer = history_maintenance.audit(args.ledger, **options)
                    expected_statuses = {"passed", "corrupt", "unsupported", "recovery_required"}
                if not isinstance(answer, dict) or not jsonschema.Draft202012Validator(
                    history_maintenance.SCHEMA, format_checker=state_writer.FORMATS
                ).is_valid(answer):
                    raise ValueError("execution history returned an invalid report")
                if answer.get("operation") != args.execution_command or answer.get("status") not in expected_statuses:
                    raise ValueError("execution history returned an invalid operation or status")
                rendered = (
                    json.dumps(answer, indent=2, sort_keys=True, default=str)
                    if args.json
                    else history_maintenance.as_text(answer)
                )
            except (query.QueryError, ValueError, OSError) as error:
                print(f"exacly executions: {error}", file=sys.stderr)
                return 2
            print(rendered)
            return 0 if answer["status"] in {"ready", "indexed", "activated", "passed"} else 1
        if args.execution_command in {"inspect", "backup", "recover", "upgrade"}:
            import exacly.execution_maintenance as execution_maintenance

            try:
                options = {} if args.lock_timeout is None else {"lock_timeout": args.lock_timeout}
                if args.execution_command == "inspect":
                    answer = execution_maintenance.inspect(args.ledger, **options)
                    if answer.get("status") not in {"clean", "recovery_required", "unsupported", "corrupt"}:
                        raise ValueError("execution inspection returned an invalid status")
                else:
                    protect_execution_copy(args.ledger, args.out)
                    maintenance_action = getattr(execution_maintenance, args.execution_command)
                    answer = maintenance_action(args.ledger, args.out, **options)
                    expected_status = {"backup": "backed_up", "recover": "recovered", "upgrade": "upgraded"}[
                        args.execution_command
                    ]
                    if answer.get("status") != expected_status:
                        raise ValueError(f"execution {args.execution_command} returned an invalid status")
                v4_report = answer.get("schema_version") == "0.3.0" or (
                    isinstance(answer.get("store"), dict) and answer["store"].get("user_version") == 4
                )
                if v4_report and not jsonschema.Draft202012Validator(
                    execution_maintenance.SCHEMA_V4, format_checker=state_writer.FORMATS
                ).is_valid(answer):
                    raise ValueError("version 4 execution maintenance returned an invalid report")
            except (query.QueryError, ValueError, OSError) as error:
                print(f"exacly executions: {error}", file=sys.stderr)
                return 2
            print(
                json.dumps(answer, indent=2, sort_keys=True, default=str)
                if args.json
                else execution_maintenance.as_text(answer)
            )
            return 0 if args.execution_command != "inspect" or answer["status"] == "clean" else 1
        import exacly.executions as executions

        try:
            if args.execution_command == "show":
                if args.lock_timeout is None:
                    answer = executions.get(args.ledger, args.binding)
                else:
                    answer = executions.get(args.ledger, args.binding, lock_timeout=args.lock_timeout)
            else:
                plan_doc, state_doc = read(args.plan), read(args.state)
                readiness_policy, trust_policy = read(args.policy), read(args.trust_policy)
                target = Target(read(args.target))
                protect_execution_ledger(
                    args.ledger, [args.plan, args.state, args.policy, args.trust_policy, args.target], trust_policy
                )
                request = executions.Registration(
                    plan=plan_doc,
                    action_id=args.action,
                    state=state_doc,
                    task=args.task,
                    opened_at=args.opened_at,
                    readiness_policy=readiness_policy,
                    trust_policy=trust_policy,
                    target=target,
                    date=args.date,
                    adopt_verification=args.adopt_verification,
                    supersedes=args.supersedes,
                )
                if args.lock_timeout is None:
                    answer = executions.register(args.ledger, request)
                else:
                    answer = executions.register(args.ledger, request, lock_timeout=args.lock_timeout)
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly executions: {error}", file=sys.stderr)
            return 2
        print(json.dumps(answer, indent=2, sort_keys=True, default=str) if args.json else executions.as_text(answer))
        return 0
    if args.command == "completions":
        import exacly.completion as completion

        try:
            completion_options: dict[str, Any] = {}
            if args.lock_timeout is not None:
                completion_options["lock_timeout"] = args.lock_timeout
            if args.completion_command == "recover":
                report = completion.recover(args.ledger, args.completion, state_path=args.state, **completion_options)
            else:
                report = completion.show(args.ledger, args.completion, **completion_options)
            if report.get("status") not in {"completed", "pending", "stale", "prepared", "aborted", "conflict"}:
                raise ValueError("completion returned an invalid status")
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly completions: {error}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else completion.as_text(report))
        return 0 if args.completion_command == "show" or report["status"] in {"completed", "aborted"} else 1
    if args.command == "complete":
        import exacly.completion as completion

        lazy_source = LazyGitHubSource()
        try:
            plan_doc, readiness_policy = read(args.plan), read(args.policy)
            trust_policy = read(args.trust_policy)
            target = Target(read(args.target))
            completion_inputs = [args.plan, args.policy, args.trust_policy, args.target]
            protect_completion_paths(args.state, args.ledger, completion_inputs, trust_policy, args.out)
            complete_options: dict[str, Any] = {
                "ledger": args.ledger,
                "binding_id": args.binding,
                "state_path": args.state,
            }
            if args.lock_timeout is not None:
                complete_options["lock_timeout"] = args.lock_timeout
            report = completion.complete(
                lazy_source,
                plan_doc,
                args.action,
                readiness_policy,
                trust_policy,
                target,
                args.run,
                args.attempt,
                args.date,
                **complete_options,
            )
            if report.get("status") not in {"completed", "pending", "stale", "prepared", "aborted", "conflict"}:
                raise ValueError("completion returned an invalid status")
            if args.out is not None:
                try:
                    write_reconciliation(
                        args.out,
                        [*completion_inputs, args.state, *execution_ledger_paths(args.ledger)],
                        trust_policy,
                        report,
                    )
                except (ValueError, OSError) as error:
                    if report["status"] == "completed":
                        completion_id = report.get("completion_id")
                        inspect_command = shlex.join(
                            [
                                "exacly",
                                "completions",
                                "show",
                                "--ledger",
                                str(args.ledger),
                                "--completion",
                                str(completion_id),
                            ]
                        )
                        raise ValueError(
                            f"completion {completion_id} is already recorded; --out report could not be written: "
                            f"{error}. Inspect the retained receipt with {inspect_command}"
                        ) from error
                    raise
        except (query.QueryError, ValueError, OSError, GitHubCredentialsError) as error:
            print(f"exacly complete: {lazy_source.redact(error)}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else completion.as_text(report))
        return 0 if report["status"] == "completed" else 1
    if args.command == "reconcile":
        from exacly import github_evidence, reconciliation

        binding_options = (args.ledger, args.binding, args.state)
        if any(value is not None for value in binding_options) and not all(
            value is not None for value in binding_options
        ):
            parser.error("--ledger, --binding and --state must be supplied together")
        if args.lock_timeout is not None and args.ledger is None:
            parser.error("--lock-timeout requires a registered binding with --ledger, --binding and --state")
        token = os.environ.get("GH_TOKEN")
        if not token or not token.strip():
            token = os.environ.get("GITHUB_TOKEN")
        try:
            plan_doc, readiness_policy = read(args.plan), read(args.policy)
            trust_policy = read(args.trust_policy)
            target = Target(read(args.target))
            if not token or not token.strip():
                raise ValueError("set GH_TOKEN or GITHUB_TOKEN to authenticate GitHub Actions evidence retrieval")
            try:
                source = github_evidence.GitHubClient(token)
            except github_evidence.GitHubError:
                raise ValueError("GitHub credentials are invalid") from None
            preview_args = (
                source,
                plan_doc,
                args.action,
                readiness_policy,
                trust_policy,
                target,
                args.run,
                args.attempt,
                args.date,
            )
            if args.ledger is None:
                report = reconciliation.preview(*preview_args)
            else:
                binding_kwargs = {"ledger": args.ledger, "binding_id": args.binding, "state_doc": read(args.state)}
                if args.lock_timeout is not None:
                    binding_kwargs["lock_timeout"] = args.lock_timeout
                report = reconciliation.preview(*preview_args, **binding_kwargs)
            if report.get("status") not in {"ready", "pending", "stale"}:
                raise ValueError("reconciliation preview has an invalid status")
            if args.out is not None:
                protected_inputs = [args.plan, args.policy, args.trust_policy, args.target]
                if args.ledger is not None:
                    protected_inputs.extend([args.state, *execution_ledger_paths(args.ledger)])
                write_reconciliation(args.out, protected_inputs, trust_policy, report)
        except (query.QueryError, ValueError, OSError) as error:
            message = str(error).replace(token, "[redacted]") if token and token.strip() else str(error)
            print(f"exacly reconcile: {message}", file=sys.stderr)
            return 2
        print(
            json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else reconciliation.as_text(report)
        )
        return 0 if report["status"] == "ready" else 1
    if args.command == "admit":
        from exacly import admission, github_evidence

        token = os.environ.get("GH_TOKEN")
        if not token or not token.strip():
            token = os.environ.get("GITHUB_TOKEN")
        try:
            if os.path.lexists(args.out):
                raise ValueError("--out must be a new directory, not an existing path or symlink")
            trust_policy = read(args.trust_policy)
            target = Target(read(args.target))
            if not token or not token.strip():
                raise ValueError("set GH_TOKEN or GITHUB_TOKEN to authenticate GitHub Actions evidence retrieval")
            try:
                source = github_evidence.GitHubClient(token)
            except github_evidence.GitHubError:
                raise ValueError("GitHub credentials are invalid") from None
            result = admission.admit(source, trust_policy, target, args.run, args.attempt, args.date)
            write_admission(args.out, [args.trust_policy, args.target], trust_policy, result)
        except (query.QueryError, ValueError, OSError) as error:
            message = str(error).replace(token, "[redacted]") if token and token.strip() else str(error)
            print(f"exacly admit: {message}", file=sys.stderr)
            return 2
        print(
            json.dumps(result.receipt, indent=2, sort_keys=True, default=str)
            if args.json
            else admission.as_text(result.receipt)
        )
        return 0 if result.receipt["status"] == "admitted" else 1
    if args.command == "plan":
        try:
            paths = args.model or [DEFAULT_MODEL]
            documents = load(paths)
            answer = planning.create(documents, read(args.policy), Target(read(args.target)), args.date)
            saved = json.dumps(answer, indent=2, sort_keys=True, default=str) + "\n"
            if args.out is not None:
                write_plan(args.out, paths, [*(Path(name) for name in documents), args.policy, args.target], saved)
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly plan: {error}", file=sys.stderr)
            return 2
        print(saved if args.json else planning.as_text(answer), end="" if args.json else "\n")
        return 0
    if args.command == "gate":
        try:
            target = Target(read(args.target))
            model = query.load(load(args.model or [DEFAULT_MODEL]), args.date)
            answer = policy.evaluate(model, read(args.policy), target)
        except (query.QueryError, ValueError) as error:
            print(f"exacly gate: {error}", file=sys.stderr)
            return 2
        print(json.dumps(answer, indent=2, default=str) if args.json else policy.as_text(answer))
        return 0 if answer["result"] == "satisfied" else 1
    if args.command == "assess":
        try:
            target = Target(read(args.target))
            model = query.load(load(args.model or [DEFAULT_MODEL]), args.date)
            answer = query.assess(model, args.record, target)
        except (query.QueryError, ValueError) as error:
            print(f"exacly assess: {error}", file=sys.stderr)
            return 2
        print(json.dumps(answer, indent=2, default=str) if args.json else assessment_text(answer))
        return 0
    if args.command == "view":
        try:
            return view_main(args)
        except query.QueryError as error:
            print(f"exacly: {error}", file=sys.stderr)
            return 1
    if args.command == "mcp":
        mcp.serve(mcp.Server(args.model or [DEFAULT_MODEL], ask, metadata.version("exacly")))
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


def renamed(command: str, run: Callable[[list[str] | None], int]) -> Callable[[], int]:
    """The command under its name before 0.2.0, which says what to run instead."""

    def run_renamed() -> int:
        print(f"exacly-{command} is now exacly {command}; the old name goes in the next release.", file=sys.stderr)
        return run(None)

    return run_renamed


validate_renamed = renamed("validate", main)
refresh_renamed = renamed("refresh", refresh_main)


def assessment_text(answer: dict[str, Any]) -> str:
    """A scoped result with the reasons observations did not count."""
    lines = [
        label(answer["record"]),
        f"Target: {json.dumps(answer['target'], sort_keys=True)}",
        f"As of: {answer['as_of']}",
    ]
    lines += [f"{dimension}: {state}" for dimension, state in answer["status"].items()]
    lines += [
        f"Excluded {e['assessment']['dimension']} from {e['assessment'].get('source', 'legacy')}: {e['reason']}"
        for e in answer["excluded"]
    ]
    lines += [
        f"Missing {m['dimension']} from {m['source']} {json.dumps(m.get('binding', {}), sort_keys=True)}"
        for m in answer["missing"]
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(exacly_main())
