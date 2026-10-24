# Exacly validator

Validates an Exacly model against the [Exacly Specification](../spec/README.md) and prints the validation report that section 11 describes. A model is either a 0.1 product map, or 0.2 config and state documents.

```sh
uv tool install "exacly-validate @ git+https://github.com/whiteredhouse/exacly"

# 0.2: a folder of config documents, with state under it if there is any
exacly-validate spec/exacly --revision "$(git rev-parse HEAD)"

# 0.1: one product map
exacly-validate docs/exacly/product-map.yaml --json --date 2026-10-23
```

Given a folder, it reads every YAML and JSON file under it, skipping hidden folders, so a README or the copied spec beside them is ignored. Files and folders can also be given one by one. A document is state when it has state's fields (`context`, `config_revision`, `serial` or `lineage`); every other document is config. `--revision` is the config revision being validated. State computed against another revision is reported. An abbreviated Git commit hash matches the full one; any other revision name, such as `release-1`, must match exactly.

`pip install` works the same way. The package carries the spec's schemas, so it runs anywhere. In Python, `exacly_validate.validate_documents({name: doc, ...}, date, revision)` returns the report for a set of documents, and `exacly_validate.validate(doc, date)` for one document. Neither changes its input.

It exits 0 when the model conforms, with or without exceptions, and 1 when it does not.

Conformance does not establish measurement readiness or observation completeness. An active Outcome linked to a proposed Measure can conform, as can config without state. The validator checks supplied documents; it does not execute observation bindings. [Measurement coverage acceptance criteria](../docs/measurement-coverage-acceptance-criteria.md) describe planned findings beyond these checks.

## Refresh

`exacly-refresh` writes a model's state for one context from what its bindings observe (section 9). It reads test results as JUnit XML reports from one CI run. For each record with a `tests` binding, it writes a verification assessment for the observed config revision:
- **passed** when every selected test case passed;
- **failed** when any selected case failed, naming the failing cases in the reason;
- **not checked** when no selected case ran, with the reason.

A binding without `cases` takes the job's status from `--job-status` instead, and is not checked without it. A record without a binding gets no verification assessment, because a missing binding says nothing about whether the record is tested.

```sh
exacly-refresh spec/exacly --context main --state spec/exacly/state/main.json --junit junit.xml --distribution
```

- **Case IDs.** A test case's ID is its `exacly-case` property in the report, or `<file>::<name>`.
- **Releases.** With `--distribution`, the context is how the product is distributed, for example when users install from main. A Requirement whose active implementer's `implementation_ref` or `operational_ref` exists at the revision is then also assessed as released there, with the reference as implementation evidence.
- **Earlier state.** An existing state file is read first. Its lineage is kept and its serial increases by one. A write replaces only the assessments of the dimensions it observed: refreshing verification keeps an earlier release, other evidence, and records it did not observe, such as a binding for another workflow. Observations and pending work for records or relationships that left the config are dropped. State for another model or context is refused.
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
- **Impact** lists each affected record with the shortest path from a changed one. With `--base`, a record or relationship is changed when any authored field differs, such as a dependency's conditions or a storage role; relationships to other models count too. A relationship added, changed or removed affects the record that relies on the other end, such as the Requirement that gains an implementer or the Application that reads new Information. A removed record's impact is traced in the earlier model. The model may be given by a relative or absolute path inside its Git repository.
- **Gaps** go beyond conformance (section 10's coverage view): a conforming model can still have Requirements that no context has verified or released.

`exacly validate` and `exacly refresh` run `exacly-validate` and `exacly-refresh`. In Python, `exacly_validate.query` gives the same answers as data: `load(documents)`, then `show`, `trace`, `impact`, `gaps`, `changes` and `change_impact`.

## What it checks

1. **Documents.** Each document must match the JSON Schema for its spec version and kind, which is the latest dated iteration in `spec/schema/0.1/`, `spec/schema/0.2/config/` or `spec/schema/0.2/state/`. In 0.2, every document must declare the same spec version (`11.version-mismatch`), and exactly one config document must declare the model boundary (`11.model-boundary`). If any of this fails, the report lists the errors and skips the other checks. Authored evidence that should be observed is a schema error, because the config schema allows only `research` and `decision` evidence.
2. **Section 11 errors.**
   - Duplicate IDs (`11.duplicate-id`).
   - Unresolved references in relationships, exceptions and assessments (`11.unresolved`). A reference to another model is `namespace:ID`; a reference to this model may be qualified with its own namespace.
   - Relationship endpoints section 6 does not allow, and `contains` or `supersedes` between different types (`11.endpoint`). From 0.2, `stores` runs from Infrastructure to Information.
   - Duplicate edges (`11.duplicate-edge`).
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
| `mise run sync` | Installs the validator and development tools into `.venv` from `uv.lock` |
| `mise run test` | Runs the schema, validator and refresh tests with pytest, writing `reports/junit.xml` |
| `mise run lint` | Checks style and formatting with Ruff |
| `mise run typecheck` | Type-checks with mypy in strict mode |
| `mise run examples` | Validates every example in `spec/examples/`: each 0.1 map and each 0.2 folder |
| `mise run model` | Refreshes Exacly's own state from the test results into `reports/state/`, then validates it with the model in [`spec/exacly/`](../spec/exacly/README.md) |
| `mise run check` | All of the above |

To try a model against your working copy, run `uv run exacly-validate path/to/spec/exacly`.
