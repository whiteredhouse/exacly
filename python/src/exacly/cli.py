"""Command lines.

exacly validate PATH... [--revision REV] [--date YYYY-MM-DD] [--json]
exacly refresh PATH... --context CONTEXT --state FILE [--junit FILE...] [--distribution] [run options]
exacly observe manual-qa PATH... --target FILE --state FILE [--revision REV] [--root DIR]
exacly evidence package --report FILE --out NEWDIR [--json]
exacly evidence propose-policy github-actions --repository OWNER/REPO --revision SHA --proposal FILE ...
exacly admit github-actions --trust-policy FILE --target FILE --run ID --attempt N --date YYYY-MM-DD --out DIR
exacly composition validate --definition FILE --date YYYY-MM-DD [--json]
exacly compose github-actions --definition FILE --selection FILE --date DATE --out NEWDIR [--readiness-junit] [--json]
exacly reconcile github-actions --dry-run --plan FILE --action ID --policy FILE --trust-policy FILE --target FILE ...
exacly complete github-actions --plan FILE --action ID --policy FILE --trust-policy FILE --target FILE ...
exacly completions show|recover --ledger FILE --completion ID [--state FILE] [--json]
exacly dispatch github-issues --plan FILE --action ID --dispatch-policy FILE --state FILE ... [--dry-run|--apply]
exacly dispatches init|show|recover|recover-journal|resolve|supersede --journal FILE ... [--json]
exacly execute local --plan FILE --action ID --executor-policy FILE --binding ID ... [--dry-run|--apply]
exacly executors init|show|cancel|recover|recover-journal --journal FILE ... [--json]
exacly handoff local-git --request FILE [--dry-run|--apply] [--authorization FILE] [--json]
exacly handoffs init-repository|init|show|recover|recover-journal ... [--json]
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
exacly implementation propose -m PATH... --intent FILE --policy FILE --target FILE --date DATE ... --out NEWDIR
exacly implementation check -m PATH... --intent FILE --policy FILE --target FILE --date DATE ... --bundle DIR
exacly delivery key-init --key FILE [--json]
exacly delivery init|show|revoke --journal FILE ... [--json]
exacly delivery merge|publish --journal FILE --journal-id UUID --request FILE --payload FILE ... [--apply]
exacly delivery deploy|rollback --journal FILE --journal-id UUID --request FILE --payload FILE ... [--apply]
exacly delivery reconcile --journal FILE --journal-id UUID --operation-key DIGEST --policy FILE --config FILE ...
exacly gate [-m PATH]... --policy FILE --target FILE [--date YYYY-MM-DD] [--json] [--junit FILE --junit-id ID]
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
from typing import TYPE_CHECKING, Any, NoReturn

import jsonschema
import yaml

from exacly import manual_qa, mcp, planning, policy, query, state_writer, views
from exacly.applicability import Target
from exacly.core import binding_words, bindings, identity, is_state, validate_documents
from exacly.refresh import RefreshError, Run, check_distribution_identity, read_junit, refresh, unattributed

if TYPE_CHECKING:
    from exacly.admission import AdmissionResult, Source
    from exacly.github_dispatch import GitHubIssueAdapter

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


def write_derived(out: Path, paths: list[Path], inputs: Iterable[Path], text: str, *, option: str = "--out") -> None:
    """Save a derived artifact outside its inputs and model folders, replacing it atomically.

    A glob's static prefix is treated as a model folder too: a later recursive model read must not
    accidentally load a saved report. Resolving paths also protects inputs accessed through symlink aliases.
    """
    try:
        destination = out.resolve()
        # Atomic replacement replaces the final path entry, including a dangling symlink, rather than
        # following it. Guard that location as well as the symlink's resolved target.
        location = out.parent.resolve() / out.name
        inputs = list(inputs)
        if destination in {p.resolve() for p in inputs} or (
            out.exists() and any(p.exists() and out.samefile(p) for p in inputs)
        ):
            raise ValueError(f"{option} cannot overwrite a model, state, policy or target input or its alias")
    except RuntimeError as error:
        raise ValueError(f"{option} cannot be resolved: {error}") from error
    destinations = (destination, location)
    for path in paths:
        folder = static_part(path).resolve() if is_pattern(path) else path.resolve() if path.is_dir() else None
        if folder is not None and any(p.is_relative_to(folder) for p in destinations):
            raise ValueError(f"{option} must be outside model folders and model glob prefixes")
    # A glob can traverse a symlink beyond its static prefix. Guard the real parent of each selected file.
    for path in paths:
        if is_pattern(path):
            for name in glob.glob(str(path)):
                if Path(name).is_file() and any(p.is_relative_to(Path(name).resolve().parent) for p in destinations):
                    raise ValueError(f"{option} must be outside model folders and model glob prefixes")
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


def strict_json_document(path: Path, limit: int) -> Any:
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
    try:
        return json.loads(encoded.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except RecursionError as error:
        raise ValueError(f"{path} exceeds the permitted JSON nesting depth") from error


def isolation_inputs(path: Path | None) -> dict[str, bytes]:
    """Decode explicitly supplied text snapshots; never discover or open candidate host paths."""
    if path is None:
        return {}
    from exacly import isolation

    document = strict_json_document(path, 16 * 1024 * 1024)
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
    """Credential setup failed before a live GitHub operation."""


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


class LazyGitHubIssueAdapter:
    """Local dispatch replay does not require credentials or construct a remote client."""

    def __init__(self) -> None:
        self._adapter: GitHubIssueAdapter | None = None

    def _client(self) -> GitHubIssueAdapter:
        if self._adapter is None:
            from exacly import github_dispatch

            token = os.environ.get("GH_TOKEN")
            if not token or not token.strip():
                token = os.environ.get("GITHUB_TOKEN")
            if not token or not token.strip():
                raise GitHubCredentialsError("set GH_TOKEN or GITHUB_TOKEN to authenticate GitHub issue dispatch")
            try:
                self._adapter = github_dispatch.GitHubIssueAdapter(token)
            except github_dispatch.GitHubDispatchError:
                raise GitHubCredentialsError("GitHub credentials are invalid") from None
        return self._adapter

    def repository(self, repository: dict[str, Any]) -> dict[str, Any]:
        return self._client().repository(repository)

    def discover(self, repository: dict[str, Any], marker: str, effect: dict[str, Any]) -> list[dict[str, Any]]:
        return self._client().discover(repository, marker, effect)

    def create(self, repository: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
        return self._client().create(repository, effect)


def redact_tokens(message: str, tokens: Iterable[str | None]) -> str:
    """Keep both GitHub credential variables out of even local input and output diagnostics."""
    for token in sorted({token for token in tokens if token and token.strip()}, key=len, reverse=True):
        message = message.replace(token, "[redacted]")
    return message


def protect_policy_candidate(out: Path, proposal: Path, model_files: list[str]) -> None:
    """A candidate is a new file outside explicit remote-model parents and the local proposal input."""
    if os.path.lexists(out):
        raise ValueError("--out must be a new file, not an existing path or symlink")
    try:
        destinations = (out.resolve(), out.parent.resolve() / out.name)
        protected = (proposal.resolve(), proposal.parent.resolve() / proposal.name)
        if any(destination in protected for destination in destinations):
            raise ValueError("--out cannot overwrite the proposal input or its alias")
        folders = [Path(name).parent.resolve() for name in model_files]
        if any(destination.is_relative_to(folder) for destination in destinations for folder in folders):
            raise ValueError("--out must be outside the declared model folders")
        parent = out.parent.resolve()
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            raise ValueError("--out parent must be a directory")
    except RuntimeError as error:
        raise ValueError(f"--out or its protected input paths cannot be resolved: {error}") from error


def write_policy_candidate(out: Path, proposal: Path, model_files: list[str], text: str) -> None:
    """Publish a closed, private candidate file atomically without replacing any concurrent output."""
    protect_policy_candidate(out, proposal, model_files)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{out.name}-", dir=out.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(text)
        protect_policy_candidate(out, proposal, model_files)
        os.link(temporary, out)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_admission(out: Path, inputs: Iterable[Path], trust_policy: dict[str, Any], result: AdmissionResult) -> None:
    """Publish one complete admission bundle in a new directory, preserving every input.

    The caller must validate trust_policy through admission.admit before invoking this helper. Model file
    declarations are remote Git paths; their corresponding local folders are excluded from output placement.
    """
    from exacly.evidence_retention import admission_bundle, require_private_filesystem

    require_private_filesystem()
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
    admission_bundle(out, result)


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
        write_derived(out, [], inputs, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
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


def protect_dispatch_paths(
    state: Path,
    journal: Path,
    ledger: Path,
    inputs: list[Path],
    trust_policy: dict[str, Any],
) -> None:
    """Keep both stores and their SQLite sidecars disjoint from state and reviewed inputs."""
    protect_completion_paths(state, ledger, [*inputs, *execution_ledger_paths(journal)], trust_policy, None)
    protect_execution_ledger(journal, [state, *inputs, *execution_ledger_paths(ledger)], trust_policy)


def protect_dispatch_resolution_authorization(authorization: Path, journal: Path, inspection: dict[str, Any]) -> None:
    """Do not interpret a retained resource or its file alias as new resolution authorization."""
    protected = execution_ledger_paths(journal)
    record = inspection.get("record")
    if record is not None:
        try:
            intent = record["intent"]
            protected.extend(execution_ledger_paths(Path(intent["paths"]["ledger"])))
            protected.append(Path(intent["paths"]["state"]))
            protected.extend(Path(name) for name in intent["contract"]["trust_policy"]["model"]["files"])
        except (KeyError, TypeError) as error:
            raise ValueError("dispatch inspection did not retain its protected resource paths") from error
    try:
        selected = (authorization.resolve(), authorization.parent.resolve() / authorization.name)
        for source in protected:
            locations = (source.resolve(), source.parent.resolve() / source.name)
            if any(destination in locations for destination in selected) or (
                authorization.exists() and source.exists() and authorization.samefile(source)
            ):
                raise ValueError("--authorization cannot alias a dispatch journal, state, ledger or model resource")
    except RuntimeError as error:
        raise ValueError(f"resolution authorization or retained paths cannot be resolved: {error}") from error


def protect_executor_initialization(journal: Path, ledger: Path) -> None:
    """The new companion and its sidecars cannot alias the retained binding ledger."""
    try:
        for destination in execution_ledger_paths(journal):
            selected = (destination.resolve(), destination.parent.resolve() / destination.name)
            for source in execution_ledger_paths(ledger):
                retained = (source.resolve(), source.parent.resolve() / source.name)
                if any(path in retained for path in selected) or (
                    destination.exists() and source.exists() and destination.samefile(source)
                ):
                    raise ValueError("executor companion ledger and binding ledger or their sidecars cannot alias")
    except RuntimeError as error:
        raise ValueError(f"executor initialization paths cannot be resolved: {error}") from error


def protect_executor_paths(
    state: Path,
    journal: Path | None,
    ledger: Path | None,
    inputs: list[Path],
    trust_policy: dict[str, Any],
    executor_policy: Any,
) -> None:
    """Check reviewed storage and output destinations before any executor mutation."""
    try:
        storage = executor_policy["storage"]
        if not isinstance(storage, dict) or not all(
            isinstance(storage.get(key), str) for key in ("journal", "ledger", "state", "output_root")
        ):
            raise ValueError("executor policy must declare its journal, ledger, state and output root")
        selected_journal, selected_ledger = Path(storage["journal"]), Path(storage["ledger"])
    except (KeyError, TypeError):
        raise ValueError("executor policy must declare its journal, ledger, state and output root") from None
    try:
        if journal is not None and journal.resolve() != selected_journal.resolve():
            raise ValueError("--journal differs from the reviewed executor storage")
        if ledger is not None and ledger.resolve() != selected_ledger.resolve():
            raise ValueError("--ledger differs from the reviewed executor storage")
        if state.resolve() != Path(storage["state"]).resolve():
            raise ValueError("--state differs from the reviewed executor storage")
        protect_dispatch_paths(state, selected_journal, selected_ledger, inputs, trust_policy)
        output_root = Path(storage["output_root"]).resolve()
        protected = [
            state,
            *inputs,
            *execution_ledger_paths(selected_journal),
            *execution_ledger_paths(selected_ledger),
        ]
        for source in protected:
            locations = (source.resolve(), source.parent.resolve() / source.name)
            if any(output_root.is_relative_to(path) or path.is_relative_to(output_root) for path in locations):
                raise ValueError("executor output root cannot overlap reviewed inputs, state or ledgers")
        folders = [Path(name).parent.resolve() for name in trust_policy["model"]["files"]]
        if any(output_root.is_relative_to(folder) or folder.is_relative_to(output_root) for folder in folders):
            raise ValueError("executor output root must be outside the trust policy's declared model folders")
        runtime = executor_policy.get("runtime")
        if isinstance(runtime, dict) and isinstance(runtime.get("rootfs"), str):
            rootfs = Path(runtime["rootfs"]).resolve()
            if output_root.is_relative_to(rootfs) or rootfs.is_relative_to(output_root):
                raise ValueError("executor output root and runtime root filesystem cannot overlap")
    except RuntimeError as error:
        raise ValueError(f"executor or protected input paths cannot be resolved: {error}") from error


def exacly_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("validate", "refresh"):
        return main(argv[1:]) if argv[0] == "validate" else refresh_main(argv[1:])
    if argv and argv[0] == "observe":
        return observe_main(argv[1:])
    github_tokens = (os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN"))
    delivery_command_line = argv[:1] == ["delivery"]

    class PolicyDraftParser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            tokens = (
                (*github_tokens, os.environ.get("EXACLY_DELIVERY_MERGE_TOKEN"))
                if delivery_command_line
                else github_tokens
            )
            super().error(redact_tokens(message, tokens))

    parser_class = (
        PolicyDraftParser
        if argv[:2] == ["evidence", "propose-policy"]
        or argv[:1]
        in (
            ["dispatch"],
            ["dispatches"],
            ["execute"],
            ["executors"],
            ["handoff"],
            ["handoffs"],
            ["composition"],
            ["compose"],
            ["implementation"],
            ["delivery"],
        )
        else argparse.ArgumentParser
    )
    parser = parser_class(
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
    delivery_parser = commands.add_parser(
        "delivery", help="inspect or separately authorize one exact product-delivery effect"
    )
    delivery_commands = delivery_parser.add_subparsers(dest="delivery_command", required=True)
    for delivery_command in (
        "key-init",
        "init",
        "show",
        "revoke",
        "merge",
        "publish",
        "deploy",
        "rollback",
        "reconcile",
    ):
        delivery_command_parser = delivery_commands.add_parser(
            delivery_command,
            help={
                "key-init": "initialize a new private merge-marker key without granting delivery authority",
                "init": "initialize one private delivery journal and immutable objective limits",
                "show": "read retained delivery history without credentials or a provider",
                "revoke": "record scope withdrawal; cannot undo or cancel an in-flight remote request",
                "merge": "preview or separately apply the fixed GitHub atomic merge profile",
                "publish": "preview or separately apply the fixed private owning publisher profile",
                "deploy": "preview or separately apply an exact artifact to the fixed static environment",
                "rollback": "preview or separately apply explicitly bounded static rollback authority",
                "reconcile": "discover a retained uncertain outcome without resending the effect",
            }[delivery_command],
        )
        delivery_command_parser.add_argument("--json", action="store_true", help="print the native report as JSON")
        delivery_command_parser.add_argument("--lock-timeout", type=nonnegative_seconds)
        if delivery_command == "key-init":
            delivery_command_parser.add_argument(
                "--key", required=True, type=Path, help="new private delivery key file"
            )
            continue
        delivery_command_parser.add_argument("--journal", required=True, type=Path)
        if delivery_command == "init":
            for name in ("objective", "limits", "native-limits"):
                delivery_command_parser.add_argument(f"--{name}", required=True, type=Path)
            continue
        delivery_command_parser.add_argument("--journal-id", required=True, help="explicit initialized journal UUID")
        if delivery_command == "show":
            delivery_command_parser.add_argument("--operation-key", help="select one retained operation")
            continue
        if delivery_command == "revoke":
            delivery_command_parser.add_argument("--reason", required=True, help="retained reason for scope withdrawal")
            continue
        delivery_command_parser.add_argument(
            "--config", required=True, type=Path, help="operator-selected installed adapter config"
        )
        delivery_command_parser.add_argument("--policy", required=True, type=Path, help="approved exact stage policy")
        for name in ("approval",):
            delivery_command_parser.add_argument(f"--{name}", type=Path)
        delivery_command_parser.add_argument(
            "--approved-approval-digest", help="approval digest selected outside its JSON"
        )
        delivery_command_parser.add_argument("--issuer", help="independently selected approval issuer")
        delivery_command_parser.add_argument("--approval-id", help="independently selected approval identity")
        if delivery_command == "reconcile":
            delivery_command_parser.add_argument("--operation-key", required=True)
            delivery_command_parser.set_defaults(apply=False)
        else:
            delivery_command_parser.add_argument("--request", required=True, type=Path)
            delivery_command_parser.add_argument(
                "--payload", required=True, type=Path, help="exact native stage payload"
            )
            delivery_command_parser.add_argument(
                "--apply",
                action="store_true",
                help="separately authorize a new effect; default only previews local inputs",
            )
    implementation_parser = commands.add_parser(
        "implementation", help="propose product implementation work under separately reviewed planning scope"
    )
    implementation_commands = implementation_parser.add_subparsers(dest="implementation_command", required=True)
    for implementation_command in ("propose", "check"):
        implementation_command_parser = implementation_commands.add_parser(
            implementation_command,
            help=(
                "retain an immutable advisory proposal and its input snapshots"
                if implementation_command == "propose"
                else "check a complete proposal bundle against currently selected inputs"
            ),
        )
        implementation_command_parser.add_argument(
            "-m",
            "--model",
            required=True,
            action="append",
            type=Path,
            metavar="PATH",
            help="explicit model files/folders",
        )
        implementation_command_parser.add_argument("--intent", required=True, type=Path)
        implementation_command_parser.add_argument("--policy", required=True, type=Path)
        implementation_command_parser.add_argument("--target", required=True, type=Path)
        implementation_command_parser.add_argument("--date", required=True, type=dt.date.fromisoformat)
        implementation_command_parser.add_argument(
            "--approved-intent-digest", required=True, help="intent content digest selected by independent review"
        )
        implementation_command_parser.add_argument(
            "--approved-policy-digest", required=True, help="planning-policy digest selected by independent review"
        )
        implementation_command_parser.add_argument("--issuer", required=True, help="independent planning approver")
        implementation_command_parser.add_argument("--approval-id", required=True, help="planning approval identity")
        implementation_command_parser.add_argument(
            "--previous", type=Path, help="exact predecessor proposal JSON required for an explicitly revised intent"
        )
        implementation_command_parser.add_argument("--json", action="store_true")
        if implementation_command == "propose":
            implementation_command_parser.add_argument("--out", required=True, type=Path, help="new private bundle")
        else:
            implementation_command_parser.add_argument(
                "--bundle", required=True, type=Path, help="complete retained implementation proposal directory"
            )
    composition_parser = commands.add_parser(
        "composition", help="validate the complete independently reviewed composition definition offline"
    )
    composition_commands = composition_parser.add_subparsers(dest="composition_command", required=True)
    validate_composition = composition_commands.add_parser(
        "validate", help="validate required members, policies and model inputs without fetching evidence"
    )
    validate_composition.add_argument("--definition", required=True, type=Path, help="composition definition YAML/JSON")
    validate_composition.add_argument("--date", required=True, type=dt.date.fromisoformat)
    validate_composition.add_argument(
        "--json", action="store_true", help="print validation and content identity as JSON"
    )
    compose = commands.add_parser(
        "compose", help="admit and gate every required member of an independently approved composition"
    )
    compose.add_argument("source", choices=["github-actions"])
    compose.add_argument("--definition", required=True, type=Path, help="approved composition definition YAML/JSON")
    compose.add_argument("--selection", required=True, type=Path, help="complete immutable member run selections")
    compose.add_argument("--date", required=True, type=dt.date.fromisoformat)
    compose.add_argument("--out", required=True, type=Path, help="new private evidence and aggregate report directory")
    compose.add_argument(
        "--readiness-junit",
        action="store_true",
        help="retain derived readiness.xml inside --out, separately from producer JUnit reports",
    )
    compose.add_argument("--json", action="store_true", help="print the complete aggregate report as JSON")
    isolation_parser = commands.add_parser("isolation", help="inspect an optional local backend's runtime filesystem")
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
        help="run one command through the optional local Linux Bubblewrap backend",
        description=(
            "Run one command through linux-bubblewrap-v1 using an approved runtime policy and immutable inputs. "
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
    evidence_parser = commands.add_parser("evidence", help="prepare verification evidence for admission")
    evidence_commands = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    package = evidence_commands.add_parser("package", help="prepare a verification report and its producer manifest")
    package.add_argument("--report", required=True, type=Path, help="complete JUnit XML report from the producer")
    package.add_argument("--out", required=True, type=Path, help="new evidence bundle directory")
    package.add_argument("--json", action="store_true", help="print the producer manifest as JSON")
    propose = evidence_commands.add_parser(
        "propose-policy", help="draft a trust-policy candidate for independent review"
    )
    proposal_sources = propose.add_subparsers(dest="proposal_source", required=True)
    github_proposal = proposal_sources.add_parser(
        "github-actions", help="fingerprint an explicit GitHub Actions producer"
    )
    github_proposal.add_argument("--repository", required=True, help="exact OWNER/REPO to read")
    github_proposal.add_argument("--revision", required=True, help="full immutable 40-character Git commit SHA")
    github_proposal.add_argument(
        "--proposal", required=True, type=Path, help="YAML/JSON producer and observer_paths proposal"
    )
    github_proposal.add_argument(
        "--model-file",
        required=True,
        action="append",
        help="literal repository-relative model YAML/JSON file; repeatable",
    )
    github_proposal.add_argument("--name", required=True, help="explicit candidate policy name")
    github_proposal.add_argument("--version", required=True, help="explicit candidate policy version")
    github_proposal.add_argument(
        "--out", required=True, type=Path, help="new candidate JSON file outside model folders"
    )
    github_proposal.add_argument("--branch-protection", choices=("required", "not_required"))
    github_proposal.add_argument("--json", action="store_true", help="print the exact saved candidate policy JSON")
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
    execute_parser = commands.add_parser("execute", help="preview or explicitly run bounded approved work")
    execute_sources = execute_parser.add_subparsers(dest="execute_source", required=True)
    execute_local = execute_sources.add_parser(
        "local", parents=[common], help="supervise one approved isolated local executor attempt"
    )
    execute_local.add_argument("--plan", required=True, type=Path, help="saved verification plan JSON")
    execute_local.add_argument("--action", required=True, help="selected saved action ID")
    execute_local.add_argument("--policy", required=True, type=Path, help="reviewed readiness policy JSON")
    execute_local.add_argument("--trust-policy", required=True, type=Path, help="reviewed producer/model policy JSON")
    execute_local.add_argument(
        "--executor-policy", required=True, type=Path, help="reviewed local executor policy JSON"
    )
    execute_local.add_argument("--target", required=True, type=Path, help="approved input implementation target JSON")
    execute_local.add_argument("--state", required=True, type=Path, help="actual current state with pending work")
    execute_local.add_argument("--date", required=True, type=dt.date.fromisoformat, help="evaluation date YYYY-MM-DD")
    execute_local.add_argument("--binding", required=True, help="active registered execution binding ID")
    execute_local.add_argument(
        "--inputs", type=Path, help="explicit versioned UTF-8 snapshot JSON (default: no inputs)"
    )
    execute_local.add_argument("--journal", type=Path, help="initialized private executor companion ledger")
    execute_local.add_argument("--ledger", type=Path, help="private execution binding ledger")
    execute_local.add_argument("--authorization", type=Path, help="separately reviewed attempt authorization JSON")
    execute_local.add_argument(
        "--lock-timeout", type=nonnegative_seconds, help="maximum shared storage lock wait in seconds (default: 30)"
    )
    execute_mode = execute_local.add_mutually_exclusive_group()
    execute_mode.add_argument("--dry-run", action="store_true", help="read-only execution preview (the default)")
    execute_mode.add_argument("--apply", action="store_true", help="run the separately authorized bounded attempt")
    executors = commands.add_parser("executors", help="initialize, inspect or stop retained executor attempts")
    executor_commands = executors.add_subparsers(dest="executor_command", required=True)
    for operation in ("init", "show", "cancel", "recover", "recover-journal"):
        executor_parser = executor_commands.add_parser(
            operation,
            help={
                "init": "explicitly initialize a private companion ledger with a fixed concurrency limit",
                "show": "inspect retained attempts without changing them",
                "cancel": "request cancellation and stop the exact retained attempt",
                "recover": "reconcile or terminate an interrupted attempt before another launch",
                "recover-journal": "explicitly repair a protected interrupted SQLite transaction",
            }[operation],
        )
        executor_parser.add_argument("--journal", required=True, type=Path, help="private executor companion ledger")
        if operation == "init":
            executor_parser.add_argument(
                "--ledger", required=True, type=Path, help="canonical execution binding ledger"
            )
            executor_parser.add_argument(
                "--max-concurrency",
                required=True,
                type=positive_integer,
                choices=range(1, 17),
                help="fixed coordinator concurrency limit (1-16)",
            )
        elif operation == "recover-journal":
            executor_parser.add_argument(
                "--authorize-recovery", action="store_true", help="authorize protected backup and SQLite recovery"
            )
        else:
            executor_parser.add_argument("--execution", required=True, help="retained execution identity")
        executor_parser.add_argument(
            "--lock-timeout", type=nonnegative_seconds, help="maximum shared storage lock wait in seconds (default: 30)"
        )
        executor_parser.add_argument("--json", action="store_true", help="print the retained executor report as JSON")
    handoff_parser = commands.add_parser("handoff", help="review or publish retained executor artifacts locally")
    handoff_sources = handoff_parser.add_subparsers(dest="handoff_source", required=True)
    handoff_git_parser = handoff_sources.add_parser(
        "local-git", help="preview or separately authorize an exact artifact-to-Git handoff"
    )
    handoff_git_parser.add_argument("--request", required=True, type=Path, help="reviewed handoff request JSON")
    handoff_git_parser.add_argument("--authorization", type=Path, help="separately reviewed handoff authorization JSON")
    handoff_mode = handoff_git_parser.add_mutually_exclusive_group()
    handoff_mode.add_argument("--dry-run", action="store_true", help="read-only handoff preview (the default)")
    handoff_mode.add_argument("--apply", action="store_true", help="publish only the separately authorized local ref")
    handoff_git_parser.add_argument(
        "--lock-timeout", type=nonnegative_seconds, help="maximum shared handoff lock wait in seconds (default: 30)"
    )
    handoff_git_parser.add_argument("--json", action="store_true", help="print the handoff report as JSON")
    handoffs = commands.add_parser("handoffs", help="initialize, inspect or recover protected local handoffs")
    handoff_commands = handoffs.add_subparsers(dest="handoff_command", required=True)
    for operation in ("init-repository", "init", "show", "recover", "recover-journal"):
        handoff_command = handoff_commands.add_parser(
            operation,
            help={
                "init-repository": "initialize a new protected bare Git repository",
                "init": "initialize a new private handoff journal for one repository",
                "show": "inspect retained handoff facts without publishing",
                "recover": "resume the exact retained local publication",
                "recover-journal": "back up and repair an interrupted handoff journal transaction",
            }[operation],
        )
        if operation in {"init-repository", "init"}:
            handoff_command.add_argument(
                "--repository", required=True, type=Path, help="protected local bare repository"
            )
        if operation != "init-repository":
            handoff_command.add_argument("--journal", required=True, type=Path, help="private handoff journal")
        if operation in {"show", "recover"}:
            handoff_command.add_argument("--handoff", required=True, help="retained handoff identity")
        if operation == "recover-journal":
            handoff_command.add_argument(
                "--authorize-recovery",
                required=True,
                action="store_true",
                help="authorize protected backup and SQLite repair",
            )
        if operation != "init-repository":
            handoff_command.add_argument(
                "--lock-timeout",
                type=nonnegative_seconds,
                help="maximum shared handoff lock wait in seconds (default: 30)",
            )
        handoff_command.add_argument("--json", action="store_true", help="print the retained handoff report as JSON")
    dispatch_parser = commands.add_parser("dispatch", help="preview or explicitly dispatch approved work")
    dispatch_sources = dispatch_parser.add_subparsers(dest="dispatch_source", required=True)
    dispatch_github = dispatch_sources.add_parser(
        "github-issues", parents=[common], help="dispatch one reviewed verification action to GitHub issues"
    )

    def dispatch_selection_arguments(selected: argparse.ArgumentParser, *, storage_required: bool) -> None:
        selected.add_argument("--plan", required=True, type=Path, help="saved verification plan JSON")
        selected.add_argument("--action", required=True, help="selected saved action ID")
        selected.add_argument("--policy", required=True, type=Path, help="reviewed readiness policy JSON")
        selected.add_argument("--trust-policy", required=True, type=Path, help="reviewed producer/model policy JSON")
        selected.add_argument(
            "--dispatch-policy", required=True, type=Path, help="reviewed GitHub dispatch policy JSON"
        )
        selected.add_argument("--target", required=True, type=Path, help="approved implementation target JSON")
        selected.add_argument("--state", required=True, type=Path, help="actual current state file")
        selected.add_argument("--date", required=True, type=dt.date.fromisoformat, help="evaluation date YYYY-MM-DD")
        selected.add_argument(
            "--journal", required=storage_required, type=Path, help="initialized private dispatch journal"
        )
        selected.add_argument("--ledger", required=storage_required, type=Path, help="private execution binding ledger")
        selected.add_argument(
            "--authorization",
            required=storage_required,
            type=Path,
            help="separately reviewed dispatch authorization JSON",
        )
        selected.add_argument(
            "--lock-timeout", type=nonnegative_seconds, help="maximum shared storage lock wait for apply/recovery"
        )

    dispatch_selection_arguments(dispatch_github, storage_required=False)
    dispatch_mode = dispatch_github.add_mutually_exclusive_group()
    dispatch_mode.add_argument("--dry-run", action="store_true", help="read-only preview (the default)")
    dispatch_mode.add_argument("--apply", action="store_true", help="apply the separately reviewed authorization")
    dispatches = commands.add_parser("dispatches", help="initialize, inspect or recover durable dispatch history")
    dispatch_commands = dispatches.add_subparsers(dest="dispatch_command", required=True)
    dispatch_recover = dispatch_commands.add_parser(
        "recover", parents=[common], help="explicitly reconcile one retained dispatch and its current contracts"
    )
    dispatch_selection_arguments(dispatch_recover, storage_required=True)
    dispatch_resolve = dispatch_commands.add_parser(
        "resolve", help="review or explicitly resolve stale local coordination without changing its resources"
    )
    dispatch_resolve.add_argument("--journal", required=True, type=Path, help="existing private dispatch journal")
    dispatch_resolve.add_argument("--dispatch", required=True, help="retained partial registration dispatch ID")
    dispatch_resolve.add_argument(
        "--reason", required=True, help="reason pinned in the separate reviewed authorization"
    )
    resolution_mode = dispatch_resolve.add_mutually_exclusive_group()
    resolution_mode.add_argument("--dry-run", action="store_true", help="read-only resolution preview (the default)")
    resolution_mode.add_argument("--apply", action="store_true", help="append only the reviewed resolution receipt")
    dispatch_resolve.add_argument(
        "--authorization", type=Path, help="separately reviewed resolution authorization JSON"
    )
    dispatch_resolve.add_argument("--lock-timeout", type=nonnegative_seconds, help="maximum shared storage lock wait")
    dispatch_resolve.add_argument("--json", action="store_true", help="print the resolution report as JSON")
    for operation in ("init", "show", "supersede", "recover-journal"):
        retained_dispatch = dispatch_commands.add_parser(
            operation,
            help={
                "init": "explicitly initialize a new private dispatch journal",
                "show": "inspect retained dispatch history without changing it",
                "supersede": "explicitly retire local dispatch intent without deleting remote work",
                "recover-journal": "explicitly repair only a hot dispatch journal, preserving its storage backup",
            }[operation],
        )
        retained_dispatch.add_argument("--journal", required=True, type=Path, help="private dispatch journal file")
        if operation in {"show", "supersede"}:
            retained_dispatch.add_argument("--dispatch", required=True, help="retained dispatch ID")
        if operation == "supersede":
            retained_dispatch.add_argument("--reason", required=True, help="explicit reason retained in history")
            retained_dispatch.add_argument(
                "--authorize-supersession", required=True, action="store_true", help="authorize this local supersession"
            )
        if operation == "recover-journal":
            retained_dispatch.add_argument(
                "--authorize-recovery",
                required=True,
                action="store_true",
                help="authorize storage-only journal recovery",
            )
        retained_dispatch.add_argument("--lock-timeout", type=nonnegative_seconds, help="maximum journal lock wait")
        retained_dispatch.add_argument("--json", action="store_true", help="print the dispatch result as JSON")
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
    gate.add_argument("--junit", type=Path, help="save readiness JUnit XML outside model folders")
    gate.add_argument("--junit-id", help="stable logical target/member ID, required with --junit")
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
    if args.command in {"handoff", "handoffs"}:
        from exacly import handoff, handoff_git, handoff_store

        handoff_options: dict[str, Any] = {}
        if getattr(args, "lock_timeout", None) is not None:
            handoff_options["lock_timeout"] = args.lock_timeout
        handoff_operation: str = args.handoff_command if args.command == "handoffs" else ""
        try:
            if handoff_operation == "init-repository":
                handoff_report = {
                    "schema_version": "0.1.0",
                    "status": "initialized",
                    "repository": handoff_git.initialize_repository(args.repository),
                }
            elif handoff_operation == "init":
                handoff_report = handoff_store.initialize(
                    args.journal, repository=handoff_git.inspect_repository(args.repository), **handoff_options
                )
            elif handoff_operation == "show":
                handoff_report = handoff.show(args.journal, args.handoff, **handoff_options)
            elif handoff_operation == "recover":
                handoff_report = handoff.recover(args.journal, args.handoff, **handoff_options)
            elif handoff_operation == "recover-journal":
                handoff_report = handoff.recover_journal(
                    args.journal, authorize=args.authorize_recovery, **handoff_options
                )
            else:
                if not args.apply and args.authorization is not None:
                    parser.error("--authorization requires --apply; a handoff preview grants no permission")
                if args.apply and args.authorization is None:
                    parser.error("--apply requires --authorization")
                handoff_request = handoff.request_from_document(strict_json_document(args.request, 8 * 1024 * 1024))
                if args.apply:
                    handoff_report = handoff.apply(
                        handoff_request,
                        authorization=strict_json_document(args.authorization, 8 * 1024 * 1024),
                        **handoff_options,
                    )
                else:
                    handoff_report = handoff.preview(handoff_request, **handoff_options)
            handoff_preview = args.command == "handoff" and not args.apply
            handoff_statuses = {
                "init-repository": {"initialized"},
                "init": {"initialized"},
                "show": {"prepared", "published", "missing"},
                "recover": {"prepared", "published", "conflict", "missing"},
                "recover-journal": {"recovered"},
            }.get(handoff_operation, {"prepared", "published", "conflict", "blocked"})
            if handoff_preview:
                handoff_statuses.add("ready")
            if (
                not isinstance(handoff_report, dict)
                or handoff_report.get("schema_version") != "0.1.0"
                or handoff_report.get("status") not in handoff_statuses
                or (handoff_operation == "init" and not isinstance(handoff_report.get("journal_id"), str))
                or (handoff_operation == "init-repository" and not isinstance(handoff_report.get("repository"), dict))
                or (
                    handoff_operation not in {"init-repository", "init"}
                    and handoff_report.get("dry_run") is not handoff_preview
                )
            ):
                raise ValueError("handoff returned an invalid report")
            if args.json:
                handoff_rendered = json.dumps(handoff_report, indent=2, sort_keys=True)
            elif handoff_operation in {"init-repository", "init"}:
                handoff_rendered = (
                    f"Initialized protected handoff repository: {args.repository}"
                    if handoff_operation == "init-repository"
                    else (
                        f"Initialized private handoff journal: {args.journal}\n"
                        f"Journal ID: {handoff_report['journal_id']}"
                    )
                )
            else:
                handoff_rendered = handoff.as_text(handoff_report)
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly {args.command}: {redact_tokens(str(error), github_tokens)}", file=sys.stderr)
            return 2
        print(handoff_rendered)
        if handoff_operation == "show":
            return 1 if handoff_report["status"] == "missing" else 0
        return 0 if handoff_report["status"] in {"ready", "published", "initialized", "recovered"} else 1
    if args.command == "delivery":
        from exacly import delivery_io

        delivery_tokens = (*github_tokens, os.environ.get(delivery_io.TOKEN_ENVIRONMENT))
        try:
            delivery_answer = delivery_io.run(args)
            delivery_status = delivery_io.exit_status(delivery_answer)
            delivery_operational = delivery_answer.get("operational_status")
            delivery_health_point = (
                delivery_operational.get("evaluated_at") if isinstance(delivery_operational, dict) else None
            )
            delivery_health_label = (
                f"Static readiness as of {delivery_health_point}"
                if isinstance(delivery_health_point, str)
                else "Static readiness"
            )
            delivery_health_text = (
                f"{delivery_health_label}: {delivery_operational['status']} "
                f"({delivery_operational['freshness']}). {delivery_operational['reason']}\n"
                if isinstance(delivery_operational, dict)
                else ""
            )
            delivery_rendered = (
                json.dumps(delivery_answer, indent=2, sort_keys=True)
                if args.json
                else (
                    f"Delivery {args.delivery_command}: {delivery_answer['status']}\n"
                    f"{delivery_health_text}"
                    "Receipts are exact-stage history; verification and later effects require separate authority."
                )
            )
            if redact_tokens(delivery_rendered, delivery_tokens) != delivery_rendered:
                raise ValueError("delivery output contains protected credential material; inspect the private journal")
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly delivery: {redact_tokens(str(error), delivery_tokens)}", file=sys.stderr)
            return 2
        print(delivery_rendered)
        return delivery_status
    if args.command == "implementation":
        from exacly import implementation_planning, implementation_planning_io

        try:
            implementation_inputs = implementation_planning_io.load_inputs(
                args.model, intent=args.intent, policy=args.policy, target=args.target, previous=args.previous
            )
            planning_options = {
                "approved_intent_digest": args.approved_intent_digest,
                "approved_policy_digest": args.approved_policy_digest,
                "issuer": args.issuer,
                "approval_id": args.approval_id,
                "previous": implementation_inputs.previous,
            }
            if args.implementation_command == "propose":
                implementation_planning_io.protect_output(args.out, implementation_inputs)
                implementation_answer = implementation_planning.create(
                    implementation_inputs.documents,
                    implementation_inputs.intent,
                    implementation_inputs.policy,
                    Target(implementation_inputs.target),
                    args.date,
                    **planning_options,
                )
                implementation_planning_io.retain(args.out, implementation_inputs, implementation_answer)
            else:
                implementation_answer = implementation_planning.validate_current(
                    implementation_planning_io.load_bundle(args.bundle),
                    implementation_inputs.documents,
                    implementation_inputs.intent,
                    implementation_inputs.policy,
                    Target(implementation_inputs.target),
                    args.date,
                    **planning_options,
                )
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly implementation: {redact_tokens(str(error), github_tokens)}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(implementation_answer, indent=2, sort_keys=True))
        else:
            print(f"Implementation {args.implementation_command}: {implementation_answer['status']}")
            print("Advisory planning only; execution, merge, release and deployment require separate authority.")
        return 1 if implementation_answer["status"] == "blocked" else 0
    if args.command in {"composition", "compose"}:
        from exacly import composition, composition_io, github_evidence

        try:
            loaded = composition_io.load_definition(args.definition, args.date)
            if args.command == "composition":
                report = loaded.prepared.as_dict()
            else:
                selection = composition_io.read_document(args.selection)
                composition.validate_selection(loaded.prepared, selection, args.date)
                output = composition_io.protect_output(args.out, [*loaded.paths, args.selection], loaded.model_folders)
                token = os.environ.get("GH_TOKEN")
                if not token or not token.strip():
                    token = os.environ.get("GITHUB_TOKEN")
                if not token or not token.strip():
                    raise ValueError("set GH_TOKEN or GITHUB_TOKEN to authenticate GitHub Actions evidence retrieval")
                try:
                    client = github_evidence.GitHubClient(token)
                except github_evidence.GitHubError:
                    raise ValueError("GitHub credentials are invalid") from None

                class CompositionSource:
                    def get_json(self, path: str) -> dict[str, Any]:
                        try:
                            return client.get_json(path)
                        except github_evidence.GitHubError as error:
                            raise github_evidence.GitHubError(redact_tokens(str(error), github_tokens)) from None

                    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
                        try:
                            return client.download_artifact(repository, artifact_id)
                        except github_evidence.GitHubError as error:
                            raise github_evidence.GitHubError(redact_tokens(str(error), github_tokens)) from None

                writer = composition_io.ResultWriter(output, loaded, selection, args.date)
                composition_answer = composition.evaluate(
                    CompositionSource(),
                    loaded.prepared,
                    selection,
                    args.date,
                    on_member=writer.member,
                    retain_admissions=False,
                )
                report = composition_answer.report
                writer.finish(report, readiness_junit=args.readiness_junit)
        except (query.QueryError, ValueError, OSError, github_evidence.GitHubError) as error:
            print(f"exacly {args.command}: {redact_tokens(str(error), github_tokens)}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        elif args.command == "composition":
            print(f"Composition definition: {report['definition']['name']}\nDigest: {report['definition_digest']}")
            print("Validation identifies supplied content; independent approval remains required.")
        else:
            print(f"Composition {report['definition']['name']}: {report['result']}")
            for member in report["members"]:
                print(f"  {member['id']}: {member['status']} — {member['reason']}")
        return 0 if args.command == "composition" or report["result"] == "satisfied" else 1
    if args.command in {"execute", "executors"}:
        from exacly import executor, executor_store

        executor_options: dict[str, Any] = {}
        if args.lock_timeout is not None:
            executor_options["lock_timeout"] = args.lock_timeout
        try:
            executor_operation = args.executor_command if args.command == "executors" else None
            if executor_operation == "init":
                protect_executor_initialization(args.journal, args.ledger)
                executor_report = executor_store.initialize(
                    args.journal, ledger=args.ledger, max_concurrency=args.max_concurrency, **executor_options
                )
                if (
                    not isinstance(executor_report, dict)
                    or executor_report.get("schema_version") != "0.1.0"
                    or executor_report.get("status") != "initialized"
                    or not isinstance(executor_report.get("journal_id"), str)
                ):
                    raise ValueError("executor initialization returned an invalid report")
                print(
                    json.dumps(executor_report, indent=2, sort_keys=True)
                    if args.json
                    else (
                        f"Initialized private executor companion ledger: {args.journal}\n"
                        f"Journal ID: {executor_report['journal_id']}"
                    )
                )
                return 0
            if executor_operation == "show":
                executor_report = executor.show(args.journal, args.execution, **executor_options)
            elif executor_operation == "cancel":
                executor_report = executor.cancel(args.journal, args.execution, **executor_options)
            elif executor_operation == "recover":
                executor_report = executor.recover(args.journal, args.execution, **executor_options)
            elif executor_operation == "recover-journal":
                executor_report = executor.recover_journal(
                    args.journal, authorize=args.authorize_recovery, **executor_options
                )
            else:
                if not args.apply and args.authorization is not None:
                    parser.error("--authorization requires --apply; an execution preview grants no permission")
                if args.apply and any(value is None for value in (args.journal, args.ledger, args.authorization)):
                    parser.error("--apply requires --journal, --ledger and --authorization")
                if (args.journal is None) != (args.ledger is None):
                    parser.error("--journal and --ledger must be supplied together")
                executor_plan = strict_json_document(args.plan, 8 * 1024 * 1024)
                executor_readiness = strict_json_document(args.policy, 1024 * 1024)
                executor_trust = strict_json_document(args.trust_policy, 1024 * 1024)
                executor_policy = strict_json_document(args.executor_policy, 1024 * 1024)
                executor_target = Target(strict_json_document(args.target, 64 * 1024))
                executor_documents = load(args.model or [DEFAULT_MODEL])
                executor_inputs = [args.plan, args.policy, args.trust_policy, args.executor_policy, args.target]
                executor_inputs.extend(Path(name) for name, doc in executor_documents.items() if not is_state(doc))
                if args.inputs is not None:
                    executor_inputs.append(args.inputs)
                if args.authorization is not None:
                    executor_inputs.append(args.authorization)
                protect_executor_paths(
                    args.state, args.journal, args.ledger, executor_inputs, executor_trust, executor_policy
                )
                executor_request = executor.Request(
                    plan=executor_plan,
                    action_id=args.action,
                    readiness_policy=executor_readiness,
                    trust_policy=executor_trust,
                    executor_policy=executor_policy,
                    target=executor_target,
                    date=args.date,
                    binding_id=args.binding,
                    inputs=isolation_inputs(args.inputs),
                )
                if args.apply:
                    executor_report = executor.apply(
                        args.journal,
                        args.ledger,
                        executor_documents,
                        args.state,
                        executor_request,
                        authorization=strict_json_document(args.authorization, 64 * 1024),
                        **executor_options,
                    )
                else:
                    executor_report = executor.preview(
                        executor_documents, args.state, executor_request, **executor_options
                    )
            executor_preview = args.command == "execute" and not args.apply
            executor_terminal = {
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
                "interrupted",
                "output_invalid",
                "runtime_failed",
                "output_limit",
            }
            executor_statuses = {
                *executor_terminal,
                "running",
                "reserved",
                "attempts_exhausted",
                "cancellation_requested",
                "blocked",
                "conflict",
                "missing",
            }
            if executor_preview:
                executor_statuses.add("ready")
            if executor_operation in {"show", "cancel", "recover"}:
                executor_statuses.add("prepared")
            elif executor_operation == "recover-journal":
                executor_statuses = {"recovered"}
            if (
                not isinstance(executor_report, dict)
                or executor_report.get("schema_version") != "0.1.0"
                or executor_report.get("status") not in executor_statuses
                or executor_report.get("dry_run") is not executor_preview
            ):
                raise ValueError("executor returned an invalid report")
            executor_rendered = (
                json.dumps(executor_report, indent=2, sort_keys=True)
                if args.json
                else executor.as_text(executor_report)
            )
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly {args.command}: {redact_tokens(str(error), github_tokens)}", file=sys.stderr)
            return 2
        print(executor_rendered)
        if executor_operation == "show":
            return 1 if executor_report["status"] == "missing" else 0
        if executor_operation in {"cancel", "recover"}:
            return 0 if executor_report["status"] in {*executor_terminal, "prepared"} else 1
        return 0 if executor_report["status"] in {"ready", "succeeded", "recovered"} else 1
    if args.command in {"dispatch", "dispatches"}:
        from exacly import dispatch, dispatch_store, github_dispatch

        dispatch_options: dict[str, Any] = {}
        if args.lock_timeout is not None:
            dispatch_options["lock_timeout"] = args.lock_timeout
        try:
            retained_operation = args.dispatch_command if args.command == "dispatches" else None
            if retained_operation == "init":
                dispatch_report = dispatch_store.initialize(args.journal, **dispatch_options)
                if (
                    not isinstance(dispatch_report, dict)
                    or dispatch_report.get("schema_version") != "0.1.0"
                    or dispatch_report.get("status") != "initialized"
                    or not isinstance(dispatch_report.get("journal_id"), str)
                ):
                    raise ValueError("dispatch journal initialization returned an invalid report")
                print(
                    json.dumps(dispatch_report, indent=2, sort_keys=True)
                    if args.json
                    else (
                        f"Initialized private dispatch journal: {args.journal}\n"
                        f"Journal ID: {dispatch_report['journal_id']}"
                    )
                )
                return 0
            if retained_operation == "show":
                dispatch_report = dispatch.show(args.journal, args.dispatch, **dispatch_options)
            elif retained_operation == "resolve":
                if not args.apply:
                    if args.authorization is not None:
                        parser.error("--authorization requires --apply; a resolution preview grants no permission")
                    dispatch_report = dispatch.resolution_preview(
                        args.journal, args.dispatch, reason=args.reason, **dispatch_options
                    )
                else:
                    if args.authorization is None:
                        parser.error("resolve --apply requires --authorization")
                    inspected = dispatch.show(args.journal, args.dispatch, **dispatch_options)
                    protect_dispatch_resolution_authorization(args.authorization, args.journal, inspected)
                    resolution_authorization = strict_json_document(args.authorization, 64 * 1024)
                    if (
                        not isinstance(resolution_authorization, dict)
                        or resolution_authorization.get("reason") != args.reason
                    ):
                        raise ValueError("--reason must match the separately reviewed resolution authorization")
                    dispatch_report = dispatch.resolve(
                        args.journal, args.dispatch, authorization=resolution_authorization, **dispatch_options
                    )
            elif retained_operation == "recover-journal":
                dispatch_report = dispatch.recover_journal(
                    args.journal, authorize=args.authorize_recovery, **dispatch_options
                )
            elif retained_operation == "supersede":
                dispatch_report = dispatch.supersede(
                    args.journal,
                    args.dispatch,
                    reason=args.reason,
                    authorize=args.authorize_supersession,
                    **dispatch_options,
                )
            else:
                perform_apply = retained_operation == "recover" or args.apply
                if not perform_apply:
                    if args.authorization is not None:
                        parser.error("--authorization requires --apply; a preview does not grant dispatch permission")
                    if (args.journal is None) != (args.ledger is None):
                        parser.error("--journal and --ledger must be supplied together")
                    if args.lock_timeout is not None:
                        parser.error("--lock-timeout requires --apply or dispatches recover")
                elif any(value is None for value in (args.journal, args.ledger, args.authorization)):
                    parser.error("--apply requires --journal, --ledger and --authorization")
                dispatch_plan = strict_json_document(args.plan, 8 * 1024 * 1024)
                dispatch_readiness = strict_json_document(args.policy, 1024 * 1024)
                dispatch_trust = strict_json_document(args.trust_policy, 1024 * 1024)
                dispatch_policy = strict_json_document(args.dispatch_policy, 64 * 1024)
                dispatch_target = Target(strict_json_document(args.target, 64 * 1024))
                dispatch_documents = load(args.model or [DEFAULT_MODEL])
                dispatch_inputs = [args.plan, args.policy, args.trust_policy, args.dispatch_policy, args.target]
                dispatch_inputs.extend(Path(name) for name, doc in dispatch_documents.items() if not is_state(doc))
                if args.authorization is not None:
                    dispatch_inputs.append(args.authorization)
                if args.journal is not None:
                    protect_dispatch_paths(args.state, args.journal, args.ledger, dispatch_inputs, dispatch_trust)
                dispatch_request = dispatch.Request(
                    plan=dispatch_plan,
                    action_id=args.action,
                    readiness_policy=dispatch_readiness,
                    trust_policy=dispatch_trust,
                    dispatch_policy=dispatch_policy,
                    target=dispatch_target,
                    date=args.date,
                )
                if not perform_apply:
                    dispatch_report = dispatch.preview(dispatch_documents, args.state, dispatch_request)
                    if not isinstance(dispatch_report, dict) or dispatch_report.get("dry_run") is not True:
                        raise ValueError("dispatch preview returned a non-dry-run report")
                else:
                    reviewed_authorization = strict_json_document(args.authorization, 64 * 1024)
                    dispatch_apply = dispatch.recover if retained_operation == "recover" else dispatch.apply
                    dispatch_report = dispatch_apply(
                        args.journal,
                        args.ledger,
                        dispatch_documents,
                        args.state,
                        dispatch_request,
                        authorization=reviewed_authorization,
                        client=LazyGitHubIssueAdapter(),
                        **dispatch_options,
                    )
            dispatch_statuses = {
                "registered",
                "superseded",
                "resolved",
                "ambiguous",
                "conflict",
                "attempts_exhausted",
                "blocked",
                "missing",
            }
            dispatch_preview = (args.command == "dispatch" or retained_operation == "resolve") and not args.apply
            if dispatch_preview:
                dispatch_statuses.add("ready")
            if retained_operation == "show":
                dispatch_statuses.update({"prepared", "creating", "remote_confirmed", "registration_prepared"})
            elif retained_operation == "resolve":
                dispatch_statuses &= {"ready", "resolved", "conflict", "blocked", "missing"}
            elif retained_operation == "recover-journal":
                dispatch_statuses = {"repaired"}
            if (
                not isinstance(dispatch_report, dict)
                or dispatch_report.get("schema_version") != "0.1.0"
                or dispatch_report.get("status") not in dispatch_statuses
                or dispatch_report.get("dry_run") is not dispatch_preview
            ):
                raise ValueError("dispatch returned an invalid report")
            dispatch_rendered = (
                json.dumps(dispatch_report, indent=2, sort_keys=True)
                if args.json
                else dispatch.as_text(dispatch_report)
            )
        except (
            query.QueryError,
            ValueError,
            OSError,
            GitHubCredentialsError,
            github_dispatch.GitHubDispatchError,
        ) as error:
            print(f"exacly {args.command}: {redact_tokens(str(error), github_tokens)}", file=sys.stderr)
            return 2
        print(dispatch_rendered)
        if retained_operation == "show":
            return 1 if dispatch_report["status"] == "missing" else 0
        return 0 if dispatch_report["status"] in {"ready", "registered", "superseded", "resolved", "repaired"} else 1
    if args.command == "isolation":
        from exacly import isolation

        try:
            digest = isolation.rootfs_digest(args.rootfs)
            identity_report = {
                "schema_version": "0.1.0",
                "operation": "inspect-rootfs",
                "rootfs": str(args.rootfs),
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
            runtime = isolation.parse_policy(strict_json_document(args.runtime_policy, 64 * 1024))
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
    if args.command == "evidence" and args.evidence_command == "propose-policy":
        from exacly import admission, github_evidence

        try:
            proposal = read(args.proposal)
            if not isinstance(proposal, dict) or set(proposal) != {"producer", "observer_paths"}:
                raise ValueError("--proposal must contain exactly producer and observer_paths")
            if not args.name.strip() or not args.version.strip():
                raise ValueError("--name and --version must be nonblank")
            if not all(
                admission.safe_path(path) and not any(char in path for char in "*?[]") for path in args.model_file
            ):
                raise ValueError("--model-file must name a safe literal repository-relative config file")
            github_evidence.api_path(f"/repos/{args.repository}")
            protect_policy_candidate(args.out, args.proposal, args.model_file)
            candidate = admission.propose_policy(
                LazyGitHubSource(),
                args.repository,
                args.revision,
                args.model_file,
                producer=proposal["producer"],
                observer_paths=proposal["observer_paths"],
                name=args.name,
                version=args.version,
                branch_protection=args.branch_protection,
            )
            saved = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
            write_policy_candidate(args.out, args.proposal, args.model_file, saved)
        except (
            query.QueryError,
            ValueError,
            TypeError,
            OSError,
            GitHubCredentialsError,
            admission.Rejected,
            github_evidence.GitHubError,
            yaml.YAMLError,
        ) as error:
            print(f"exacly evidence propose-policy: {redact_tokens(str(error), github_tokens)}", file=sys.stderr)
            return 2
        print(
            saved
            if args.json
            else f"Saved candidate policy to {args.out}; it requires independent review before approval.",
            end="" if args.json else "\n",
        )
        return 0
    if args.command == "evidence" and args.evidence_command == "package":
        from exacly import evidence

        try:
            manifest = evidence.package(args.report, args.out)
        except (evidence.EvidenceError, OSError) as error:
            print(f"exacly evidence package: {error}", file=sys.stderr)
            return 1
        print(
            json.dumps(manifest, indent=2, sort_keys=True)
            if args.json
            else f"Prepared verification evidence in {args.out}"
        )
        return 0
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
        from exacly.evidence_retention import require_private_filesystem

        token = os.environ.get("GH_TOKEN")
        if not token or not token.strip():
            token = os.environ.get("GITHUB_TOKEN")
        try:
            require_private_filesystem()
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
                write_derived(args.out, paths, [*(Path(name) for name in documents), args.policy, args.target], saved)
        except (query.QueryError, ValueError, OSError) as error:
            print(f"exacly plan: {error}", file=sys.stderr)
            return 2
        print(saved if args.json else planning.as_text(answer), end="" if args.json else "\n")
        return 0
    if args.command == "gate":
        try:
            if (args.junit is None) != (args.junit_id is None):
                raise ValueError("--junit and --junit-id must be supplied together")
            target = Target(read(args.target))
            paths = args.model or [DEFAULT_MODEL]
            documents = load(paths)
            model = query.load(documents, args.date)
            answer = policy.evaluate(model, read(args.policy), target)
            if args.junit is not None:
                xml = policy.as_junit(answer, namespace=model.graph.namespace, report_id=args.junit_id)
                write_derived(
                    args.junit,
                    paths,
                    [*(Path(name) for name in documents), args.policy, args.target],
                    xml,
                    option="--junit",
                )
        except (query.QueryError, ValueError, OSError, RuntimeError) as error:
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
