# exacly

The `exacly` command and Python package work with an Exacly model, either a 0.1 product map or 0.2 config and state documents, as the [Exacly Specification](../spec/README.md) describes it:

- [`exacly validate`](#validate) checks the model and prints the section 11 validation report.
- [`exacly refresh`](#refresh) writes state from what the model's bindings observe.
- [`exacly admit github-actions`](#admit-github-actions-evidence) checks a live CI run against an approved producer policy before deriving verification state.
- [`exacly show`, `trace`, `impact` and `gaps`](#questions) answer questions about the model, and [`exacly view`](#views) renders them for people.
- [`exacly assess`](#assess-an-implementation-target) projects observations onto an explicit implementation and scope.
- [`exacly gate`](#evaluate-readiness-policy) evaluates a named readiness policy against that target, separately from conformance.
- [`exacly plan`](#plan-verification-work) proposes work for verification prerequisites against that target, with an explicit evaluation date.
- [`exacly executions register` and `show`](#register-execution-bindings) record and inspect operator-controlled bindings between existing pending work and reviewed verification contracts.
- [`exacly reconcile github-actions --dry-run`](#preview-verification-reconciliation) previews whether fresh admitted evidence satisfies a selected plan action's verification goal.
- [`exacly mcp`](#mcp-server) answers show, trace, impact and gaps questions for coding agents.

```sh
uv tool install exacly    # or: pipx install exacly, pip install exacly
uvx exacly validate spec/exacly    # or run it once without installing
```

Exacly is at 0.x and the specification is a Draft, so commands, output and the model may change in any release until 1.0. Pin a version (`exacly==0.5.0`) where a change would break you. To run the latest main instead, install `"exacly @ git+https://github.com/whiteredhouse/exacly"`.

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

Verification and delivery are independent: passing bound tests can conform before any release or deployment is observed. A positive claim still needs its own applicable evidence and, for a Requirement, an identified implementer. Missing delivery remains visible in coverage; it does not invalidate the test observation.

Conformance does not establish measurement readiness or observation completeness. An active Outcome linked to a proposed Measure can conform, as can config without state. Validation checks supplied documents; it does not execute observation bindings. [Measurement coverage acceptance criteria](../docs/measurement-coverage-acceptance-criteria.md) describe planned findings beyond these checks.

## Refresh

`exacly refresh` writes a model's state for one context from what its bindings observe (section 9). It reads test results as JUnit XML reports from one CI run. For each record with a `tests` binding, it writes a verification assessment for the observed implementation against the supplied model:

- **passed** when every selected test case passed and every pattern in `cases` selected at least one case;
- **failed** when any selected case failed, naming the failing cases in the reason;
- **inconclusive** when some selected cases passed but others were skipped, or some pattern selected nothing, such as a test that was renamed or no longer runs; the reason names skipped cases and missing patterns;
- **not checked** when no selected case ran, with the reason.

A skipped case still counts as selected, so a test that exists but was skipped is not missing. It cannot establish successful verification: mixed passing/skipped cases retain the run as evidence and report how many selected cases passed and skipped. Pytest expected failures (`xfail`) are also skipped outcomes in JUnit XML. Case IDs repeated across reports retain the existing merge rule: failure wins over pass, and pass wins over skip.

Pass every report from the run, including each shard of a split test job: a pattern whose cases are in a report that was left out selects nothing. A nonempty wildcard cannot identify missing cases that were never included in the supplied reports.

A binding without `cases` takes the job's status from `--job-status` instead, and is not checked without it. A record without a binding gets no verification assessment, because a missing binding says nothing about whether the record is tested.

```sh
exacly refresh spec/exacly --context main --state spec/exacly/state/main.json --junit junit.xml --distribution
```

- **Model and implementation identity.** `--revision` identifies the observed implementation. Optional `--model-revision` identifies the model whose criteria and bindings were used; omitting it keeps the existing behavior, using `--revision` for both. State's `config_revision` names the model revision. Each new verification assessment names both identities in `subject`, while its `context` and evidence `applies_to` name the implementation revision.
- **Case IDs.** A test case's ID is its `exacly-case` property in the report, or `<file>::<name>`.
- **Releases.** With `--distribution`, the context is how the product is distributed, for example when users install from main. A Requirement whose active implementer's `implementation_ref` or `operational_ref` exists at the revision is then also assessed as released there, with the reference as implementation evidence. Distribution refresh requires matching model and implementation revisions, allowing the existing abbreviated Git-hash matching. Distinct revisions are refused because reference existence alone cannot establish distribution of an implementation against a separate model.
- **Sources.** Each assessment names its source: `tests` for verification from a `tests` binding, with the binding's `workflow` and `job`, and `distribution` for a release from `--distribution`.
- **Several bindings.** `tests` can be a list of bindings, such as unit tests in one job and end-to-end tests in another. A run observes a binding when the binding's workflow and job match the run's `--workflow` and `--job`; a field the binding leaves out, or the run doesn't name, matches any. Each binding keeps its own result, a run replaces only the results of the bindings it observed, and the record is verified only when every binding passed. A binding's result goes when the binding leaves the config or its workflow or job changes. Bindings observe the model's own repository: a requirement realized partly in another repository is split, with the other part in that repository's own model, linked by a cross-model reference.
- **Earlier state.** An existing state file is read first. Its lineage is kept and its serial increases by one. A write replaces only the assessments of the sources it observed: refreshing tests keeps an earlier release, another observer's assessments such as a manual check, other evidence, and records it did not observe, such as a binding for another workflow. Retained assessment subjects keep their original identities when the enclosing state's model revision advances. An earlier verification or delivery assessment without a source was written by refresh, so it is read as `tests` or `distribution` and rewritten with it; one of another dimension without a source is kept and named on stderr, since its origin is unknown. Legacy source migration preserves the original observation's identity rather than substituting the new model or implementation revision. Observations and pending work for records or relationships that left the config are dropped. State for another model or context is refused.
- **GitHub Actions.** The implementation revision defaults to `GITHUB_SHA`; workflow, job and run link default to the current run. The automatic source link uses the selected implementation revision, including an explicit `--revision` override. An explicit `--source-url` is preserved.

When approved criteria and the tested implementation have different revisions, supply the approved model files as the positional input and identify both revisions:

```sh
exacly refresh approved-model --context main --state reports/state/main.json \
  --junit reports/junit.xml --model-revision aaaaaaa --revision bbbbbbb
```

The identity flags label the supplied model and report; they do not check out either Git revision, load the model from that revision, or authenticate the report. The caller must supply the intended model files and test results. Distinct identities use the existing state schema. They allow `assess`, `gate` and `plan` to select the approved model and resulting implementation independently. Use [GitHub Actions admission](#admit-github-actions-evidence) when verification state must be derived from a live run checked against an approved producer policy.

In Python, `exacly.refresh.Run(revision=implementation_revision, context=..., observed_at=..., model_revision=model_revision)` supplies the same split. `model_revision` is optional, and `Run.config_revision` gives its effective value, defaulting to `revision`.

### Manual verification

Some requirements are verified by a person running the product, such as a check on a real phone against the deployed backend. The `manual-qa` extension writes those results into state from files people commit, never by hand. Declare it in the model (`extensions: [manual-qa]`) and bind the record to its observations:

```yaml
observe:
  manual-qa:observations:
    path: qa/observations/*.yaml   # relative to the repository root, outside the model folder
    platforms: [ios, android]      # optional; each must pass for the record to count as verified
```

A target manifest per context says what is being assessed now. The release process changes it:

```yaml
# qa/targets/production.yaml
context: production
model_revision: 4f1c2e9          # the config revision the due criteria are numbered at
release: "1.4"
builds:
  app: { version: 1.4.0, digest: "sha256:9b2d..." }
  backend: { revision: b7f3e21 }
due:                              # criteria in scope for this release, numbered from 1
  REQ-001: [1, 2]
```

Each run a tester does is one observation, with its output retained beside it:

```yaml
# qa/observations/2026-10-25T14-12-ios.yaml
observed_by: jane-tester
observed_at: "2026-10-25T14:12:00Z"
record: REQ-001
release: "1.4"
model_revision: 4f1c2e9
platform: ios
device: iPhone 15, iOS 26.1       # descriptive only
builds:
  app: { version: 1.4.0, digest: "sha256:9b2d..." }
  backend: { revision: b7f3e21 }
results:
  1: { result: passed }
  2: { result: failed, reason: Uploaded twice after the connection dropped. }
output:
  file: qa/observations/2026-10-25T14-12-ios.log
  digest: "sha256:51ac..."        # sha256 of the file
  locator: https://chat.example/thread/42   # optional, where it was first shared
```

```sh
exacly observe manual-qa spec/exacly --target qa/targets/production.yaml --state spec/exacly/state/production.json
```

It writes one verification assessment per platform of each binding, with source `manual-qa:observations`, and replaces only that source's assessments, so a tests refresh and a manual observation keep each other's results.

- **What counts.** An observation counts only for the manifest's release and exactly its builds: every build named, each the same. One that leaves out a build, names another platform, has an invalid field such as a failed result without a reason, or whose output is missing or doesn't match its digest, is not used and is named on stderr and in the assessment's `x-unused`. One for another release or other builds is history and is skipped. One read at a `model_revision` other than the one observed, including the manifest's, counts only when the record's criteria are the same there as now; otherwise it is stale. A manifest whose due criteria are numbered at a revision where they differ is refused.
- **Per criterion.** The latest result of each criterion on each platform wins, by `observed_at`, so a later run that leaves a failed criterion out doesn't hide the failure. Two results for one criterion at the same time are an error that names both files.
- **States.** A platform is failed when any criterion's latest result failed, inconclusive when one was inconclusive, passed when every criterion passed, and not checked when it has no usable observation, naming the release and builds. When every criterion due in the release passed but others are open, it is inconclusive and names them, with `x-release-scope: passed`, so the release can ship while the record is not yet verified. The release's scope is only its due criteria: a failure outside them fails the platform but leaves `x-release-scope: passed`.
- **Details.** Each assessment carries `x-platform`, `x-release`, `x-builds`, `x-criteria` (each criterion's latest result) and, when the release has due criteria, `x-release-scope`. Its evidence is each observation's retained output with its digest. `exacly gaps` lists what the extension finds in them under "From extension observers": a release whose scope passed while the record is not verified, and observations that were not used.
- **Validation.** When the model declares the extension, the validator checks each `manual-qa:observations` binding (`11.extension-binding`): `path`, and optionally `platforms`, nothing else. It also checks the `x-` fields on this source's assessments in state (`11.extension-assessment`), and `gaps` reports an assessment whose fields are not valid instead of reading them. The binding, assessment, target and observation schemas are `BINDING_SCHEMA`, `ASSESSMENT_SCHEMA`, `TARGET_SCHEMA` and `OBSERVATION_SCHEMA` in `exacly.manual_qa`. `observed_at` must be a timestamp with its offset, such as `2026-10-25T14:12:00Z`.

## Admit GitHub Actions evidence

`exacly admit github-actions` reads a live GitHub Actions run and artifact through authenticated GitHub APIs, checks them against an independently approved trust policy, and derives fresh verification state from the accepted JUnit report. Admission requires conforming 0.2 config; approved model inputs cannot contain observed state. It accepts neither caller-authored state nor offline run metadata as evidence of origin.

Each trust policy supplies its exact protected branch, workflow path, job ID and name, verification/package/upload step names, artifact name prefix and observer paths. The library supplies no repository layout or producer defaults. The supported producer contract remains one job in a push workflow on the approved branch.

Exacly's own [producer proposal](../ci/evidence-producer.yaml) selects [the verification evidence workflow](../.github/workflows/verification-evidence.yml). This example runs on pushes to `main`, has one job named `Observe verification`, explicitly selects the pytest configuration with `-c pyproject.toml`, and packages `manifest.json` and `junit.xml` in an artifact named `exacly-evidence-RUN-ATTEMPT`. Other repositories supply their own contract. The packaging helper checks the checkout and supported run metadata before producing the bundle; admission independently compares that metadata with GitHub responses and the approved policy.

### Approve a producer policy

The trust policy names the repository by numeric ID and full name, the exact approved branch, `workflow` with its path, byte digest, job ID/name and step names, `artifact.name_prefix`, `observer` with approved paths and digest, and the approved model revision, file list and config digest. The model revision and selected implementation revision must be full Git commit SHAs. The implementation may be a newer revision whose approved workflow and observer fingerprint still match.

Observer paths are literal, safe repository-relative file or directory selectors, with no glob patterns. Each selector pins that file or directory subtree. An explicitly selected absent path pins its absence, so adding it later changes the fingerprint; removing or changing selected content also changes it. Reviewers must check that the selectors cover the observer's execution inputs, including applicable configuration and ancestor test hooks. Selectors identify Git content and absence; they do not discover a complete runtime dependency closure or prove test honesty.

Prepare a candidate policy from an already reviewed commit containing the producer workflow. Run from a checkout of that revision to read its producer proposal, set `EXACLY_APPROVED_REVISION` to its full SHA and provide a read-permission GitHub token through `GH_TOKEN`:

```python
import json
import os
from pathlib import Path

import yaml

from exacly.admission import propose_policy
from exacly.github_evidence import GitHubClient

source = GitHubClient(os.environ["GH_TOKEN"])
proposal = yaml.safe_load(Path("ci/evidence-producer.yaml").read_text())
candidate = propose_policy(
    source,
    "whiteredhouse/exacly",
    os.environ["EXACLY_APPROVED_REVISION"],
    [
        "spec/exacly/model.yaml",
        "spec/exacly/validation.yaml",
        "spec/exacly/observation.yaml",
        "spec/exacly/understanding.yaml",
        "spec/exacly/planning.yaml",
        "spec/exacly/admission.yaml",
        "spec/exacly/reconciliation.yaml",
        "spec/exacly/executions.yaml",
    ],
    producer=proposal["producer"],
    observer_paths=proposal["observer_paths"],
)
Path("admission-policy.candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")
```

`producer` must explicitly supply `branch`, `workflow` with `path`, `job_id`, `job_name` and `steps` (`verification`, `package`, `upload`), and `artifact.name_prefix`. `observer_paths` is also required. The helper computes the workflow, observer and model fingerprints from the selected revision.

Review both the proposal and generated policy independently of the implementation being evaluated, then keep the approved policy in controlled storage outside model directories. A repository proposal is not an approved policy. Generating hashes from candidate content does not approve its workflow, observer paths or model. The helper prepares policy content; it grants no execution authority or approval.

### Admit an exact run attempt

The target selects a named context, the policy's approved `model_revision`, and the observed implementation `revision`. The context is independent of the producer branch; Exacly's example uses `main`. After approving the policy, set `EXACLY_IMPLEMENTATION_REVISION` to the full implementation SHA from the intended run and prepare the target:

```python
import json
import os
from pathlib import Path

approved = json.loads(Path("trusted/github-verification.json").read_text())
target = {
    "context": "main",
    "model_revision": approved["model"]["revision"],
    "revision": os.environ["EXACLY_IMPLEMENTATION_REVISION"],
}
Path("targets").mkdir(exist_ok=True)
Path("targets/main.json").write_text(json.dumps(target, indent=2) + "\n")
```

Supply the run ID and exact attempt to evaluate, an explicit date and a new output directory:

```sh
exacly admit github-actions --trust-policy trusted/github-verification.json \
  --target targets/main.json --run "$EXACLY_RUN_ID" --attempt "$EXACLY_RUN_ATTEMPT" \
  --date 2026-10-28 --out reports/admission-run --json
```

`--date` supplies the UTC evaluation date for model conformance and expiry checks. A producer job completed after that UTC date is rejected. The live branch-protection check records its actual timestamp separately from the receipt's `as_of` date.

The command reads `GH_TOKEN`, falling back to `GITHUB_TOKEN`; it has no command-line token option. The token needs read access to the repository content, workflow runs, jobs and artifacts used by admission.

Admission checks the repository identity, current protection of the approved branch, a push run on that exact branch for the selected implementation SHA and attempt, the configured workflow and single observation job, matching workflow and observer fingerprints, and one uniquely named artifact created during the job. The artifact name is the approved prefix followed by the run ID and attempt. Admission verifies the API's archive digest, the strict manifest and report digest, and the ZIP contents. Approved model files are fetched at their full revision through GitHub and checked against the policy's config digest before refresh derives state with that model and implementation identity.

GitHub's artifact metadata does not directly identify its producing job or attempt. The receipt therefore records that association as an inference from the approved single-job producer contract, exact artifact name and job timing. Current branch protection is an admission precondition; its `branch_protection_checked_at` timestamp does not prove historical protection at the evaluation date or when the run occurred.

A completed failed verification step can supply evidence of failure. If its report contains positive assessments despite the step failing, admission downgrades those positives to `inconclusive`. A skipped or cancelled verification step, or a missing report, is rejected.

For an admission decision, the new output directory contains `receipt.json`. An admitted result also retains `archive.zip`, `junit.xml` and freshly derived `state.json`; a rejected result saves no report or state. Output is written atomically to a new directory. Exit status is 0 for admitted, 1 for rejected, and 2 for invalid inputs or output errors.

The receipt establishes only the checked evidence origin and target applicability. It records `test_honesty_verified: false`, `coverage_verified: false` and `self_authenticating: false`: a trusted producer can run weak or misleading checks, and a copied or edited receipt is not independently authenticated. Changes to acceptance coverage still need review. Retain admission output in protected storage or revalidate the live source before using it for a later decision. This command does not sign receipts, dispatch work or close pending tasks.

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
   - A declared extension's bindings, or the fields it writes on assessments, that break its schemas (`11.extension-binding`, `11.extension-assessment`), such as a `manual-qa:observations` binding with a field it doesn't define.
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
   - Rule 7.8 needs an implementer for a released or verified Requirement. Each released delivery assessment needs implementation or deployment evidence, and each passed verification needs test or decision evidence. The evidence must apply to that assessment's own context, because assessments do not carry forward to another release or environment, and must not have expired. In 0.2 verification and delivery are independent; each assessment stands on its own evidence. The historical 0.1 product-map rule still requires a supported release for a verified Requirement. Use explicit targets to evaluate implementation-specific readiness.
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

## Assess an implementation target

`exacly assess` answers a narrower question than the context summary: which observations establish verification and delivery for this exact implementation and scope?

Keep target files outside the model folder, since model folders recursively load YAML and JSON:

```yaml
# targets/main.yaml
context: main
model_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

```sh
exacly assess REQ-001 -m spec/exacly --target targets/main.yaml --date 2026-10-26
exacly assess REQ-001 -m spec/exacly --target targets/main.yaml --json
```

A target requires `context`, `model_revision` and at least one of `revision` or `builds`. `builds` maps component names to identity-field maps, for example `app: {digest: 'sha256:…'}` and `backend: {revision: '…'}`. Optional `environment`, `platform` and `release` restrict the question. For delivery, an omitted environment selects delivery to the named `context`; for verification, omitting it makes no environmental verification claim. Every requested identity must be established by each observation used. Build compositions match exactly; revisions use the validator's Git-abbreviation matching. An observation scoped to an environment, platform or release cannot establish a broader claim: select that scope explicitly (delivery environment defaults to context). For manual QA, include its exact builds, release, environment and selected platform.

New refresh observations carry their own `subject` with model and implementation revision, supplied separately when `--model-revision` is used; distribution observations also name the environment and require matching revisions. Manual QA carries the model revision, builds, release, environment and platform from its target. Earlier refresh test observations can establish their documented config/source revision; generic legacy delivery cannot establish an implementation identity. No identity is recovered from prose or a newer enclosing state's config revision.

The report includes applicable assessments, exclusions with reasons, and missing bound sources. Expired or unsupported positive evidence does not count. Delivery with no applicable result is displayed as `not_observed`, a derived label rather than a stored delivery state. All required bindings must have applicable results; source-level tests do not establish direct artifact testing or deployed-system verification. The command is read-only and returns 0 when it answers the question, including when verification is not established; invalid input returns 2. `--date` sets the as-of date for expiry, defaulting to today.

Context summaries used by `show`, `trace`, `gaps` and generated views also guard against combining refresh tests from different revisions. Use `assess` for implementation-specific conclusions; context summaries are not release gates. Original observations stay available when they are excluded from a selected target.

## Evaluate readiness policy

`exacly gate` evaluates declared prerequisites against an explicit target using the same applicability rules as `assess`. It does not deploy anything or modify config, state, policy, or targets. Keep policies outside model folders:

```yaml
# policies/main-readiness.yaml
name: main-readiness
version: '1'
requirements:
  - record: REQ-001
    dimensions: [verification, delivery]
```

```sh
exacly gate -m spec/exacly --policy policies/main-readiness.yaml --target targets/main.yaml
exacly gate -m spec/exacly --policy policies/main-readiness.yaml --target targets/main.yaml --date 2026-10-26 --json
```

Each check names a Requirement and requires `verification` (passed) or `delivery` (released), or both. Checks can be repeated for different records, but overlapping checks—including qualified references to the same record—and repeated dimensions are rejected. Name, version and requirements are mandatory; empty policies and unknown fields are rejected. Increment the policy version when its prerequisites change.

- **`satisfied`:** all requested checks have applicable support and every required binding has a result.
- **`not_satisfied`:** an applicable check failed, or delivery is explicitly withdrawn, planned, or in progress. A negative result dominates unknown prerequisites.
- **`unknown`:** a result is absent, not checked, inconclusive, expired, unsupported, or has mismatched/ambiguous identity or scope. A nonconforming model also prevents a readiness conclusion and includes its validation findings.

Exit status is 0 only for `satisfied`; 1 for `not_satisfied` or `unknown`; 2 for invalid policy, target, or unreadable/schema-invalid input. Unknown readiness can therefore block CI without pretending a test failed. Expired negative evidence is retained historically but cannot establish a current failure. A recorded negative assessment without evidence can still express a failure by its required reason, as allowed by the specification.

Reports identify policy name/version, selected target, as-of date, individual checks, evidence, excluded assessments, and missing bindings. Delivery environment defaults to target context when omitted; verification without environment establishes only the selected source-level result. An environment-specific gate requires environment-specific verification evidence. A source check plus publication gate does not establish production behavior. The command evaluates only the explicit policy; it is not a general proof of release safety, artifact provenance, or complete acceptance-criteria coverage.

## Plan verification work

`exacly plan` proposes the next observation, investigation or configuration decision for each verification prerequisite in a named policy. It uses the same target applicability and readiness evaluation as `assess` and `gate`. A passing result in another revision or environment cannot satisfy the selected goal.

Use a verification-only policy and an explicit evaluation date:

```yaml
# policies/verification.yaml
name: verification-work
version: '1'
requirements:
  - record: REQ-001
    dimensions: [verification]
```

```sh
exacly plan -m spec/exacly --policy policies/verification.yaml \
  --target targets/main.yaml --date 2026-10-28
exacly plan -m spec/exacly --policy policies/verification.yaml \
  --target targets/main.yaml --date 2026-10-28 --json --out reports/plan.json
```

`--date` is required so that evidence expiry has an explicit evaluation time. Policies containing delivery checks are rejected in this first version. `exacly view plan --base main` remains the PR model-change view; the top-level `plan` command evaluates verification prerequisites for a selected implementation.

Each check retains its readiness result, evidence, exclusions and missing bindings. The proposed next step follows this order:

| Finding | Proposal |
| --- | --- |
| Applicable evidence satisfies the prerequisite | No action. |
| An applicable verification result failed | `investigate_failure`, retaining the failure reason even if other results are missing. |
| Verification remains unknown and the Requirement has no verification binding | `blocked_configuration`, requesting an observation configuration decision. |
| Other unknown verification, including absent, skipped, mismatched or expired results | `collect_evidence`, explaining the observations still needed. |

A schema-valid model that does not conform produces a `blocked` plan with validation findings and no action proposals. A valid plan with proposals has status `work_proposed`; one whose prerequisites are all satisfied has status `satisfied`. Missing evidence never becomes an implementation task automatically.

The saved JSON has `schema_version: "0.1.0"` and carries tool and model identity, the target, policy identity and digest, evaluation date, config and state fingerprints, state lineage and serials, checks, actions and a plan digest. Each action identifies its Requirement, dimension, reason, criteria and binding fingerprints, and observable completion condition. Identical inputs and evaluation date produce the same plan; authored array order remains significant. Action IDs exclude the evaluation date, state serial and mutable diagnostic reasons, so those changes preserve identity for the same unresolved goal and proposal kind. A changed policy, target, criteria, binding or proposal kind changes the relevant action identity.

The default human-readable view and `--json` render the same plan. `--out` saves that plan as JSON; keep it outside model directories, including the static folder prefix of a model glob such as `spec/exacly/*.yaml`. Output paths that resolve to model, state, policy or target inputs are refused, including symlink aliases. Planning leaves those inputs untouched.

Exit status is 0 when a plan is produced, including a blocked plan or one with unresolved prerequisites, and 2 for invalid inputs or an output error. Use `gate` when CI needs a readiness exit status.

Plans are advisory. The command does not create tasks, dispatch agents, change observations or close pending work. Its fingerprints identify supplied content; they do not authenticate an evidence producer or authorize execution. Use a [reconciliation preview](#preview-verification-reconciliation) to check a selected action against fresh live admission. Actual task closure remains separate work, as described in the [plan and apply research](../docs/automated-plan-apply-research.md).

In Python, `exacly.planning.create(documents, policy_doc, target, date)` produces the same plan from named documents, a policy, an `exacly.applicability.Target` and a date. `exacly.planning.as_text(plan)` renders it for people, and `exacly.planning.SCHEMA` defines the JSON artifact contract.

## Register execution bindings

`exacly executions register` records an operator's explicit binding between one existing pending verification task and a reviewed plan action, readiness policy, producer trust policy and output target. Registration is offline: it requires no GitHub token and makes no live producer or verification claim. It writes only the selected execution ledger, leaving the task, state and input documents unchanged.

Select the pending entry by the action's Requirement, task identifier and opening time. `--state` supplies the state containing that entry; `--target` supplies the planned output implementation to evaluate later. Set `EXACLY_LEDGER_FILE` to a ledger path in a pre-existing, operator-owned directory with POSIX mode `0700`, outside Git working trees, model folders and input paths:

```sh
exacly executions register --ledger "$EXACLY_LEDGER_FILE" --plan reports/plan.json \
  --action "$EXACLY_ACTION_ID" --state observations/current.json \
  --task "$EXACLY_TASK_ID" --opened-at 2026-10-28T12:00:00Z \
  --policy policies/verification.yaml --trust-policy trusted/github-verification.json \
  --target targets/output.json --date 2026-10-28 --json
```

Pending work with `dimension: verification` can be registered directly. If its dimension is absent, registration requires `--adopt-verification` as an explicit operator choice; this does not add the dimension to the source state. Other dimensions are rejected.

The evaluation date cannot precede the plan or the source state's UTC writing date, and the selected pending task cannot have opened after that state was written. Opening timestamps are compared in UTC.

The versioned binding captures namespace, context and lineage; canonical Requirement ID, task identifier and UTC opening time; the complete pending-entry digest and adopted dimension; the selected plan/action, criteria, bindings, policies and output target; and the historical state serial, config revision and digest, registration timestamp and explicit evaluation date. The plan's content digest alone is not approval: registration must run in the operator's controlled context with reviewed inputs.

The ledger is an explicitly selected private SQLite database using POSIX ownership, permissions and file locks, with file mode `0600`. It is not discovered from a repository and does not import JSON binding files. Directory ownership and permissions protect its storage boundary, but they do not isolate candidate code running as the same operating-system user. Run registration separately from code that is not authorized to change these bindings.

Repeating a registration for the same pending entry and contract is idempotent and returns the original record, including its first `as_of`, `registered_at` and historical state snapshot. A different contract for an already active pending key conflicts unless `--supersedes` names its exact current binding ID. Explicit supersession atomically appends a replacement and leaves the original immutable record visible as inactive. A concurrent change to the active binding makes that replacement fail rather than silently overwrite history.

The store keeps the latest registered state serial and digest for each pending identity. Registration rejects an older state or different content at the same serial for that identity. Registering the same contract against a newer state advances this watermark without rewriting the original binding. A later refresh with the same pending entry can still be evaluated; read-only previews never advance the watermark.

The watermark guards registered history for that pending item. It does not authenticate global state freshness or prove that the supplied file is the latest filesystem snapshot; a future state-changing closure operation still needs an exact-state concurrency check.

Inspect a binding without changing the database:

```sh
exacly executions show --ledger "$EXACLY_LEDGER_FILE" --binding "$EXACLY_BINDING_ID" --json
```

Inspection requires an existing valid ledger; unknown schemas and existing journal or WAL sidecars are refused. Read-only operations do not initialize, migrate or recover the store. In Python, `exacly.executions.Registration` describes the explicit registration inputs; `register(ledger_path, request)` writes the binding, `get(ledger_path, binding_id)` reads it, and `as_text(record)` renders the result. `exacly.executions.SCHEMA` defines each immutable registration record's `0.1.0` format; inspection returns that record together with its current active status and watermark.

## Preview verification reconciliation

`exacly reconcile github-actions --dry-run` checks whether fresh admitted evidence satisfies the verification goal of one existing action in a saved plan. For evidence evaluation it revalidates the live producer and report through [GitHub Actions admission](#admit-github-actions-evidence), fetches the approved model through that path, and evaluates only freshly derived state. It accepts no saved receipt, executor-authored state or local model files as a substitute for those live reads.

The readiness policy supplied with `--policy` must match the policy used to make the plan. The separate `--trust-policy` authorizes the producer and approved model for live evidence admission. Keep both policies under their respective review controls; a saved plan's content digest checks consistency and does not establish approval or execution authority.

Supply an existing action ID from the plan and a target for the implementation whose result is being checked:

```sh
exacly reconcile github-actions --dry-run --plan reports/plan.json \
  --action "$EXACLY_ACTION_ID" --policy policies/verification.yaml \
  --trust-policy trusted/github-verification.json --target targets/output.json \
  --run "$EXACLY_RUN_ID" --attempt "$EXACLY_RUN_ATTEMPT" --date 2026-10-28 \
  --out reports/reconciliation.json --json
```

`--dry-run` is required, and the evaluation date cannot precede the saved plan's date. This first version supports source-verification targets containing only `context`, `model_revision` and `revision`, with full 40-character model and implementation commit SHAs. The output target must retain the planned model revision and context. Its implementation revision may differ from the original target, or remain the same when collecting evidence for an existing implementation. The model namespace, approved config, criteria, bindings and readiness policy must still match the plan.

The report identifies the plan, selected action, original and output targets, live admission checks and verification findings. Its status is:

| Status | Meaning | Exit status |
| --- | --- | --- |
| `ready` | Fresh admitted evidence satisfies the selected action's verification goal against the unchanged planned contract. | 0 |
| `pending` | Admission or applicable verification is insufficient, missing, incomplete or failed. | 1 |
| `stale` | The readiness policy or approved model contract changed; make a new plan. | 1 |

A selected action can be `ready` while other requirements in the readiness policy remain unsatisfied. The report gives whole-policy readiness separately; readiness of one action does not establish readiness of the whole product or release. Invalid plan schema or digest, inconsistent action/check/completion identity, unknown action IDs and invalid inputs return 2.

Each evaluation of evidence performs live admission for the exact run attempt and explicit date; a stale policy or scope can be reported before those reads. Repeating a preview leaves input documents, observations and pending work untouched; the result can change when the live source or applicable evidence changes. The default text view and `--json` render the same report. `--out` optionally saves that report outside input files and model folders, with input aliases protected.

To check the registered pending-task binding as well, add all three of `--ledger`, `--binding` and `--state`:

```sh
exacly reconcile github-actions --dry-run --plan reports/plan.json \
  --action "$EXACLY_ACTION_ID" --policy policies/verification.yaml \
  --trust-policy trusted/github-verification.json --target targets/output.json \
  --run "$EXACLY_RUN_ID" --attempt "$EXACLY_RUN_ATTEMPT" --date 2026-10-28 \
  --ledger "$EXACLY_LEDGER_FILE" --binding "$EXACLY_BINDING_ID" \
  --state observations/current.json --json
```

The options are all-or-none. Bound previews open an existing ledger read-only and check that the binding is active, still identifies the exact pending entry, and matches the selected plan, goal, policies and output target. The supplied current state must match the registered model revision, context and lineage, must not regress below the store's registered state watermark, and must preserve the complete pending entry. A higher state serial is allowed when those conditions still hold. Mismatches produce `stale`; invalid inputs or ledger errors return 2. These checks do not replace fresh live evidence admission.

A successfully checked bound preview records `claims.registered_binding_verified: true`. An unbound preview remains supported and records that claim as false, without implying that any pending-task binding was registered or checked. Neither mode removes pending work or changes the ledger or its watermark.

Bound previews check the binding before fetching evidence and again immediately before reporting `ready`. The binding's `checked_at` records the last read. These are read snapshots; a future completion writer must still check its state and authorization preconditions atomically when committing a change.

The report is advisory and unsigned. It does not prove that an executor produced the selected output revision, authorize task closure, dispatch work or maintain the execution ledger. Evidence origin, applicability and the admission limits on test honesty and acceptance coverage remain visible; an executor completion claim alone cannot make an action ready.

In Python, `exacly.reconciliation.preview(source, plan_doc, action_id, readiness_policy, trust_policy, output_target, run_id, attempt, date)` returns the same read-only preview using a live GitHub source and an `exacly.applicability.Target`. Optional keyword arguments `ledger`, `binding_id` and `state_doc` request the bound check and must be supplied together.

`exacly.reconciliation.SCHEMA` defines the `0.2.0` JSON report. Existing unbound command and Python calls remain supported, but JSON consumers of report version `0.1.0` must handle the new version and required fields: `binding` is null or an object, and `claims.registered_binding_verified` is a boolean. Saved preview reports remain advisory artifacts rather than ledger registrations.

The [live rehearsal report](../docs/verification-reconciliation-rehearsal.md) records an actual producer artifact, admission and reconciliation preview, including the transport coverage and environment limitation.
