# Package release notes

## Unreleased

- `exacly composition validate` checks a required-member definition and its resolved approved policies and model inputs without GitHub access. `exacly compose github-actions` pins that definition's digest, validates every exact member selection before source reads, then uses native admission and readiness evaluation independently for each member. Scoped repository/model constraints preserve original namespaces; all required gates must pass. New private output retains native evidence and an atomic aggregate that distinguishes incomplete diagnostics from final success. The CLI streams saved member payloads instead of accumulating archive and report bytes; Python callers retain full native results by default and can opt into synchronous streaming retention. Every rerun performs fresh admission; producer policies, platform and external release inventories remain consumer responsibilities. Supported pure admission-request, approved-model and readiness-policy validation APIs share the native checks. These commands are unreleased functionality on main.
- `exacly gate --junit FILE --junit-id LABEL` optionally exports the native readiness evaluation as JUnit XML alongside text or JSON. Versioned testcase selectors include an explicit stable logical-target label; mutable policy versions and target revisions remain metadata. Unknown prerequisites remain blocking failures, distinct from known unmet prerequisites. Invalid input or unsafe/failed report output returns 2; reporting grants no evidence-origin, fresh-test, delivery or state-writing authority.
- The executor coordinator consumes public attempt-storage, invocation-ownership and worker-lifecycle protocols through an explicit trusted Python backend bundle. Default local adapters preserve the existing CLI, policies and journal formats. Callback identities and event order are checked before durable append so malformed adapter callbacks cannot block inspection or recovery. Common conformance and substitution cases cover reservation, budgets, ownership, cancellation and recovery; protocol `0.1.0` accepts one local-machine domain and requires a separately verified contract before distributed coordination or another provider is supported.
- `exacly execute local` previews or separately authorizes one bounded local snapshot command for an active registered verification task. Durable attempts and fixed shared concurrency live in an explicitly initialized private companion journal without changing existing binding/completion ledger formats. The supervisor uses the verified Bubblewrap launcher, enforces a full-worker wall-clock budget, retains host-derived content build identities and supports model-independent inspection, cancellation and recovery through `executors` commands. Explicit retries remain within the retained budget; no repository effects, evidence admission, pending-work removal or completion authority are granted.
- `exacly dispatches resolve` previews stale partial-registration history and exact current state, pending and binding facts, then requires separate reviewed authorization to append a terminal journal-only `resolved` receipt. Resolution preserves current work and remote tasks, leaves registration unfinalized and keeps the prohibition on another create for that dispatch identity. `dispatches recover-journal --authorize-recovery` repairs a protected interrupted outbox transaction with a retained backup, without reusing a stale model or changing state or bindings.
- `exacly dispatch github-issues` previews or explicitly creates one reviewed verification task with a separate private dispatch journal, stable identity and bounded recovery. A possibly sent create is never blindly repeated; confirmed work is associated with pending verification and an execution binding through recoverable conditional state publication. `dispatches init`, `show`, `recover` and `supersede` retain controlled history without changing execution-ledger formats or granting evidence, completion or executor authority.
- The optional local Bubblewrap backend denies `perf_event_open` for candidate processes and descendants, preventing CPU-wide performance monitoring from crossing the runtime boundary on permissive hosts. A harmless denial probe must pass before candidate execution; the protection does not depend on or change the host's `perf_event_paranoid` setting.
- `exacly isolate` provides the optional local `linux-bubblewrap-v1` backend for the runtime-neutral candidate-isolation contract. It runs one explicitly authorized command with a reviewed detached root filesystem, immutable input snapshots and a verified namespace gate before execution. The launcher clears inherited authority, denies host network and operator interfaces, applies a fixed syscall filter protecting kernel keyrings and socket-family limits, cleans up descendants and safely collects bounded untrusted logs and regular artifacts into a new private bundle. `exacly isolation inspect` identifies runtime content for independent review; it grants no execution or completion authority. Core model and completion commands do not require the backend. REQ-025 is verified through real Linux integration cases, while durable dispatch and executor attempt supervision retain their separate contracts.
- `mise run check-core` offers a development subset without the local backend's test modules. Its separate `reports/core-junit.xml` report is not used for state refresh or full verification; required Linux CI still runs the complete isolation suite and verification gate.


## 0.6.0 — 2026-10-29

This stable 0.x release adds operator-controlled verification work: plan,
package and admit evidence, register an existing pending task, and explicitly
complete it from applicable observations. Candidate isolation, task dispatch
and executor supervision (REQ-025–027) remain proposed.

### Changes since 0.5.0

- **Plan and reconcile verification.** `exacly plan` proposes investigation,
  evidence collection or configuration work for explicit verification goals.
  `reconcile github-actions --dry-run` evaluates a saved action through live
  evidence admission, optionally checking its registered pending-task binding.
  Planning and previews preserve state; a ready preview grants no mutation
  authority and does not establish whole-product readiness.
- **Install producer tools.** `exacly evidence package --report FILE --out
  NEW_DIR` prepares the manifest/JUnit bundle from the actual GitHub push
  checkout, preserving report bytes and failure/skipped observations.
  `exacly evidence propose-policy github-actions` drafts a candidate from
  explicit repository, revision, producer, observer and model inputs. Candidates
  require independent review and controlled storage before use.
- **Admit live evidence.** Admission checks the exact approved repository,
  branch, workflow, observer/model content, run attempt and artifact before
  deriving verification state. Branch protection remains required by default;
  an independently reviewed `branch_protection: not_required` policy records
  `claims.branch_protection_verified: false`. The check establishes neither
  historical protection nor the strength of branch rules. Admitted and rejected
  bundles use `0700` directories and retain files as `0600`, independent of umask.
- **Register and complete local work.** Registration creates completion-ready
  v2 ledgers. Completion explicitly authorizes only the selected registered
  task, preserves unrelated work, advances state once and retains recoverable
  proof and an immutable receipt. Offline inspection, exact replay and explicit
  recovery preserve history. Separate maintenance commands provide diagnosis,
  backup, copy-only recovery and upgrade; v4 activation adds profile-bound
  semantic reuse while still checking all retained bytes and global invariants.
- **Publish state conditionally.** `refresh` and `observe manual-qa` use a
  shared conditional atomic writer on supported local POSIX filesystems.
  Conflicting destination changes require fresh observation. The new
  `refresh --model-revision` separates model and implementation identities;
  omission retains single-revision behavior. State and ledger operations have
  bounded lock waits without imposing a whole-command or HTTP timeout.

Receipts remain unsigned and do not establish test honesty, acceptance
coverage, executor-to-output provenance or globally latest state. Completion
does not run candidate code, dispatch agents, close external tickets or grant
release authority. Same-user file permissions provide no candidate isolation.

### Formats and observed trials

Package, specification, SQLite and JSON report versions are independent.

| Artifact or operation | Version in 0.6.0 |
| --- | --- |
| Readable specification/config/state | `0.1` product maps and `0.2` config/state; unchanged |
| Producer evidence manifest and verification plan | `0.1.0` |
| GitHub admission receipt | `0.2.0`, including explicit branch-protection claims |
| Reconciliation preview | `0.2.0`, with required `binding` and `claims.registered_binding_verified` |
| Immutable binding record and completion receipt | `0.1.0` |
| Completion/show/recovery wrapper | `0.1.0` for v2; `0.2.0` for v4, with `history_validation` |
| Ordinary ledger maintenance | `0.2.0` for v1/v2; `0.3.0` for v4 |
| Audit-only v3 conversion/audit; operational v4 activation/audit | `0.1.0`; `0.2.0`, respectively |

An operator reported successful consumer trials. The latest used
[AgentiScale push run 40958071220](https://github.com/whiteredhouse/agentiscale/actions/runs/40958071220),
attempt 1 at `9bbdce9a7c9a298c24dd1add693345fbe4c843a6`. It covered fresh
v2 registration, admission, bound reconciliation and completion without upgrade
or activation; admission permissions under umasks `022` and `000`; offline
same-receipt replay with zero socket calls; and v1/v2/v4 ledger compatibility.
Branch protection, test honesty and coverage remained unverified in the trial.
The corresponding [Go run 40958071223](https://github.com/whiteredhouse/agentiscale/actions/runs/40958071223),
including its committed-state refresh job, also completed successfully.

### Upgrading from 0.5.0

Existing model and state documents remain readable; the specification version
does not change. Existing validation, observation and view workflows can keep
their structure. Reobserve when current evidence is needed instead of editing
or relabeling historical observations. Keep policies, targets, plans and output
artifacts outside model folders.

The new verification workflow is opt-in and needs reviewed readiness and
producer-trust policies in the operator's controlled runtime. It does not add
planning, admission or completion to an existing integration automatically.
Review the new report schemas above before consuming their JSON.

`refresh --model-revision` labels the supplied model documents and results; it
does not fetch revisions or authenticate them. Distribution refresh requires
matching model and implementation revisions. Retained assessment subjects keep
their original model and implementation identities when the enclosing state
advances.

Reconciliation report consumers must handle version `0.2.0` and its required
`binding` and `claims.registered_binding_verified` fields. Unbound previews use
`binding: null` and a false claim. Immutable binding records and completion
receipts retain their existing formats.

### Existing ledgers and admission receipts

Existing operational v1, v2 and v4 ledgers keep their formats; v3 remains
audit-only. A v1 ledger requires the explicit copy-only `executions upgrade`
before completion. Fresh registration now creates v2, so remove an
unconditional post-registration upgrade from new-ledger setup. Inspect an
existing ledger's format with `exacly executions inspect --ledger FILE`.

| Starting ledger | Explicit command | Output to review and select |
| --- | --- | --- |
| Version 1 | `exacly executions upgrade --ledger SOURCE --out NEW_DIR` | `NEW_DIR/upgraded/ledger.sqlite3`, version 2 |
| Clean version 2, audit copy only | `exacly executions index --ledger SOURCE --out NEW_DIR` | `NEW_DIR/indexed/ledger.sqlite3`, audit-only version 3; retain the source as operational authority |
| Supported version 2, 3 or 4 | `exacly executions activate --ledger SOURCE --out NEW_DIR` | `NEW_DIR/operational/ledger.sqlite3`, version 4 |

Copies preserve the source, include exact raw backups and a manifest, use private
paths and refuse overlap or overwrite. Review the output before explicitly
selecting one canonical operational ledger; old and new copies must not remain
parallel active authorities. Initial v2/v3 activation rejects unresolved
prepared completions; explicit v4 refresh can preserve prepared work for normal
recovery against the actual current state.

Admission-receipt consumers must handle `0.2.0`; retained `0.1.0` proof
remains readable with its original protection-required meaning. Changing the
branch-protection policy requires independent review and a matching registered
contract. Use the installed packager instead of the removed repository-only CI
helper; preserve its report/artifact layout and regenerate a candidate policy
when workflow, observer or model pins change. Review that candidate independently
before admitting evidence under the changed inputs.

Older validation inputs can make a recognized v4 profile `known_stale`. Reads
and offline replay freshly validate retained semantics after required integrity
checks; further writes require explicit copy-only profile refresh:

```sh
exacly executions activate --ledger SOURCE --out NEW_DIR
```

Review and explicitly select `NEW_DIR/operational/ledger.sqlite3` as canonical.
Activation preserves the source and never performs automatic cutover, completion
recovery or resealing of changed history. Versions 1 and 2 need no profile
refresh; v3 audit requires a matching profile.

### Validation profiles and historical replay

A v4 profile remains current while code, schemas, installed dependency artifacts
and runtime remain identical. Exacly's version metadata and README are not
profile inputs. Recreating or changing an environment can change fingerprinted
artifacts such as dependency launcher paths, even when package versions match.
Inspect the selected ledger's retained and installed
profiles before writes. Activation is needed when actual validation inputs
change and the retained profile becomes stale, not merely for the `0.6.0` label.
Normal commands never activate or migrate an existing ledger implicitly.

Reuse the original saved plan and reviewed inputs for registered completion and
historical replay. Newly generated plans name tool version `0.6.0` and can have
different plan digests; they are not substitutes for the registered original.
Exact replay also requires the original normalized absolute `--state`
destination. A copied state file at another path changes the request even if
its bytes match; ledger copies and activation preserve the recorded destination.
Historical replay does not read or rewrite current state contents.
`exacly completions show` inspects retained history without a state path.

Updating a package pin in a producer workflow changes its reviewed bytes.
Regenerate and independently review the matching producer-policy candidate when
workflow, observer or model inputs change. Installing 0.6.0 does not approve a
policy or change permissions on existing admission bundles.

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
