"""Structure and interface diff of one git commit, at component level.

Reads the parent and the commit straight from git (no checkout), parses the
changed Python modules with `ast`, and reports:

- per changed module: its component, whether it was added, removed or
  modified, imports added and removed, and public signatures that changed;
- component-level dependency edges added, removed and unchanged;
- optionally, a Mermaid component diagram of the change.

Components come from a domain map: a YAML file with a `rules` list of
`{match: <glob>, domain: <name>}`, matched against module paths relative to
the package root, first match wins. A module no rule matches becomes its own
component, `unmapped:<path>`.

Usage:
    python model_diff.py REPO COMMIT --source-root backend/src --package mypkg \\
        --domains architecture/domains.yaml [--domains-rev REV] [--mermaid]
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys

import yaml


class Git:
    def __init__(self, repo: str) -> None:
        self.repo = repo

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True)

    def exists(self, rev: str, path: str) -> bool:
        return self.run("cat-file", "-e", f"{rev}:{path}").returncode == 0

    def show(self, rev: str, path: str) -> str | None:
        result = self.run("show", f"{rev}:{path}")
        return result.stdout if result.returncode == 0 else None


class DomainMap:
    def __init__(self, rules: list[dict[str, str]]) -> None:
        self.rules = rules

    def domain(self, path: str) -> str:
        for rule in self.rules:
            if fnmatch.fnmatch(path, rule["match"]):
                return rule["domain"]
        return "unmapped:" + path.removesuffix(".py")


class Snapshot:
    """One revision of the package, read from git."""

    def __init__(self, git: Git, rev: str, root: str, package: str) -> None:
        self.git, self.rev, self.root, self.package = git, rev, root, package

    def module_path(self, dotted: str) -> str | None:
        """`a.b` → `a/b.py` or `a/b/__init__.py`, relative to the package root."""
        base = dotted.replace(".", "/")
        for candidate in (base + ".py", base + "/__init__.py"):
            if self.git.exists(self.rev, self.root + candidate):
                return candidate
        return None

    def parse(self, path: str) -> ast.Module | None:
        source = self.git.show(self.rev, self.root + path)
        return ast.parse(source) if source is not None else None

    def imports(self, tree: ast.Module) -> set[str]:
        """Package modules a module imports, as paths relative to the package root."""
        prefix = self.package + "."
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == self.package or node.module.startswith(prefix)
            ):
                base = node.module.removeprefix(self.package).lstrip(".")
                for alias in node.names:
                    # `from pkg.a import b` imports module a/b when it exists, else a name in a.
                    sub = f"{base}.{alias.name}".strip(".")
                    path = self.module_path(sub) or self.module_path(base) or base.replace(".", "/") + ".py"
                    found.add(path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(prefix):
                        dotted = alias.name.removeprefix(prefix)
                        found.add(self.module_path(dotted) or dotted.replace(".", "/") + ".py")
        return found


def signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    returns = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    keyword = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    return f"{keyword} {fn.name}({ast.unparse(fn.args)}){returns}"


def public_api(tree: ast.Module) -> dict[str, str]:
    """Public top-level functions and classes, with their public methods and fields."""
    api: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            api[node.name] = signature(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            api[node.name] = f"class {node.name}({bases})"
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                    api[f"{node.name}.{member.name}"] = signature(member)
                elif (
                    isinstance(member, ast.AnnAssign)
                    and isinstance(member.target, ast.Name)
                    and not member.target.id.startswith("_")
                ):
                    field = f"{member.target.id}: {ast.unparse(member.annotation)}"
                    if member.value is not None and len(default := ast.unparse(member.value)) < 40:
                        field += f" = {default}"
                    api[f"{node.name}.{member.target.id}"] = field
    return api


def importers(git: Git, rev: str, root: str, package: str, path: str) -> list[str]:
    """Modules anywhere in the package that import `path` (by text search)."""
    module = package + "." + path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    parent, _, leaf = module.rpartition(".")
    pattern = (
        rf"(from {re.escape(module)} import|import {re.escape(module)}\b"
        rf"|from {re.escape(parent)} import .*\b{re.escape(leaf)}\b)"
    )
    result = git.run("grep", "-l", "-E", pattern, rev, "--", root)
    return sorted({line.split(":", 1)[1].removeprefix(root) for line in result.stdout.splitlines()})


def diff_commit(git: Git, commit: str, root: str, package: str, domains: DomainMap) -> dict:
    before = Snapshot(git, commit + "^", root, package)
    after = Snapshot(git, commit, root, package)
    changed = [
        line.removeprefix(root)
        for line in git.run("show", "--name-only", "--format=", commit).stdout.split()
        if line.startswith(root) and line.endswith(".py")
    ]

    report: dict = {"commit": commit, "files": {}}
    edges_before: set[tuple[str, str]] = set()
    edges_after: set[tuple[str, str]] = set()
    for path in changed:
        tree_before, tree_after = before.parse(path), after.parse(path)
        imports_before = before.imports(tree_before) if tree_before else set()
        imports_after = after.imports(tree_after) if tree_after else set()
        edges_before |= {(path, target) for target in imports_before}
        edges_after |= {(path, target) for target in imports_after}
        api_before = public_api(tree_before) if tree_before else {}
        api_after = public_api(tree_after) if tree_after else {}
        status = "added" if tree_before is None else "removed" if tree_after is None else "modified"
        report["files"][path] = {
            "domain": domains.domain(path),
            "status": status,
            "imports_added": sorted(imports_after - imports_before),
            "imports_removed": sorted(imports_before - imports_after),
            "api_changed": {
                name: {"before": api_before.get(name), "after": api_after.get(name)}
                for name in sorted(set(api_before) | set(api_after))
                if api_before.get(name) != api_after.get(name)
            },
            "importers_before": importers(git, before.rev, root, package, path),
            "importers_after": importers(git, after.rev, root, package, path),
        }

    def lift(edges: set[tuple[str, str]]) -> set[tuple[str, str]]:
        return {(domains.domain(s), domains.domain(t)) for s, t in edges}

    domain_before, domain_after = lift(edges_before), lift(edges_after)
    report["domain_edges_added"] = sorted(domain_after - domain_before)
    report["domain_edges_removed"] = sorted(domain_before - domain_after)
    report["domain_edges_unchanged"] = sorted(domain_after & domain_before)
    return report


def mermaid(report: dict) -> str:
    """Component diagram: changed components plus any reached by a changed edge.

    Unchanged edges are drawn only between those nodes. Added edges are bold,
    removed edges dotted, and new modules get a dashed border.
    """
    touched = {f["domain"] for f in report["files"].values()}
    added = [tuple(e) for e in report["domain_edges_added"]]
    removed = [tuple(e) for e in report["domain_edges_removed"]]
    nodes = touched | {d for edge in added + removed for d in edge}
    unchanged = [
        tuple(e) for e in report["domain_edges_unchanged"] if e[0] in nodes and e[1] in nodes and e[0] != e[1]
    ]
    ids = {name: f"n{i}" for i, name in enumerate(sorted(nodes))}
    modules: dict[str, list[str]] = {name: [] for name in nodes}
    for path, info in report["files"].items():
        modules[info["domain"]].append(path.removesuffix(".py"))
    new_modules = {info["domain"] for info in report["files"].values() if info["status"] == "added"}

    lines = ["flowchart LR"]
    for name in sorted(nodes):
        label = "<br/>".join([name, *sorted(modules[name])])
        lines.append(f'    {ids[name]}["{label}"]')
    lines += [f"    {ids[s]} ==> {ids[t]}" for s, t in added]
    lines += [f"    {ids[s]} -.-> {ids[t]}" for s, t in removed]
    lines += [f"    {ids[s]} --> {ids[t]}" for s, t in unchanged]
    new_ids = [ids[n] for n in sorted(new_modules & {n for n in nodes if n.startswith("unmapped:")})]
    if new_ids:
        lines.append("    classDef new stroke-width:3px,stroke-dasharray:4 2")
        lines.append(f"    class {','.join(new_ids)} new")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("commit", help="commit to diff against its first parent")
    parser.add_argument("--source-root", required=True, help="directory holding the package, e.g. backend/src")
    parser.add_argument("--package", required=True, help="top-level package name")
    parser.add_argument("--domains", required=True, help="domain map YAML, path inside the repo")
    parser.add_argument("--domains-rev", help="revision to read the domain map from (default: COMMIT)")
    parser.add_argument("--mermaid", action="store_true", help="print a Mermaid component diagram instead of JSON")
    args = parser.parse_args()

    git = Git(args.repo)
    root = args.source_root.rstrip("/") + "/" + args.package + "/"
    raw = git.show(args.domains_rev or args.commit, args.domains)
    if raw is None:
        sys.exit(f"domain map {args.domains} not found at {args.domains_rev or args.commit}; pass --domains-rev")
    report = diff_commit(git, args.commit, root, args.package, DomainMap(yaml.safe_load(raw)["rules"]))
    print(mermaid(report) if args.mermaid else json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
