# Package release notes

## Unreleased

- `exacly isolate` runs one explicitly authorized candidate command through Linux Bubblewrap with a reviewed detached root filesystem, immutable input snapshots and a verified namespace gate before execution. The launcher clears inherited authority, denies host network and operator interfaces, applies a fixed syscall filter protecting kernel keyrings and socket-family limits, cleans up descendants and safely collects bounded untrusted logs and regular artifacts into a new private bundle. `exacly isolation inspect` identifies runtime content for independent review; it grants no execution or completion authority. REQ-025 is verified through real Linux integration cases, while durable dispatch and executor attempt supervision retain their separate contracts.

## 0.6.0rc1 — 2026-10-29

This release candidate adds an operator-controlled verification workflow: plan
work, admit GitHub evidence, register an existing task, and explicitly complete
that task when applicable observations satisfy its goal. It remains a 0.x
preview. Candidate isolation, task dispatch and bounded executors
(REQ-025–027) are proposed work and are not included.

### New commands and capabilities

- **Plan verification work.** `exacly plan --policy FILE --target FILE --date
  YYYY-MM-DD` proposes investigation, evidence collection or observation
  configuration for unsatisfied Requirement verification prerequisites. Text,
  `--json` and saved `--out` artifacts share stable action identities, input
  fingerprints and observable completion conditions. Planning is read-only and
  advisory; use `gate` for a readiness exit status.
- **Admit live GitHub evidence.** `exacly admit github-actions` checks an exact
  run attempt and verification artifact against an independently approved
  producer policy, then derives fresh state from the admitted JUnit report and
  conforming approved model. Policies explicitly identify the protected branch,
  workflow/job/steps, artifact prefix and observer selectors, including selected
  absence. Workflow and observer fingerprints, artifact/report digests and the
  selected target are checked. The explicit evaluation date governs model
  conformance and expiry and excludes jobs completed after that UTC date.
  Decisions retain a receipt; admitted bundles also retain the archive, report
  and derived state.
- **Preview reconciliation.** `exacly reconcile github-actions --dry-run`
  checks one saved plan action against fresh live admission, reporting `ready`,
  `pending` or `stale` and selected-goal verification separately from whole-policy
  readiness. It requires consistent plan/action identity and unchanged policies,
  approved model, criteria and bindings. It accepts no saved admission state as
  live evidence and preserves inputs and pending work. Optional all-or-none
  `--ledger`, `--binding` and `--state` inputs additionally check an existing
  registered pending-task binding before and after admission.
- **Register and maintain work.** `exacly executions register` records an
  explicit binding between an existing pending verification task, a reviewed
  plan action, policies and an output target in a private operator-selected
  SQLite ledger. Exact retries are idempotent; explicit supersession preserves
  immutable history, and per-pending state watermarks reject rollback and
  same-serial divergence. `executions show`, `inspect`, `backup` and `recover`
  provide bounded reads, diagnosis and validated source-preserving copies.
- **Complete existing verification work.** `exacly complete github-actions`
  explicitly authorizes completion of one registered local task from fresh
  admitted evidence evaluated against merged current state. It preserves
  unrelated observations and pending work, retains applicable failures, removes
  only the exact satisfied pending item, and advances state once. A durable
  intent and immutable receipt make the SQLite/JSON publication protocol
  recoverable; it is not one atomic transaction. Exact completed requests replay
  their historical receipt without HTTP or another state write. `completions
  show` inspects retained work, and offline `completions recover` aborts an
  exact-before intent, finalizes its exact after-state, or reports a divergent
  state conflict without overwrite.
- **Audit or activate retained ledger history.** `executions index` creates an
  audit-only version-3 copy with exact-byte baselines and terminal semantic
  verdicts; `executions audit` freshly validates it. `executions activate`
  separately creates an operational version-4 copy with installed validator
  profiles and reusable matching terminal semantics. See the format and upgrade
  tables below; ordinary commands never migrate, recover or activate implicitly.

### Changed state publication and identity

- `refresh --model-revision MODEL --revision IMPLEMENTATION` records distinct
  model and implementation identities. The enclosing state follows the model;
  assessment context, subject and evidence applicability follow the
  implementation. Omitting `--model-revision` keeps the single-revision behavior
  and existing observations retain their original identities. The option labels
  supplied documents/results; it does not fetch revisions or authenticate them.
  Distribution refresh requires matching model and implementation revisions.
  GitHub's automatic source link follows the implementation; explicit
  `--source-url` is unchanged.
- `refresh` and `observe manual-qa` now publish through a shared conditional
  atomic writer on supported local POSIX filesystems. They refuse publication if
  the destination’s bytes or presence changed since observation began, preserve
  lineage and advance the serial once. Conflicts require fresh observation.
  Staged-file and directory synchronization prevent partial JSON publication; errors after replacement
  retain the new document and report uncertain durability.
- State writers, ledger commands and bound previews accept finite nonnegative
  `--lock-timeout SECONDS` (default 30; zero permits an immediate attempt).
  Each ledger operation shares its budget across directory and SQLite waits;
  preview reads before and after admission have separate budgets. This does not
  bound HTTP or whole-command computation/copy time. Publication errors keep
  their operation-specific diagnosis after locks are released.
- Completion operations reuse full retained-history validation within one
  unchanged protected view, invalidating it after mutation or unexpected
  changes. Every new view validates history again. Version-4 normal validation
  checks every retained byte and fresh global invariants while reusing only
  matching terminal semantics; prepared work and new evidence remain fresh.
  Version-4 writes maintain baselines and affected verdicts in the same SQLite
  transaction as their actual changes. Inspection, backup, recovery and explicit
  audit freshly validate semantics. History remains retained and whole-history
  byte work and storage continue growing; this adds no archival or deletion.
  Controlled workload measurements and their limits are in the
  [benchmark guide](benchmarks/README.md) and
  [ledger-history design](docs/ledger-history-scaling.md).

### Upgrading from 0.5.0 or a main-branch preview

Existing 0.1 product maps and 0.2 config/state documents remain readable. This
package release does not change the specification version. Keep target, policy,
plan and output artifacts outside model folders. Older serialized observations
are history; refresh them when current evidence is required rather than editing
or relabeling them.

The new workflow uses reviewed readiness and producer-trust policies in the
operator's controlled runtime. A plan digest checks consistency, not approval.
Admission and completion receipts are unsigned and do not establish test
honesty, acceptance coverage, executor-to-output provenance or globally latest
state. Local completion does not close external tickets, run candidate code,
dispatch agents or authorize release. Same-user file permissions are not a
candidate-code isolation boundary.

#### JSON report compatibility

The package, SQLite ledger and JSON report versions are independent. Consumers
of main-branch preview artifacts must select the corresponding report schema;
particularly, reconciliation `0.1.0` consumers must handle the new `0.2.0`
required fields. Original immutable binding records and completion receipts keep
their formats.

| Artifact or operation | JSON `schema_version` in this release |
| --- | --- |
| Verification plans and GitHub admission receipts | `0.1.0` |
| Reconciliation previews, bound or unbound | `0.2.0`; required `binding` and `claims.registered_binding_verified`. Unbound calls keep `binding: null` and the claim false. |
| Immutable execution binding records and completion receipts | `0.1.0` |
| Completion/show/recovery wrappers using a version-2 ledger | `0.1.0` |
| Completion/show/recovery wrappers using a version-4 ledger | `0.2.0`, adding `history_validation` for the named protected view, not command-wide totals |
| Ordinary version-1/version-2 ledger maintenance | `0.2.0`, including upgrade operation/status and the supported store versions |
| Ordinary version-4 ledger maintenance | `0.3.0`, including profile, integrity coverage and fresh audit diagnostics |
| Audit-only version-3 index conversion and audit | `0.1.0` |
| Version-4 activation and audit | `0.2.0` |

#### Ledger upgrade paths

Registration still creates version-1 ledgers by default. Existing operational
ledgers retain their format. Completion supports version 2 or an explicitly
activated version 4; version 3 is never an operational ledger.

| Starting ledger | Explicit path | Output to select after review |
| --- | --- | --- |
| Version 1 | `exacly executions upgrade --ledger SOURCE --out NEW_DIR` | `NEW_DIR/upgraded/ledger.sqlite3`, version 2 |
| Clean version 2, audit copy only | `exacly executions index --ledger SOURCE --out NEW_DIR` | `NEW_DIR/indexed/ledger.sqlite3`, audit-only version 3; retain the source as operational authority |
| Supported version 2, 3 or 4 | `exacly executions activate --ledger SOURCE --out NEW_DIR` | `NEW_DIR/operational/ledger.sqlite3`, version 4 |

Copies preserve the source, include exact raw backups and a manifest, use private
paths and refuse overlap or overwrite. Review the output before explicitly
selecting one canonical operational ledger; do not use old and new copies as
parallel active authorities. A version-1 source must first upgrade to version 2.
Both index and activation support `--dry-run` without writing output. Initial
activation from version 2 or 3 rejects unresolved prepared completions.

A known stale version-4 profile with valid baselines permits fresh read-only
fallback; writes require an explicit activation copy to refresh the profile.
Missing optional terminal verdicts also fall back to fresh validation. Missing
or corrupt required baselines remain errors. Version-3 audit retains its strict
matching-profile requirement. An existing version-4 copy can be explicitly
profile-refreshed with prepared work intact; activation neither admits evidence
nor publishes state. After selecting the refreshed copy, use normal completion
recovery against the actual state. Supported interrupted SQLite DELETE-journal
recovery also produces a new validated copy and preserves the source database
and journal; it does not repair or cut over in place.

## 0.5.0

- Case-bound verification now requires every selected case to pass: mixed passing/skipped outcomes give `inconclusive`, retaining evidence and naming skipped cases alongside any missing patterns. Failures still give `failed`; all-skipped selections remain `not_checked`. This supersedes the permissive pass-plus-skip behavior accepted in #24. Verification readiness gates return `unknown` for these incomplete results and exit 1.
- Reobserve affected bindings after upgrading. Existing serialized `passed` assessments keep their original meaning until refreshed; do not rewrite historical state by hand. Broad case patterns that include platform-specific skips or pytest expected failures may now prevent a readiness gate from passing. Job-only bindings and duplicate-case-ID merging are unchanged.

## 0.4.0

- Verification and delivery are independent in 0.2 config/state validation. Passing tests can conform before publication or deployment, while each positive claim still needs its own applicable evidence and identified implementer. Historical 0.1 product-map semantics are unchanged.
- `exacly assess RECORD --target FILE` projects observations onto an explicit model revision, implementation revision or build composition, and selected scope. Reports explain exclusions and missing bindings.
- `exacly gate --policy FILE --target FILE` evaluates versioned prerequisites separately from model conformance. It exits 0 for satisfied, 1 for a known unmet or unknown prerequisite, and 2 for invalid input.
- Refresh and manual-QA observations retain structured subjects. Mixed revisions, obsolete bindings, ambiguous Git abbreviations and unsupported evidence cannot establish a passed scoped assessment.
- Exacly's own CI requires current verification for every active Requirement before publication. Wheel smoke checks run outside the checkout; a separate post-publication job installs the exact package version from PyPI and exercises the CLI.

### Upgrading

Existing 0.2 documents remain readable. Newly written state may include the optional `subject` field from the 2026-10-26 state-schema iteration; upgrade observers and validators together, since older validators may reject that field. Reobserve legacy state to establish implementation-specific applicability rather than adding identities by hand.

Keep target and policy files outside model folders. Start with a verification-only policy before release. Require delivery only when evaluating evidence of publication or deployment; source-test success does not establish production-environment verification. An omitted delivery environment selects the target's named context, while verification without environment makes a source-level claim.
