"""Section 10 views for people: a change plan for a pull request, and a static site of the model.

Both are derived from the query core's answers, so they say what `exacly impact` and `exacly show` say.
- `plan`: Markdown for a pull request comment, from `query.change_impact` and `query.gaps`.
- `site`: HTML pages, one for the model's coverage and one per record, with no scripts or outside files.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from exacly_validate import query

Doc = dict[str, Any]
MARKER = "<!-- exacly-plan -->"  # Lets CI find and replace its earlier comment.
TYPES = {
    "Outcome": "Outcomes",
    "Measure": "Measures",
    "Capability": "Capabilities",
    "Process": "Processes",
    "Requirement": "Requirements",
    "Information": "Information",
    "Application": "Applications",
    "Infrastructure": "Infrastructure",
}
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
LINKED_SCHEMES = {"http", "https"}
GOOD = {"passed", "released", "healthy", "target_met"}
BAD = {"failed", "withdrawn", "unavailable", "degraded", "target_not_met"}


def status_words(status: dict[str, dict[str, str]]) -> str:
    return "; ".join(
        f"{context}: " + ", ".join(f"{d} {s}" for d, s in sorted(dims.items()))
        for context, dims in sorted(status.items())
    )


def via(path: list[str], arrow: str) -> str:
    """How an affected record is reached: through a path, or by a change to its own relationships."""
    return "its relationships changed" if len(path) == 1 else f"via {arrow.join(path)}"


def cell(text: Any) -> str:
    """Text safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def plan(change: Doc, gaps: Doc, base: str) -> str:
    """A pull request comment: what the change does to the model, what it affects, and what to check again."""
    records, relationships, impact = change["records"], change["relationships"], change["impact"]
    counts = {kind: len(records[kind]) for kind in ("added", "changed", "removed")}
    lines = [MARKER, "### Exacly plan", ""]
    if not any(counts.values()) and not any(relationships.values()):
        lines.append(f"No change to the model since `{base}`.")
    else:
        summary = ", ".join(f"{n} {kind}" for kind, n in counts.items() if n) or "no records changed"
        rels = sum(len(found) for found in relationships.values())
        lines.append(
            f"Against `{base}`: {summary}"
            + (f", {rels} relationship{'s' * (rels != 1)} added, changed or removed" if rels else "")
            + f". It affects {len(impact['affected'])} other record{'s' * (len(impact['affected']) != 1)}"
            + (f" and reaches {', '.join(impact['outcomes'])}." if impact["outcomes"] else ".")
        )
        lines += ["", "| | Record | Type | Name | Fields |", "| --- | --- | --- | --- | --- |"]
        for kind, sign in (("added", "+"), ("changed", "~"), ("removed", "-")):
            for r in records[kind]:
                fields = ", ".join(f"`{f}`" for f in r.get("fields", []))
                lines.append(f"| {sign} | `{r['id']}` | {r['type']} | {cell(r['name'])} | {fields} |")
        lines += [f"| + | | relationship | `{cell(rel)}` | |" for rel in relationships["added"]]
        lines += [
            f"| ~ | | relationship | `{cell(r['relationship'])}` | {', '.join(f'`{f}`' for f in r['fields'])} |"
            for r in relationships["changed"]
        ]
        lines += [f"| - | | relationship | `{cell(rel)}` | |" for rel in relationships["removed"]]
        if impact["affected"]:
            lines += ["", "<details><summary>Affected records</summary>", ""]
            lines += [
                f"- `{a['id']}` {a['name']}, {via(a['path'], ' → ')}"
                + (f" ({status_words(a['status'])})" if a.get("status") else "")
                for a in impact["affected"]
            ]
            lines += ["", "</details>"]
        if impact["recheck"]:
            lines += ["", "**Check again**", ""]
            for r in impact["recheck"]:
                tests = r["observe"].get("tests")
                if tests:
                    cases = ", ".join(f"`{c}`" for c in tests.get("cases", [])) or "the job's result"
                    lines.append(f"- `{r['id']}` {r['name']}: {cases} in `{tests['workflow']}`")
                else:
                    lines.append(f"- `{r['id']}` {r['name']}: no tests binding, so check it by hand")
    errors, warnings = len(gaps["errors"]), len(gaps["warnings"])
    lines += [
        "",
        f"Validation: **{gaps['result']}**, {errors} error{'s' * (errors != 1)}, "
        f"{warnings} warning{'s' * (warnings != 1)}.",
    ]
    for f in gaps["errors"]:
        lines.append(f"- {f['rule']} `{f.get('record') or f.get('document', '')}`: {f['message']}")
    return "\n".join(lines) + "\n"


CSS = """
:root { --bg:#f4f6f7; --surface:#fff; --surface-2:#eef1f3; --ink:#18212b; --muted:#5b6672; --line:#d8dee3;
  --accent:#3346b8; --good:#24804f; --good-soft:#e2f2e8; --bad:#c2412d; --bad-soft:#f9e4e0; --warn:#a8690f;
  --warn-soft:#f8edd9; --none:#7c8791; --none-soft:#eceff1; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#12171c; --surface:#1a2128;
  --surface-2:#222b33; --ink:#e6ebef; --muted:#9aa6b1; --line:#2f3a44; --accent:#8fa0ff; --good:#5cc48a;
  --good-soft:#1d3428; --bad:#ff8a73; --bad-soft:#3d221d; --warn:#e3ad55; --warn-soft:#3a2f1b; --none:#8d98a2;
  --none-soft:#262e35; color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 system-ui, "Segoe UI", sans-serif;
  padding: 32px max(16px, env(safe-area-inset-left)) 64px; }
main { max-width: 1100px; margin: 0 auto; display: flex; flex-direction: column; gap: 28px; }
h1, h2 { margin: 0; line-height: 1.2; } h1 { font-size: 30px; } h2 { font-size: 20px; }
a { color: var(--accent); } p { margin: 0; }
code, .mono { font-family: ui-monospace, Menlo, monospace; font-size: 0.88em; }
.muted { color: var(--muted); } .eyebrow { font: 12px ui-monospace, Menlo, monospace; text-transform: uppercase;
  letter-spacing: .08em; color: var(--muted); }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile { display: flex; flex-direction: column; gap: 4px; }
.tile .n { font-size: 26px; font-weight: 700; }
.wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; vertical-align: top; padding: 8px 12px; border-bottom: 1px solid var(--line); }
th { font: 500 11.5px ui-monospace, Menlo, monospace; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); background: var(--surface-2); }
tr:last-child td { border-bottom: 0; }
.chip { display: inline-block; font: 11.5px ui-monospace, Menlo, monospace; border-radius: 999px;
  padding: 1px 8px;
  margin: 1px 2px 1px 0; background: var(--none-soft); color: var(--none); white-space: nowrap; }
.chip.good { background: var(--good-soft); color: var(--good); }
.chip.bad { background: var(--bad-soft); color: var(--bad); }
.chip.warn { background: var(--warn-soft); color: var(--warn); }
ul.tree, ul.tree ul { list-style: none; margin: 0; padding-left: 18px; }
ul.tree { padding-left: 0; }
ul.tree li { margin: 3px 0; } .via { font: 12px ui-monospace, Menlo, monospace; color: var(--muted); }
dl { display: grid; grid-template-columns: minmax(120px, max-content) 1fr; gap: 6px 16px; margin: 0; }
dt { color: var(--muted); } dd { margin: 0; } dd ul { margin: 0; padding-left: 18px; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 16px; }
.records th:first-child { width: 50%; } .records th:nth-child(2) { width: 12%; }
.card, dd, td { min-width: 0; overflow-wrap: anywhere; }
@media (max-width: 600px) { dl { grid-template-columns: 1fr; gap: 2px; } dd { margin-bottom: 8px; } }
"""


def e(text: Any) -> str:
    return html.escape(str(text))


def page(title: str, body: str, home: str) -> str:
    return (
        f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>'
        f"<style>{CSS}</style></head><body><main>"
        f'<p class="eyebrow"><a href="{home}">Exacly model</a></p>{body}</main></body></html>\n'
    )


def chip(dimension: str, state: str, context: str | None = None) -> str:
    tone = "good" if state in GOOD else "bad" if state in BAD else "warn" if state in {"inconclusive"} else ""
    label = f"{context}: {dimension} {state}" if context else f"{dimension} {state}"
    return f'<span class="chip {tone}">{e(label.replace("_", " "))}</span>'


def chips(status: dict[str, dict[str, str]]) -> str:
    return "".join(chip(d, s, c) for c, dims in sorted(status.items()) for d, s in sorted(dims.items()))


def page_name(rid: str) -> str:
    """The file name of a record's page. IDs that are not plain names, such as ../index, get a name made of their
    safe characters and a hash, so that no ID can write outside the records folder or onto another page."""
    if SAFE_NAME.fullmatch(rid):
        return f"{rid}.html"
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', rid)}-{hashlib.sha256(rid.encode()).hexdigest()[:12]}.html"


def link(node: Doc, prefix: str = "") -> str:
    return f'<a href="{prefix}{e(page_name(node["id"]))}"><code>{e(node["id"])}</code></a> {e(node["name"])}'


def evidence(ev: Doc) -> str:
    """A link for web evidence; any other locator, such as javascript: or a file path, is shown as text."""
    locator = str(ev["locator"])
    if urlsplit(locator).scheme.lower() in LINKED_SCHEMES:
        return f'<a href="{e(locator)}">{e(ev["claim"])}</a>'
    return f"{e(ev['claim'])} <code>{e(locator)}</code>"


def tree_html(node: Doc) -> str:
    via = node.get("via")
    arrow = f'<span class="via">{e(via["type"])} {"→" if via["direction"] == "out" else "←"}</span> ' if via else ""
    repeated = ' <span class="muted">(above)</span>' if node.get("repeated") else ""
    children = "".join(tree_html(child) for child in node.get("children", []))
    return (
        f'<li>{arrow}{link(node)} <span class="muted">{e(node["type"])}</span>{repeated} '
        f"{chips(node.get('status') or {})}{f'<ul>{children}</ul>' if children else ''}</li>"
    )


def value_html(value: Any) -> str:
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{value_html(v)}</li>" for v in value) + "</ul>"
    if isinstance(value, dict):
        return "<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{value_html(v)}</dd>" for k, v in value.items()) + "</dl>"
    return e(value)


def record_page(model: query.Model, rid: str) -> str:
    shown, traced = query.show(model, rid), query.trace(model, rid)
    r = shown["record"]
    fields = {k: v for k, v in r.items() if k not in {"id", "type", "name", "description"} and v not in ([], {})}
    state = ""
    for context, observed in sorted(shown["state"].items()):
        rows = "".join(
            f"<tr><td>{chip(a['dimension'], a['state'])}</td><td>{e(a.get('reason', ''))}</td><td>"
            + "<br>".join(evidence(ev) for ev in a["evidence"])
            + f'</td><td class="mono">{e(a["at"])}</td></tr>'
            for a in observed.get("assessments", [])
        )
        rows += "".join(
            f'<tr><td><span class="chip warn">pending</span></td><td>{e(w["task"])}</td><td></td>'
            f'<td class="mono">{e(w["opened_at"])}</td></tr>'
            for w in observed.get("pending", [])
        )
        state += (
            f'<h2>{e(context)} <span class="muted mono">config {e(observed["config_revision"][:12])}</span></h2>'
            f'<div class="wrap"><table><tr><th>Assessment</th><th>Reason</th><th>Evidence</th><th>At</th></tr>'
            f"{rows}</table></div>"
        )
    findings = "".join(
        f"<li><b>{e(f['severity'])}</b> <code>{e(f['rule'])}</code> {e(f['message'])}</li>" for f in shown["findings"]
    )
    body = (
        f'<header><p class="eyebrow">{e(r["type"])} · {e(r["lifecycle"])} · {e(shown["document"])}</p>'
        f'<h1><code>{e(r["id"])}</code> {e(r["name"])}</h1><p class="muted">{e(r["description"])}</p></header>'
        f'<div class="cols"><section class="card"><p class="eyebrow">Why it exists</p>'
        f'<ul class="tree">{tree_html(traced["why"])}</ul></section>'
        f'<section class="card"><p class="eyebrow">How it is realized</p>'
        f'<ul class="tree">{tree_html(traced["how"])}</ul></section></div>'
        f"{state or '<p class="muted">Not observed in any context.</p>'}"
        f'<section class="card"><p class="eyebrow">Findings</p>'
        f"{f'<ul>{findings}</ul>' if findings else '<p>None.</p>'}</section>"
        f'<section class="card"><p class="eyebrow">Config</p>{value_html(fields)}</section>'
    )
    return page(f"{r['id']} {r['name']}", body, "../index.html")


def index_page(model: query.Model) -> str:
    gaps = query.gaps(model)
    boundary = model.report.doc.get("model") or {}
    tiles = [
        ("Result", gaps["result"]),
        ("Errors", len(gaps["errors"])),
        ("Warnings", len(gaps["warnings"])),
        ("Not verified", len(gaps["coverage"]["unverified"])),
        ("Not released", len(gaps["coverage"]["unreleased"])),
        ("No binding", len(gaps["coverage"]["unobserved"])),
        ("Pending work", len(gaps["pending"])),
    ]
    contexts = ", ".join(
        f"{c} (serial {s['serial']}, config {s['config_revision'][:12]})" for c, s in sorted(gaps["contexts"].items())
    )
    findings = "".join(
        f"<tr><td>{e(f['severity'])}</td><td><code>{e(f['rule'])}</code></td>"
        f"<td>{link({'id': f['record'], 'name': ''}, 'records/') if f.get('record') in model.records else ''}</td>"
        f"<td>{e(f['message'])}</td></tr>"
        for f in gaps["errors"] + gaps["warnings"]
    )
    sections = ""
    for record_type, heading in TYPES.items():
        rids = sorted(rid for rid, r in model.records.items() if r["type"] == record_type)
        if not rids:
            continue
        rows = "".join(
            f"<tr><td>{link(model.summary(rid), 'records/')}</td><td>{e(model.records[rid]['lifecycle'])}</td>"
            f"<td>{chips(model.status(rid))}</td></tr>"
            for rid in rids
        )
        sections += (
            f'<h2>{heading}</h2><div class="wrap"><table class="records"><tr><th>Record</th><th>Lifecycle</th>'
            f"<th>State</th></tr>{rows}</table></div>"
        )
    body = (
        f"<header><h1>{e(boundary.get('product', boundary.get('namespace', 'Model')))}</h1>"
        f'<p class="muted">{e((boundary.get("scope") or {}).get("covers", ""))}</p>'
        f'<p class="muted">State: {e(contexts or "none")}</p></header>'
        + '<div class="grid">'
        + "".join(
            f'<div class="card tile"><span class="eyebrow">{k}</span><span class="n">{e(v)}</span></div>'
            for k, v in tiles
        )
        + "</div>"
        + (
            f'<h2>Findings</h2><div class="wrap"><table><tr><th>Severity</th><th>Rule</th><th>Record</th>'
            f"<th>Message</th></tr>{findings}</table></div>"
            if findings
            else ""
        )
        + sections
    )
    return page(str(boundary.get("product", "Exacly model")), body, "index.html")


def site(model: query.Model, out: Path) -> list[Path]:
    """Write the site into `out`: index.html, and a page in records/ for each record, removing pages there of
    records no longer in the model. Returns the files written."""
    records = out / "records"
    records.mkdir(parents=True, exist_ok=True)
    written = [out / "index.html"]
    written[0].write_text(index_page(model))
    for rid in sorted(model.records):
        path = records / page_name(rid)
        path.write_text(record_page(model, rid))
        written.append(path)
    for stale in set(records.glob("*.html")) - set(written):
        stale.unlink()
    return written
