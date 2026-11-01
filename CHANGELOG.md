# Package release notes

## Unreleased

- Add a separately authorized local static-artifact deployment and rollback profile. Fixed delivery commands use exact authenticated published bytes, environment generation checks, durable owner intent and activation proof. A distinct trusted HTTP observer measures actual served bytes in a bounded window; current health stays separate from the immutable delivery effect and native readiness gate. Rollback needs its own grant, fresh failure and permitted prior artifact. This reference does not establish arbitrary application or production readiness.
- Add separately authorized exact-byte publication through a fixed local owning workflow. `delivery publish` uses the delivery journal and immutable original-goal limits, independent artifact-verification provenance and a private create-only version registry. The owner retains partial reservations and authenticated receipts; reconciliation reads actual retained bytes without rebuilding, overwriting or resending. This reference profile does not modify the existing package release workflow or establish deployment or health.
- Add a separately scoped delivery journal and fixed GitHub merge adapter with exact head/base leases, protected-contract checks, durable pre-effect intent, bounded native calls, original-goal accounting and discovery-only uncertain recovery. `delivery` commands preview, inspect, withdraw or explicitly apply independently pinned merge authority. Terminal replay stays offline; credential rotation needs a separate read-only recovery grant. The concrete profile and controlled verification do not establish authentic GitHub acceptance, publication authority or runtime health.
- Add advisory implementation planning under separately approved product intent. `exacly implementation propose` generates criterion-level tasks and reviewed dependency graphs; missing verification alone never becomes a code gap. Exact model/criteria/binding/policy/target and observation fingerprints, an independently pinned genesis contract and explicit predecessor/supersession checks preserve proposal identity. New private immutable bundles retain input snapshots; `implementation check --bundle` checks retained and current content without effects. Generated contract changes remain unapproved proposals, and no execution or later delivery authority is granted.
- Introduce closed product-delivery approval, intent and receipt contracts with separate stage identities and externally selected approval pins. The pure validators perform no provider invocation, journal initialization or budget reservation; concrete merge, publication and deployment adapters remain separately scoped work.

## 0.7.0rc1 — release candidate

This candidate adds separately authorized task dispatch, isolated local execution
and reviewed artifact-to-Git handoff, plus composition of trusted evidence and
JUnit readiness reporting. The latest stable release remains 0.6.0; select
`exacly==0.7.0rc1` explicitly for a candidate trial.

### Changes since 0.6.0

- **Isolate a candidate command.** `exacly isolate` and the optional
  `linux-bubblewrap-v1` backend use a reviewed detached root filesystem,
  immutable input snapshots and a verified namespace gate. The launcher clears
  inherited authority, denies host network and operator interfaces, applies a
  fixed syscall filter, and collects bounded untrusted logs and regular
  artifacts into a new private bundle. Kernel keyring, socket-family and
  `perf_event_open` restrictions are checked before candidate execution;
  Exacly does not change host kernel settings to make a launch succeed.
- **Dispatch reviewed work durably.** `exacly dispatch github-issues` previews
  or explicitly creates one GitHub task and associates confirmed work with
  pending verification and an execution binding. A private journal retains
  intent before remote effects; an ambiguous create is never blindly repeated.
  `dispatches` commands initialize, inspect, recover and supersede dispatches.
  Separately authorized `resolve` records stale partial registration without
  changing work or remote tasks. Journal recovery retains a protected backup.
- **Bound execution attempts.** `exacly execute local` previews or separately
  authorizes one snapshot command for an active registered verification task.
  A private companion journal retains durable attempts, retry budgets and shared
  concurrency limits. The supervisor enforces the complete worker's wall-clock
  budget, retains host-derived output build identities, and provides offline
  inspection, cancellation and recovery through `executors` commands. Execution
  does not change repository source, admit evidence or complete pending work.
- **Compose coordinator backends.** Public attempt-storage, invocation-ownership
  and worker-lifecycle protocols support a trusted Python backend bundle.
  Callback identities, event order and terminal outcomes are checked before
  durable append, preserving readable history when an adapter misbehaves.
  Protocol `0.1.0` supports one `local-machine` domain and the existing local
  adapter. Another provider or distributed ownership requires a separately
  reviewed contract and real backend verification.
- **Hand reviewed artifacts to Git.** `exacly handoff local-git` previews exact
  mapped changes, a tree and a commit from one retained successful executor
  attempt. Separate authorization permits publication to one local branch in
  an explicitly initialized private bare SHA-1 repository. Trusted code validates
  the complete base, bounded selected artifact bytes, allowed paths and expected
  ref before preparing intent; missing or corrupt base objects leave storage
  unchanged. A separate journal retains exact prepared object bytes and
  provenance for immutable replay and model-independent recovery. Conditional
  ref publication preserves divergent work and unrelated base files.
- **Compose evidence and gates.** `exacly composition validate` checks an
  approved required-member definition offline. `exacly compose github-actions`
  performs fresh native admission and readiness evaluation for every explicitly
  selected member under its own approved producer and model contract. Every
  required gate must pass. Private output retains native evidence and an atomic
  aggregate, distinguishes incomplete work from success and preserves original
  namespaces. The CLI streams saved binary evidence; Python callers can retain
  native payloads or select synchronous streaming retention. Required platform
  inventories and external release identities remain consumer responsibilities.
- **Report readiness as JUnit.** `exacly gate --junit FILE --junit-id LABEL`
  exports the existing native evaluation with stable versioned case selectors.
  `compose github-actions --readiness-junit` adds private `readiness.xml` with a
  verdict for every required member, including rejected admissions. Unknown
  prerequisites remain blocking failures. The serializers perform no extra
  evaluation or admission, and output failures return an invocation error.
- **Keep partial development checks separate.** `mise run check-core` omits
  selected local runtime, ownership and handoff modules. Its separate JUnit
  report does not refresh model state or establish full verification. Required
  Linux CI and release checks still run the complete suite and verification gate.

### Supported scope and runtime

Core model, planning, admission and completion commands do not require
Bubblewrap. Local isolation and execution need the supported Linux kernel
features, Bubblewrap 0.8 or later, system libseccomp and an independently reviewed
detached Python runtime. The [isolation guide](docs/candidate-isolation.md)
describes the actual prerequisites and operator-managed AppArmor setup. A missing
protection stops execution before candidate code starts; there is no host fallback.
The boundary relies on the trusted host kernel, runtime and operator and does
not provide cgroup resource containment or distributed execution.

Dispatch, execution and handoff use separate explicitly initialized private
journals. Handoff additionally requires sealed private bare Git storage with the
complete reviewed base already imported. It supports adding or replacing
ordinary files, preserves existing modes and unrelated entries, and performs no
checkout or implicit fetch. Remote publication, binding supersession, trusted
producer execution, evidence admission, completion, release and deployment retain
their separate authorization.

CAP-004 and PROC-004 remain proposed. Releasing these components does not
establish unattended end-to-end automation. The local acceptance pilot uses real
isolated execution and controlled producer responses; it does not establish a
live remote publication or production producer run.

### Formats and upgrading from 0.6.0

The package version is independent of specification, report and storage versions.
Existing `0.1` product maps and `0.2` config/state remain readable. Existing
binding/completion ledgers keep their formats; opening an existing store never
silently migrates, activates or repairs it.

| New contract | Version in 0.7.0rc1 |
| --- | --- |
| Isolation policy/report and dispatch contract/report | `0.1.0` |
| Executor contract/report and coordinator backend protocol | `0.1.0` |
| Local Git handoff request, policy and provenance/report | `0.1.0` |
| Composition definition, selection and aggregate report | `0.1.0` |
| Dispatch, executor and handoff SQLite journals | Separate schema-version-1 stores |
| Readiness JUnit case selectors | `exacly.gate/v1/...` and `exacly.composition/v1/...` |

Retain the original saved plans, policies and normalized state destinations for
registered completion and historical replay. New plans identify tool version
`0.7.0rc1` and can have different digests; they do not replace the registered
originals. Producer workflow changes, including package pins, require regenerated
and independently reviewed producer-policy candidates.

Existing version-1 ledgers still require the explicit copy-only upgrade before
completion; version 2 needs no profile activation. Version-4 profiles identify
actual validation code, schemas, installed dependency artifacts and runtime.
Those inputs may change when upgrading or recreating the environment; the version
label and README alone do not invalidate a profile. A recognized stale profile
permits read-only fresh validation but blocks new operational writes until an
explicit copy-only refresh:

```sh
exacly executions activate --ledger SOURCE --out NEW_DIR
```

Review the output, then explicitly select
`NEW_DIR/operational/ledger.sqlite3` as the one canonical operational ledger.
Activation preserves the source and historical proof, performs no automatic
cutover and does not resolve prepared completion work. Version 3 remains
audit-only; unsupported or corrupt inputs require their existing diagnostic and
recovery procedures.


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
