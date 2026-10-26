# exacly

The `exacly` command and Python package work with an Exacly model, either a 0.1 product map or 0.2 config and state documents, as the [Exacly Specification](../spec/README.md) describes it:

- [`exacly validate`](#validate) checks the model and prints the section 11 validation report.
- [`exacly refresh`](#refresh) writes state from what the model's bindings observe.
- [`exacly show`, `trace`, `impact` and `gaps`](#questions) answer questions about the model, and [`exacly view`](#views) renders them for people.
- [`exacly mcp`](#mcp-server) answers the same questions for coding agents.

```sh
uv tool install exacly    # or: pipx install exacly, pip install exacly
uvx exacly validate spec/exacly    # or run it once without installing
```

Exacly is at 0.x and the specification is a Draft, so commands, output and the model may change in any release until 1.0. Pin a version (`exacly==0.3.0`) where a change would break you. To run the latest main instead, install `"exacly @ git+https://github.com/whiteredhouse/exacly"`.

Before 0.2.0 the package was `exacly-validate`, with `exacly-validate` and `exacly-refresh` commands. Those commands still run, with a note, until the next release.

## Validate

```sh

# 0.2: a folder of config documents, with state under it if there is any
exacly validate spec/exacly --revision "$(git rev-parse HEAD)"

# 0.1: one product map
exacly validate docs/exacly/product-map.yaml --json --date 2026-10-23
```

Given a folder, it reads every YAML and JSON file under it, skipping hidden folders, so a README or the copied spec beside them is ignored. Files and folders can also be given one by one. A document is state when it has state's fields (`context`, `config_revision`, `serial` or `lineage`); every other document is config. `--revision` is the config revision being validated. State computed against another revision is reported. An abbreviated Git commit hash matches the full one; any other revision name, such as `release-1`, must match exactly.

The package carries the spec's schemas, so it runs anywhere. In Python, `exacly.validate_documents({name: doc, ...}, date, revision)` returns the report for a set of documents, and `exacly.validate(doc, date)` for one document. Neither changes its input.

It exits 0 when the model conforms, with or without exceptions, and 1 when it does not.

Conformance does not establish measurement readiness or observation completeness. An active Outcome linked to a proposed Measure can conform, as can config without state. Validation checks supplied documents; it does not execute observation bindings. [Measurement coverage acceptance criteria](../docs/measurement-coverage-acceptance-criteria.md) describe planned findings beyond these checks.

## Refresh

`exacly refresh` writes a model's state for one context from what its bindings observe (section 9). It reads test results as JUnit XML reports from one CI run. For each record with a `tests` binding, it writes a verification assessment for the observed config revision:
- **passed** when every selected test case passed and every pattern in `cases` selected at least one case;
- **failed** when any selected case failed, naming the failing cases in the reason;
- **inconclusive** when the selected cases passed but some pattern selected nothing, such as a test that was renamed or no longer runs, naming those patterns in the reason;
- **not checked** when no selected case ran, with the reason.

A skipped case still counts as selected, so a test that exists but was skipped is not missing. Pass every report from the run, including each shard of a split test job: a pattern whose cases are in a report that was left out selects nothing.

A binding without `cases` takes the job's status from `--job-status` instead, and is not checked without it. A record without a binding gets no verification assessment, because a missing binding says nothing about whether the record is tested.

```sh
exacly refresh spec/exacly --context main --state spec/exacly/state/main.json --junit junit.xml --distribution
```

- **Case IDs.** A test case's ID is its `exacly-case` property in the report, or `<file>::<name>`.
- **Releases.** With `--distribution`, the context is how the product is distributed, for example when users install from main. A Requirement whose active implementer's `implementation_ref` or `operational_ref` exists at the revision is then also assessed as released there, with the reference as implementation evidence.
- **Sources.** Each assessment names its source: `tests` for verification from a `tests` binding, with the binding's `workflow` and `job`, and `distribution` for a release from `--distribution`.
- **Several bindings.** `tests` can be a list of bindings, such as unit tests in one job and end-to-end tests in another. A run observes a binding when the binding's workflow and job match the run's `--workflow` and `--job`; a field the binding leaves out, or the run doesn't name, matches any. Each binding keeps its own result, a run replaces only the results of the bindings it observed, and the record is verified only when every binding passed. A binding's result goes when the binding leaves the config or its workflow or job changes. Bindings observe the model's own repository: a requirement realized partly in another repository is split, with the other part in that repository's own model, linked by a cross-model reference.
- **Earlier state.** An existing state file is read first. Its lineage is kept and its serial increases by one. A write replaces only the assessments of the sources it observed: refreshing tests keeps an earlier release, another observer's assessments such as a manual check, other evidence, and records it did not observe, such as a binding for another workflow. An earlier verification or delivery assessment without a source was written by refresh, so it is read as `tests` or `distribution` and rewritten with it; one of another dimension without a source is kept and named on stderr, since its origin is unknown. Observations and pending work for records or relationships that left the config are dropped. State for another model or context is refused.
- **GitHub Actions.** There, the revision, workflow, job, run link and source link default to the current run.

## Questions

`exacly` answers questions about a model from its config and state, as text for people or JSON (`--json`) for agents and other tools. It reads `spec/exacly` unless `-m PATH` names other documents or folders.

```sh
exacly show REQ-010          # the record, its relationships both ways, its state by context, and its findings
exacly trace APP-002         # why it exists, up to Outcomes, and how it is realized, down to what runs it
exacly impact INF-001        # every record that relies on it, with the path, and the Requirements to check again
exacly impact --base main    # the same for every change since a Git revision: records added, changed and removed
exacly gaps                  # findings, and Requirements not yet verified, released or observed
```

- **Why and how.** Trace follows `supports`, `uses_capability`, `contributes_to`, `implements`, `evaluates` and `stores` toward the reason a record exists, and `uses`, `reads`, `writes`, `depends_on` and `contains` from the reason to what serves it. Information's reason is the Application that reads or writes it, and Infrastructure that stores it serves it. A record reached twice is marked rather than repeated.
- **Impact** lists each affected record with the shortest path from a changed one. With `--base`, a record or relationship is changed when any authored field differs, such as a dependency's conditions or a storage role; relationships to other models count too. A relationship added, changed or removed affects the record that relies on the other end, before and after the change, such as the Requirement that gains an implementer or the Application that reads new Information. A removed record's impact is traced in the earlier model. The model may be given by a relative or absolute path inside its Git repository.
- **Combined results.** A record's assessments of one dimension in one context combine to the state that most needs attention: for verification, failed, then inconclusive, then not checked, then passed. A source bound to the record with no result in that context counts as not checked, so a requirement with tests and a manual check is verified only when both passed. A result that can't be told apart among a source's bindings, such as one that names no binding on a record with several, counts toward none of them until the next refresh drops it. `show` lists every assessment with its source, the combined state, and the bound sources with no result; the other answers give the combined state.
- **Gaps** go beyond conformance (section 10's coverage view): a conforming model can still have Requirements that no context has verified or released. An unverified Requirement lists each source's result by context.

In Python, `exacly.query` gives the same answers as data: `load(documents)`, then `show`, `trace`, `impact`, `gaps`, `changes` and `change_impact`.

### Views

`exacly view` draws section 10's views for people from the same answers:

```sh
exacly view plan --base main   # Markdown for a pull request: what changed, what it affects, what to check again
exacly view site --out site    # HTML pages: coverage and every record, then a page per record
```

On each pull request from this repository, CI comments the plan and replaces its earlier comment, so the review shows what the change does to the model. The site has no scripts or outside files, so any static host can serve it.

## MCP server

`exacly mcp` answers the same questions as an [MCP](https://modelcontextprotocol.io) server on stdio, with one tool per question: `exacly_show`, `exacly_trace`, `exacly_impact` and `exacly_gaps`. Each result has the text answer as content and the JSON answer as structured content. Every call reads the model again, so an agent sees its own edits. In Claude Code, from the repository that holds the model:

```sh
claude mcp add exacly -- uvx exacly mcp
```

This repository's [`.mcp.json`](../.mcp.json) does the same with `uv run`. The [`exacly` skill](../skills/exacly/SKILL.md) tells an agent when to ask and how to keep config true to a change: look up the records first, change Requirements with the behaviour, and check what the branch affects before a pull request. Copy it to `.claude/skills/exacly/` in your repository.

## What validation checks

1. **Documents.** Each document must match the JSON Schema for its spec version and kind, which is the latest dated iteration in `spec/schema/0.1/`, `spec/schema/0.2/config/` or `spec/schema/0.2/state/`. In 0.2, every document must declare the same spec version (`11.version-mismatch`), and exactly one config document must declare the model boundary (`11.model-boundary`). If any of this fails, the report lists the errors and skips the other checks. Authored evidence that should be observed is a schema error, because the config schema allows only `research` and `decision` evidence.
2. **Section 11 errors.**
   - Duplicate IDs (`11.duplicate-id`).
   - Unresolved references in relationships, exceptions and assessments (`11.unresolved`). A reference to another model is `namespace:ID`; a reference to this model may be qualified with its own namespace.
   - Relationship endpoints section 6 does not allow, and `contains` or `supersedes` between different types (`11.endpoint`). From 0.2, `stores` runs from Infrastructure to Information.
   - Duplicate edges (`11.duplicate-edge`).
   - Two bindings of one source on a record that can't be told apart (`11.duplicate-binding`): tests bindings by their `workflow` and `job`, other bindings by their whole content.
   - Containment or replacement cycles (`11.cycle`).
3. **State (0.2).** Each state document is read against the config.
   - State for another model's namespace is an error and is not read (`11.state-namespace`), as is a second state document for the same context (`11.state-context`).
   - Records, relationships and pending work that state names must exist in config (`11.unresolved`). A relationship is named by its `id`, or by its source, type and target.
   - State computed against a config revision other than `--revision` is a warning (`11.state-revision`).
4. **Section 7 traceability.** Rules 7.1 to 7.9, plus active relationships to retired records (`7.retired-target`).
   - A gap in an active record is an error. A gap in a proposed record is a warning, because proposed records may be incomplete.
   - Rule 7.1 (0.2) requires a linked Measure whose lifecycle is proposed or active; a retired Measure does not satisfy it. An active Outcome linked only to retired Measures has an error, and a proposed Outcome has a warning. The 0.1 validator retains its lifecycle-independent Measure link rule.
   - Rule 7.4's business path runs through `supports`, `uses_capability` and `contributes_to`.
   - Rule 7.8 needs an implementer for a released or verified Requirement. Each released delivery assessment needs implementation or deployment evidence, and each passed verification needs test or decision evidence. The evidence must apply to that assessment's own context, because assessments do not carry forward to another release or environment, and must not have expired. A verified Requirement also needs at least one supported release. In 0.2 the assessments come from every state document read, so a release observed in production and a test run observed in CI combine.
   - Rule 7.9 (0.2) asks for exactly one `stores` relationship with role `system_of_record` for each Information record. It is a SHOULD, so a gap is a warning for active and proposed records alike.
5. **Section 11 warnings.**
   - Active records never reviewed or reviewed longer ago than the declared interval (`11.review-overdue`).
   - Unknown baselines (`11.unknown-baseline`).
   - `depends_on` cycles, including a record that depends on itself (`11.dependency-cycle`).
   - Exceptions that are expired, expiring within 30 days, or match no finding.
   - Evidence past its `expires_at` date, in config or state (`11.evidence-expired`). Evidence still applies on the day it expires.

In 0.2, every finding about a document's content names that document: the state document for a claim or an observation, the config document for a relationship or an exception, and otherwise the config document that defines the record.

An exception names the rule code from the report, such as `7.1` or `7.retired-target`, and the record, with or without the model's own namespace. A valid exception waives that error, and the report then says "conforms with exceptions". An expired exception waives nothing.

## Not checked

- Mixing current and proposed state inside one document.
- Whether a `depends_on` that is optional or asynchronous describes its conditions.
- Whether external locators can be reached.
- Whether diagrams are older than the records they describe.
- Whether a state document's serial increased and its lineage stayed the same since the last write.

These need information the documents do not carry, such as the previous state, or a person's judgement.

## Tests

`tests/0.1/cases.yaml` and `tests/0.2/cases.yaml` hold the cases as data. Each is a JSON Patch to the spec's worked example, with the exact findings and result the report must give. In 0.2 the example is a set of named documents, so a patch path starts with the document's name, such as `/billing/records/3`.

## Development

The repository uses [mise](https://mise.jdx.dev/) for its tools (Python and uv) and tasks. `mise run check` is what CI runs.

| Task | What it does |
|---|---|
| `mise run sync` | Installs exacly and the development tools into `.venv` from `uv.lock` |
| `mise run test` | Runs the schema and exacly tests with pytest, writing `reports/junit.xml` |
| `mise run lint` | Checks style and formatting with Ruff |
| `mise run typecheck` | Type-checks with mypy in strict mode |
| `mise run examples` | Validates every example in `spec/examples/`: each 0.1 map and each 0.2 folder |
| `mise run model` | Refreshes Exacly's own state from the test results into `reports/state/`, then validates it with the model in [`spec/exacly/`](../spec/exacly/README.md) |
| `mise run check` | All of the above |

To try a model against your working copy, run `uv run exacly validate path/to/spec/exacly`.
