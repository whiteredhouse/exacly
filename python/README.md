# exacly

The `exacly` command and Python package work with an Exacly model, either a 0.1 product map or 0.2 config and state documents, as the [Exacly Specification](../spec/README.md) describes it.

This reference describes **0.6.0rc1**, a release candidate for product context,
evidence-based verification and recoverable local task completion. The new
commands run in an operator-controlled environment. That release excludes
candidate isolation, task dispatch and executor supervision (REQ-025–027).
The [isolation section](#isolate-a-candidate-command) describes additional
unreleased functionality on main. Review the
[release notes and upgrade paths](../CHANGELOG.md) for report-format changes and
explicit ledger-copy upgrades.

- [`exacly validate`](#validate) checks the model and prints the section 11 validation report.
- [`exacly refresh`](#refresh) writes state from what the model's bindings observe.
- [`exacly admit github-actions`](#admit-github-actions-evidence) checks a live CI run against an approved producer policy before deriving verification state.
- [`exacly show`, `trace`, `impact` and `gaps`](#questions) answer questions about the model, and [`exacly view`](#views) renders them for people.
- [`exacly assess`](#assess-an-implementation-target) projects observations onto an explicit implementation and scope.
- [`exacly gate`](#evaluate-readiness-policy) evaluates a named readiness policy against that target, separately from conformance.
- [`exacly plan`](#plan-verification-work) proposes work for verification prerequisites against that target, with an explicit evaluation date.
- [`exacly executions register` and `show`](#register-execution-bindings) record and inspect operator-controlled bindings between existing pending work and reviewed verification contracts.
- [`exacly executions inspect`, `backup` and `recover`](#maintain-an-execution-ledger) diagnose, back up and recover an explicitly selected ledger without changing its source files.
- [`exacly executions upgrade`](#upgrade-a-ledger-for-completion), [`index` and `audit`](#create-and-audit-an-integrity-index-copy), and [`activate`](#activate-an-operational-indexed-ledger) create or inspect explicitly selected ledger copies.
- [`exacly reconcile github-actions --dry-run`](#preview-verification-reconciliation) previews whether fresh admitted evidence satisfies a selected plan action's verification goal.
- [`exacly complete github-actions` and `exacly completions show` / `recover`](#complete-registered-verification-work) complete one registered local task from admitted evidence and inspect or resolve interrupted completion.
- [`exacly isolate`](#isolate-a-candidate-command) runs one explicitly authorized command through a verified Linux boundary and retains untrusted outputs.
- [`exacly mcp`](#mcp-server) answers show, trace, impact and gaps questions for coding agents.

```sh
uv tool install 'exacly==0.6.0rc1'
uvx --from 'exacly==0.6.0rc1' exacly validate spec/exacly    # one-off run
```

The exact pin selects this prerelease; `uv tool install exacly` selects the latest
stable release instead. Python 3.12 or newer is required. Exacly is at 0.x and the
specification is a Draft, so commands, output and the model may change in any
release until 1.0. Pin the package version where a change would break you. To run
the latest main instead, install
`"exacly @ git+https://github.com/whiteredhouse/exacly"`.

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

### Conditional state publication

`refresh` and `observe manual-qa` capture the existing state before computing observations. Both publish through the same local POSIX writer: under a parent-directory lock, the writer checks the captured exact file content and its namespace, context, lineage and serial before replacing it. Initial creation also checks that the destination is still absent. Existing state keeps its lineage and advances its serial by exactly one.

If another observer writes first, the command exits 1 without overwriting its result. Run the observation again to read and retain that newer state; a stale computed output is never automatically retried. Both commands accept a finite nonnegative `--lock-timeout`, defaulting to 30 seconds; zero makes one immediate lock attempt. This controls lock waiting rather than the total observation time. Invalid state, unsupported destinations and write errors also exit 1 without a success message.

The writer synchronizes a hidden staging file, atomically replaces the destination, then synchronizes its directory before reporting success. Failure before replacement leaves the old state unchanged. If directory synchronization fails after replacement, the command reports that publication may already have happened and durability is uncertain; inspect the state before retrying. A killed process may leave its hidden staging file, which is not loaded as a model document and does not block later writes.

These guarantees require cooperating writers on a supported local POSIX filesystem. Arbitrary file editors do not honor the shared lock, and network filesystem behavior is not guaranteed. The writer requires 0.2 state documents and refuses duplicate JSON keys, nonfinite values, and symbolic-link, hard-linked or nonregular destinations. Existing permission bits are preserved; new state files use mode `0600`. Pure `refresh()` and `manual_qa.observe()` calls still compute documents without writing files; this publication protocol does not make state and an execution ledger one transaction or close pending tasks.

For a Python caller that writes local state, `exacly.state_writer.read(path)` returns a snapshot whose `document` property provides an independent parsed copy, or `None` when absent. Compute the next document from that copy, then call `exacly.state_writer.publish(snapshot, next_state, lock_timeout=30)`. Keep the snapshot unchanged as the expected input to publication; a conflict needs a new snapshot and computation.

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
        "spec/exacly/execution-maintenance.yaml",
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

Plans are advisory. The command does not create tasks, dispatch agents, change observations or close pending work. Its fingerprints identify supplied content; they do not authenticate an evidence producer or authorize execution. Use a [reconciliation preview](#preview-verification-reconciliation) to check a selected action against fresh live admission, or explicitly authorize [local completion](#complete-registered-verification-work) for an existing registered verification task.

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

Registration initializes new ledgers in schema version 1. Existing operational ledgers in versions 1, 2 and 4 retain their format; version 3 remains audit-only, and normal reads and registration never upgrade or activate a ledger. [Completion](#complete-registered-verification-work) supports version 2 through the explicit [copy-only upgrade](#upgrade-a-ledger-for-completion), or version 4 through separate [indexed activation](#activate-an-operational-indexed-ledger).

Repeating a registration for the same pending entry and contract is idempotent and returns the original record, including its first `as_of`, `registered_at` and historical state snapshot. A different contract for an already active pending key conflicts unless `--supersedes` names its exact current binding ID. Explicit supersession atomically appends a replacement and leaves the original immutable record visible as inactive. A concurrent change to the active binding makes that replacement fail rather than silently overwrite history.

An unresolved prepared completion intent blocks registration and supersession for that pending identity. A completed pending identity cannot be registered or reused; aborted intents remain in history and allow a fresh attempt. Resolve an interrupted completion through its [completion recovery command](#complete-registered-verification-work).

The store keeps the latest registered state serial and digest for each pending identity. Registration rejects an older state or different content at the same serial for that identity. Registering the same contract against a newer state advances this watermark without rewriting the original binding. A later refresh with the same pending entry can still be evaluated; read-only previews never advance the watermark.

The watermark guards registered history for that pending item. It does not authenticate global state freshness or prove that the supplied file is the latest filesystem snapshot. Local completion additionally checks the actual current state and active binding under commit-time locks.

Read a binding without changing the database:

```sh
exacly executions show --ledger "$EXACLY_LEDGER_FILE" --binding "$EXACLY_BINDING_ID" --json
```

Show and bound-preview reads require an existing valid ledger; unknown schemas and existing journal or WAL sidecars are refused. They do not initialize, migrate or recover the store. Use the explicit maintenance commands below to diagnose a refused ledger.

All ledger commands accept `--lock-timeout SECONDS`, with a default of 30 seconds. The value must be finite and nonnegative; `0` makes an immediate lock attempt and can succeed when the ledger is free. Each ledger operation uses one monotonic budget for its directory and SQLite lock waits rather than giving each wait a new allowance. A timeout returns an actionable error without changing ledger data. This is a lock-wait budget, not an HTTP timeout or a limit on the whole operation's copying or computation.

In Python, `exacly.executions.Registration` describes the explicit registration inputs; `register(ledger_path, request, lock_timeout=30)` writes the binding, `get(ledger_path, binding_id, lock_timeout=30)` reads it, and `as_text(record)` renders the result. `exacly.executions.SCHEMA` defines each immutable registration record's `0.1.0` format; show returns that record together with its current active status and watermark.

## Maintain an execution ledger

Maintenance is explicit and offline. Start with a read-only diagnosis:

```sh
exacly executions inspect --ledger "$EXACLY_LEDGER_FILE" --lock-timeout 5 --json
```

Inspection checks the source without opening SQLite when residue or an unknown format makes that unsafe. A clean supported ledger exits 0. A ledger requiring recovery, an unsupported format or corruption exits 1; invalid inputs and operational errors exit 2. Normal registration, show and bound previews continue to refuse sidecars and never recover implicitly.

Back up a clean, valid ledger into a new directory:

```sh
exacly executions backup --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_LEDGER_BACKUP_DIR" --json
```

Backup uses SQLite's consistent backup operation and validates the copied store. It writes `ledger.sqlite3` and a manifest in the destination, leaving the source database unchanged. The copy retains the same binding identities, history, watermarks and approval limits; a backup does not grant new authority.

Inspection, backup and recovery support operational ledger schemas 1, 2 and 4 without changing their version. Version 3 remains audit-only. Version-2 validation re-derives all retained completion proof. Version-4 maintenance checks complete integrity baselines and freshly audits all retained semantics, including when a terminal verdict could be reused by an ordinary operational read. Backups and recovered copies preserve the required index coherently with proof and never refresh the retained profile implicitly.

Completion operations reuse an operation-scoped validated view only while its protected database snapshot is unchanged. Those snapshots expire with their connection and locks; unexpected changes stop the operation. Versions 1 and 2 retain full fresh semantic validation. Version 4 checks all retained bytes and fresh global invariants on each new view, then reuses only matching terminal semantics under the installed profile. Prepared intents always receive fresh semantics. Missing optional verdicts and recognized stale profiles can require fresh read-only validation without writing the index; missing or mismatched required baselines are errors. Storage and byte-checking cost still grow with history.

The [ledger-history scaling design](../docs/ledger-history-scaling.md) describes the integrity, retention, replay and audit contract. The [index command](#create-and-audit-an-integrity-index-copy) retains its audit-only version-3 output. [Activation](#activate-an-operational-indexed-ledger) explicitly creates an operational version-4 copy; versions 1 and 2 keep their existing validation behavior.

For a supported interrupted SQLite DELETE-journal transaction, recover into a separate new directory:

```sh
exacly executions recover --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_LEDGER_RECOVERY_DIR" --json
```

Recovery first preserves raw copies of the source database and rollback journal under `raw/`. It recovers a working copy under `recovered/ledger.sqlite3`, then validates the complete schema, binding records, supersession chains and watermarks and writes a manifest with relative copy paths. Only a valid result is published atomically. Source database and journal files remain untouched. Unknown schemas, WAL/SHM, super-journals and partial initialization are diagnosed but not recovered: recovery exits 2 without publishing an output bundle. Failed copy validation also publishes no partial bundle; the original files remain available for diagnosis.

If final directory synchronization fails after publication, the complete copy is retained and the error asks you to inspect it before retrying. Reports distinguish the supported `store` format from the actual `observed_store` header, including unsupported headers.

Backup and recovery succeed with exit 0; errors exit 2. Their destination must be a new directory outside Git working trees, model folders and protected inputs, with no source overlap or overwrite. Created directories use POSIX mode `0700` and files `0600`. These operations retain the same operator-controlled runtime and same-user limitations as registration.

After reviewing a recovered copy, explicitly select its ledger path and cut over to one canonical ledger. Do not use the old and recovered copies as parallel active authorities. Manifests describe the operation and retained content; they do not authenticate global freshness or prove that a copy is the latest ledger.

In Python, `exacly.execution_maintenance.inspect(ledger_path)`, `backup(ledger_path, output_directory)`, `recover(ledger_path, output_directory)` and `upgrade(ledger_path, output_directory)` return maintenance reports; each accepts the optional `lock_timeout` keyword. `as_text(report)` renders the same report, and `SCHEMA` defines its `0.2.0` format. Maintenance does not modify model or state files, dispatch work or close tasks.

Version-1/version-2 maintenance retains report schema `0.2.0`. Version-4 maintenance uses `0.3.0` and reports installed and retained profiles, integrity coverage and fresh semantic audit counts. A stale recognized profile may be audited and copied without rewriting it; operational writes still require explicit activation refresh. Binding records and completion receipts retain their independent formats; the ledger's SQLite schema version is separate from report versions.

### Upgrade a ledger for completion

Completion supports schema version 2 or explicitly activated version 4. Start from a clean version-1 ledger by upgrading it into a new version-2 copy:

```sh
exacly executions upgrade --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_LEDGER_UPGRADE_DIR" --json
```

The bundle contains the exact source copy at `raw/ledger.sqlite3`, a validated version-2 copy at `upgraded/ledger.sqlite3`, and `manifest.json`. Upgrade preserves binding identities, supersession history and watermarks, and leaves the source unchanged. It succeeds with exit 0; invalid sources, unsupported formats and operational errors return 2. The destination has the same private-path, no-overwrite and atomic-publication rules as backup and recovery.

Review the bundle, then explicitly select `upgraded/ledger.sqlite3` as the canonical ledger for subsequent commands. Stop using the old path as an active authority. The manifest does not prove global freshness, and ordinary commands never migrate a ledger or perform this cutover automatically.

### Create and audit an integrity-index copy

`executions index` copies a clean version-2 ledger into a new private bundle and adds required integrity baselines and recorded terminal semantic verdicts. Version 3 remains **audit-only**. Registration, show, reconciliation, completion and ordinary maintenance do not support it. The index command performs no operational cutover or semantic reuse; use separate [activation](#activate-an-operational-indexed-ledger) when an operational version-4 copy is intended.

Preview the conversion without writing an output directory:

```sh
exacly executions index --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_HISTORY_COPY_DIR" --dry-run --json
```

Create the copy after reviewing that plan:

```sh
exacly executions index --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_HISTORY_COPY_DIR" --json
```

Both paths fully validate the selected version-2 source. Unresolved prepared intents block conversion, including dry-run; resolve them through the existing completion recovery procedure first. The destination must be a new private directory outside Git working trees, model folders and protected inputs, without source overlap or overwrite. The bundle contains the exact source at `raw/ledger.sqlite3`, the audit-only copy at `indexed/ledger.sqlite3`, and `manifest.json`. Historical records, proof bytes, identities, receipts and watermarks remain unchanged. Output files use mode `0600` and directories `0700`; a complete validated bundle is synchronized and published atomically. If final durability cannot be confirmed after publication, retain and inspect the complete output before retrying.

Process termination can leave a private staging directory or an empty reserved destination before publication. These are incomplete artifacts without a success manifest. Inspect them and select a new destination for a retry; the command never removes or overwrites existing output.

Audit the version-3 copy read-only:

```sh
exacly executions audit \
  --ledger "$EXACLY_HISTORY_COPY_DIR/indexed/ledger.sqlite3" --json
```

Audit checks the exact retained row coverage and typed input baselines, relationships and installed validation profile, then freshly re-derives every retained semantic transition. A stored positive verdict never replaces those checks. Missing or altered baselines, changed retained bytes and inconsistent index associations fail without repair or resealing. An unsupported profile is reported as unsupported. Audit never fetches GitHub evidence, writes state, changes pending work or modifies the copy. Existing journal or WAL sidecars are diagnosed without implicit recovery; version-3 backup, recovery and operational writes remain future work.

The audit-only format uses profile family `exacly-ledger-audit-v1` and transcript codec `typed-cells-v1`. The required integrity baseline covers the complete retained-row roster. Each terminal verdict separately binds its exact binding row, intent row and receipt row or explicit receipt absence, under the independently fingerprinted installed code, schemas, parser dependencies and runtime. Version-3 audit requires a matching installation; unsupported profile changes remain explicit failures. Version 3 never becomes operational through an ordinary read or write.

Index and version-3 audit reports retain schema version `0.1.0`, independent of ordinary maintenance reports. Dry-run reports `ready` and conversion reports `indexed`, both with exit 0. Audit exits 0 for `passed`, 1 for `corrupt`, `unsupported` or `recovery_required`, and 2 for invalid inputs or operational errors. Both commands accept `--lock-timeout SECONDS`, default 30; the finite nonnegative value bounds lock waiting rather than copying or whole-audit computation. These audit-only reports identify the checked scope and profile and declare `eligible_operational: false` and `automatic_cutover: false`.

In Python, `exacly.history_maintenance.convert(ledger_path, output_directory, dry_run=False, lock_timeout=30)` and `audit(ledger_path, lock_timeout=30)` return these reports; `as_text(report)` renders them, and `SCHEMA` defines their format. Digests and recorded verdicts do not authenticate who produced historical evidence, prove that a coherent copy is globally latest or protect against a same-user rewrite of proof and index. All historical bytes remain retained; this foundation adds no archival, expiry or garbage collection.

### Activate an operational indexed ledger

`executions activate` explicitly creates a version-4 operational copy from a clean supported version-2, version-3 or version-4 ledger. It fully validates the source before creating current-profile baselines and terminal verdicts in a new private bundle. A required integrity mismatch is a hard error; activation never reseals changed history as the original retained bytes. The source remains unchanged. Initial conversion from version 2 or 3 rejects unresolved prepared completions.

Activation can refresh a well-formed stale version-3 or version-4 profile from a known family and codec after baseline checks and fresh semantics. Unknown families or codecs are unsupported. The existing version-3 audit command keeps its strict matching-profile behavior; activation does not silently relax that audit or change the source copy.

Preview the copy and profile selection without writing output:

```sh
exacly executions activate --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_OPERATIONAL_COPY_DIR" --dry-run --json
```

Create the operational copy:

```sh
exacly executions activate --ledger "$EXACLY_LEDGER_FILE" \
  --out "$EXACLY_OPERATIONAL_COPY_DIR" --json
```

The new destination contains `raw/ledger.sqlite3`, the exact source, `operational/ledger.sqlite3`, the validated version-4 copy, and `manifest.json`. It has the same private-path, no-overwrite, source-preservation and durable atomic-publication rules as index conversion. Review the copy, then explicitly select `operational/ledger.sqlite3` as the canonical ledger for subsequent commands. Stop using the old path as a parallel active authority. Activation never changes authoritative state or performs this cutover automatically. A version-1 source must first use the existing version-2 upgrade.

Version 4 supports registration, binding show, bound reconciliation, completion, offline historical replay and explicit completion recovery. Each new validated view checks complete historical byte coverage and global binding, phase, receipt and watermark invariants. Matching terminal semantics may be reused; prepared proof and new evidence evaluations remain fresh. Registration, preparation, abort and final receipt transactions maintain the required index with their actual ledger changes. Cached historical results never substitute for live evidence admission or current-state authorization.

Version-4 binding show includes validation diagnostics for the installed and retained profile, checked integrity scope and fresh or reused semantic work. The immutable binding record and historical completion receipt keep their existing formats; version-1/version-2 show wrappers are unchanged. The `exacly.execution_index.check()` result also exposes these validation facts through `.facts`. An index-assisted result is distinct from a fresh full audit.

Version-4 completion, show and recovery wrappers use report schema `0.2.0`, with `history_validation` identifying the named protected view at stage entry and its profile, byte coverage and fresh/reused counts. These counts describe that view rather than totals for the whole command. Version-2 wrappers and all immutable historical receipts retain `0.1.0`. Bound reconciliation includes the corresponding facts in `binding.validation`.

A missing optional semantic verdict falls back to fresh validation in memory. A recognized stale version-4 profile with valid baselines also permits read-only fresh validation, but operational writes stop until a new explicit activation copy refreshes the profile. Normal commands never silently upgrade a profile or rewrite unrelated verdicts. Missing or corrupt required baselines remain errors during reads, activation, backup, recovery and audit.

An existing version-4 ledger may be explicitly copy-refreshed while it contains prepared work. Activation validates all baselines and fresh semantics, preserves the exact intent, proof, state destination and receipt absence, and does not fetch evidence or publish state. After reviewing and selecting the refreshed copy as canonical, run normal completion recovery against the actual current state. Exact-before abort, exact-after finalization and third-state conflict retain their existing meanings and locks; activation itself never recovers the pending publication.

Audit an operational ledger with the same explicit command:

```sh
exacly executions audit \
  --ledger "$EXACLY_OPERATIONAL_COPY_DIR/operational/ledger.sqlite3" --json
```

Version-4 audit always freshly validates every retained semantic transition, regardless of reusable verdicts. Ordinary version-4 inspection, backup and copy-only hot-journal recovery also use a full fresh audit and preserve the profile and index coherently. Activation and version-4 audit reports use history schema `0.2.0`; activation reports `ready` for dry-run or `activated` for a published copy. Their lock-timeout and exit-status conventions match the history commands above. The checked scope distinguishes byte integrity and reused semantics from a fresh full audit. No history is archived or deleted, and whole-history byte work still grows with retained storage.

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

Bound previews also accept `--lock-timeout SECONDS`, defaulting to 30 seconds; the option requires the binding inputs. Each ledger read uses its own deadline shared by that read's directory and SQLite waits. The reads before and after live admission are separate operations. This does not bound the whole preview or its HTTP requests or permit implicit store recovery.

A successfully checked bound preview records `claims.registered_binding_verified: true`. An unbound preview remains supported and records that claim as false, without implying that any pending-task binding was registered or checked. Neither mode removes pending work or changes the ledger or its watermark.

Bound previews check the binding before fetching evidence and again immediately before reporting `ready`. The binding's `checked_at` records the last read. These are read snapshots; the separate completion command checks current state and authorization preconditions under its commit-time locks.

The report is advisory and unsigned. It does not prove that an executor produced the selected output revision, authorize task closure, dispatch work or maintain the execution ledger. Evidence origin, applicability and the admission limits on test honesty and acceptance coverage remain visible; an executor completion claim alone cannot make an action ready.

In Python, `exacly.reconciliation.preview(source, plan_doc, action_id, readiness_policy, trust_policy, output_target, run_id, attempt, date)` returns the same read-only preview using a live GitHub source and an `exacly.applicability.Target`. Optional keyword arguments `ledger`, `binding_id` and `state_doc` request the bound check and must be supplied together; `lock_timeout` controls its ledger lock waits.

`exacly.reconciliation.SCHEMA` defines the `0.2.0` JSON report. Existing unbound command and Python calls remain supported, but JSON consumers of report version `0.1.0` must handle the new version and required fields: `binding` is null or an object, and `claims.registered_binding_verified` is a boolean. Saved preview reports remain advisory artifacts rather than ledger registrations.

The [live rehearsal report](../docs/verification-reconciliation-rehearsal.md) records an actual producer artifact, admission and reconciliation preview, including the transport coverage and environment limitation.

## Complete registered verification work

`exacly complete github-actions` explicitly authorizes completion of one registered local pending verification item. It requires an operational version-2 or version-4 ledger, an active binding and the same reviewed plan, policies and output target used for that binding. Registration and a `ready` preview do not authorize mutation. Invoking this command with the selected inputs grants that local completion authority; it requires no additional confirmation prompt or authorization flag.

```sh
exacly complete github-actions --plan reports/plan.json \
  --action "$EXACLY_ACTION_ID" --policy policies/verification.yaml \
  --trust-policy trusted/github-verification.json --target targets/output.json \
  --run "$EXACLY_RUN_ID" --attempt "$EXACLY_RUN_ATTEMPT" --date 2026-10-28 \
  --ledger "$EXACLY_LEDGER_FILE" --binding "$EXACLY_BINDING_ID" \
  --state observations/current.json --json
```

Each new evaluation performs fresh live admission for the exact GitHub run attempt and output implementation, using `GH_TOKEN` or `GITHUB_TOKEN` as for admission. It validates the approved model contract and actual current state, then rechecks the active binding, complete pending entry, lineage, serial, digest and registered watermark under ledger and state locks. Source-verification target restrictions remain the same as the preview: the approved model and context stay fixed while the resulting implementation may differ from the planning input.

The command merges only the admitted observer's results using the source and binding replacement rules. It keeps unrelated assessments, evidence, relationships and pending entries, and evaluates the selected goal against this merged state. Retained applicable failures or missing required observations keep the task pending. Whole-policy readiness is reported separately; one completed action does not establish readiness of the whole product or release.

When the goal is satisfied, completion removes only the exact registered pending entry, preserves namespace, context and lineage, and advances the serial once. Before publication, it durably records the admitted proof, request identity and prepared intent with the exact before-state identity and candidate after-state bytes. It publishes those exact bytes through the shared conditional atomic writer, then finalizes an immutable receipt while retaining the locks. SQLite and JSON remain separate resources joined by a recoverable protocol, rather than one atomic transaction. A failure after replacement retains the complete published state and prepared intent for explicit recovery.

Version-2 ledger writes use SQLite DELETE journaling with `synchronous=EXTRA`, including synchronization of journal removal; state publication separately synchronizes the state file and its directory.

The text and JSON reports describe the same result. `completed` exits 0; `pending`, `stale` and `conflict` exit 1; invalid inputs and operational errors exit 2. Optional `--out FILE` saves the report outside protected inputs and model folders. An output error does not undo a completion already recorded in the ledger. `--lock-timeout SECONDS` defaults to 30 and accepts finite nonnegative values, including zero for an immediate attempt; it limits lock waiting rather than live HTTP or whole-command time.

Repeat the exact completed request, including its original evaluation date, run and attempt, to obtain the original durable receipt without HTTP, another task removal or a serial increment. A different request cannot reuse that completed binding or pending identity. A replayed receipt records historical completion; it does not assert that the current state file remains unchanged or that evidence is still current.

Inspect a retained completion offline:

```sh
exacly completions show --ledger "$EXACLY_LEDGER_FILE" \
  --completion "$EXACLY_COMPLETION_ID" --json
```

To resolve a prepared completion after interruption, use the same state destination:

```sh
exacly completions recover --ledger "$EXACLY_LEDGER_FILE" \
  --completion "$EXACLY_COMPLETION_ID" --state observations/current.json --json
```

This recovery operates on the selected completion intent, separately from `executions recover`, which copies and repairs an interrupted SQLite transaction. Completion recovery verifies retained proof and binding history without HTTP. If the exact before-state remains, it marks the intent `aborted`; another completion attempt must obtain fresh admission. If the exact recorded after-state is present, it synchronizes the state and directory and finalizes the receipt once. Any third state reports `conflict` and remains untouched. Recovery never publishes a previously computed candidate blindly. Unresolved prepared intents continue to block conflicting registration changes.

Show exits 0 for a valid retained phase, including `prepared` or `aborted`. Recovery exits 0 for `completed` or `aborted`, 1 for an unresolved result, and 2 for invalid inputs or operational errors. Both commands accept `--lock-timeout`, render text or JSON, retain their records in the ledger and need no GitHub token. Reports omit raw retained proof bytes.

In Python, `exacly.completion.complete(source, plan_doc, action_id, readiness_policy, trust_policy, output_target, run_id, attempt, date, *, ledger, binding_id, state_path, lock_timeout=30)` returns the same report. `show(ledger, completion_id, lock_timeout=30)` inspects it; `recover(ledger, completion_id, state_path=..., lock_timeout=30)` resolves a prepared intent; `as_text(report)` renders it for people.

Completion receipts remain unsigned and do not prove test honesty, acceptance coverage or that an executor produced the named output. These commands complete local verification work without closing external tickets, running candidate code, dispatching agents or granting release authority. The ledger must remain in the operator-controlled runtime; its file permissions do not isolate code running as the same operating-system user.

## Isolate a candidate command

**Unreleased functionality on main.**

`exacly isolate` runs one explicit argument vector through Linux Bubblewrap using a reviewed detached root filesystem and immutable input snapshots. Before starting candidate code, a trusted bootstrap verifies the actual launch's namespace separation, capability and mount protections and mandatory syscall filter. The runtime provides private writable temporary filesystems and no host network, candidate-writable host mounts, operator storage, inherited credentials or control interfaces; child processes retain the boundary.

```sh
exacly isolation inspect --rootfs /srv/exacly/runtimes/python-rootfs --json
mkdir -p runs
exacly isolate --runtime-policy trusted/runtime.json \
  --inputs approved-inputs.json --out runs/candidate --timeout 30 --json -- \
  /usr/bin/python3 -I -S /inputs/work.py
```

Inspection identifies content for independent review; its digest does not approve a runtime. `--inputs` is an optional JSON manifest of explicit relative paths and UTF-8 contents, with no implicit source traversal. The Python API accepts binary snapshots. A fresh private output bundle retains the report, bounded logs and safely collected regular artifacts after the namespace and descendants have exited. Existing destinations are never overwritten, and unavailable or invalid isolation stops before candidate execution.

Exit 0 means the command completed successfully; exit 1 records a candidate failure, timeout or output limit; exit 2 reports invalid input, setup or operational failure. Candidate status, logs and artifacts remain untrusted and cannot establish verification or completion. This launcher supplies one execution boundary; durable dispatch and executor attempt budgets retain their own contracts.

See the [candidate isolation guide](../docs/candidate-isolation.md) for the exact runtime policy and snapshot formats, supported platform, limits, Python API, real integration checks and kernel/runtime trust assumptions.
