# Exacly's own model

Exacly models itself, following the layout in section 13 of the specification. CI records observations as state alongside the authored config.

| | |
| --- | --- |
| Namespace | `exacly` |
| Owner | `exacly-maintainers` |
| Specification | [0.2.0](exacly-0.2.0.md), an unmodified copy of [`../versions/0.2.0.md`](../versions/0.2.0.md) |
| Boundary | [`model.yaml`](model.yaml) |
| Extensions | None |

## Config

- [`validation.yaml`](validation.yaml) covers document, relationship and traceability checks.
- [`observation.yaml`](observation.yaml) covers state observation, publication and the measurement definition it supports.
- [`understanding.yaml`](understanding.yaml) covers answering questions about a model: the `exacly` command's show, trace, impact and gaps.
- [`planning.yaml`](planning.yaml) covers target-specific verification plans.
- [`admission.yaml`](admission.yaml) covers drafting producer-policy candidates and live GitHub evidence admission under an independently approved policy.
- [`composition.yaml`](composition.yaml) covers approved required-member definitions, whole-input preflight and fresh per-member admission/readiness evaluation with retained aggregate decisions.
- [`evidence-packaging.yaml`](evidence-packaging.yaml) covers the installed evidence producer and its shared manifest-format contract.
- [`reconciliation.yaml`](reconciliation.yaml) covers read-only completion previews.
- [`executions.yaml`](executions.yaml) covers operator registrations and the private execution ledger.
- [`execution-maintenance.yaml`](execution-maintenance.yaml) covers bounded access, diagnosis, backup and copy-only ledger recovery and upgrade.
- [`ledger-history.yaml`](ledger-history.yaml) covers separate audit-only conversion and operational indexed activation, including the proof-integrity, validation-profile and semantic-reuse contracts described in the [ledger-history scaling design](../../docs/ledger-history-scaling.md).
- [`automation.yaml`](automation.yaml) covers verification completion, candidate isolation, dispatch and executor control.

## Automation roadmap

The YAML records define requirements, implementers, dependencies and lifecycle. Their observation bindings connect them to verification evidence in state. Consult the records for current definitions and lifecycle, and observed state for what has been verified or released.

REQ-030 and APP-017 are active for the implemented `exacly evidence package`
command and `exacly.evidence` API, included in `0.6.0`. Active lifecycle
describes the current contract; observed verification and release state remain
separate. The package and admission share manifest `0.1.0` definitions, without
making packaging an admission or completion decision. See the [producer and installation
guidance](../../python/README.md#package-verification-evidence).

REQ-019 and APP-008 also cover the installed `exacly evidence propose-policy
github-actions` command for drafting a candidate from explicit producer and
model inputs. This command is included in `0.6.0` and reuses the existing
`exacly.admission.propose_policy` API. Drafting grants no approval or execution
authority. See the [candidate-policy guidance](../../python/README.md#draft-a-producer-policy)
for the independent review and controlled storage steps.

The `0.6.0` contract for REQ-021 creates new registration ledgers in
completion-ready schema version 2. Existing ledgers keep their format,
version-1 upgrade remains explicit, and version-4 activation remains a separate
choice. REQ-019 also requires admission bundles to use POSIX `0700` directories
and `0600` files, independently of the caller's umask. These are current
contracts; verification and release claims still come from observed state.

REQ-032 and APP-018 cover the composition commands and `exacly.composition` API.
The definition binds required member roles to their approved policy and model
inputs; every invocation pins that identity and selects all required runs and
targets explicitly. INFO-009 records the per-member and aggregate decisions in
INF-006, a new private output bundle separate from authoritative state and the
execution ledgers. The [composition guide](../../docs/admission-composition.md)
describes the `0.7.0rc1` candidate. Active lifecycle identifies the
implemented contract; matching observations establish verification and delivery.

- Browse the [published model and observations](https://whiteredhouse.github.io/exacly/project/) for the checked baseline on main. Branch proposals appear there after merge and successful CI publication.
- Read [`automation.yaml`](automation.yaml) for the automation contracts and process steps, the [durable dispatch guide](../../docs/durable-dispatch.md) for approved GitHub task creation and recovery, the [bounded executor guide](../../docs/bounded-executor.md) for local attempt supervision, the [output handoff guide](../../docs/output-handoff.md) for reviewed artifact-to-Git publication, the [local Bubblewrap backend contract](../../docs/candidate-isolation.md#linux-bubblewrap-v1-backend-contract) for the current isolation implementation, and the dated [plan/apply research](../../docs/automated-plan-apply-research.md) for the original design.
- Run `exacly show RECORD`, `exacly trace RECORD` or `exacly gaps` from the repository root to inspect the local checkout. Preview a branch's model with `exacly view site --out reports/model-site`.

Implementation and runtime references can be marked `planned`; observation bindings have no special planned mode. A binding with case patterns that select no executed tests produces `not_checked` and establishes no successful verification claim.

The [completion commands](../../python/README.md#complete-registered-verification-work) authorize local state publication for an existing registered task. They do not launch candidate code or require the optional local isolation runtime. Exacly owns the approved work contract, evidence admission and completion decision; the selected runtime enforces the execution boundary required by REQ-025.

The [dispatcher](../../python/README.md#dispatch-approved-verification-work), APP-013, uses a separately initialized private journal for durable GitHub issue intent, discovery and recoverable registration. It keeps existing execution-ledger formats unchanged and leaves independent evidence admission and completion in control of verification. Ambiguous issue creation never grants permission for a second create merely because discovery finds no task. A separately reviewed [journal-only resolution](../../docs/durable-dispatch.md#resolve-stale-partial-registration) can terminate coordination after stale partial registration while preserving current state, pending work and bindings; it establishes no completed registration or verification claim.

The [isolated command launcher](../../python/README.md#isolate-a-candidate-command), APP-016, is the current local backend for the isolation contract. It delegates isolation to installed Linux Bubblewrap and verifies the actual boundary before running an explicitly authorized command. Its real integration tests verify this backend; they establish no support for a remote provider or another runtime. Every executor backend must satisfy the isolation contract without gaining evidence or completion authority.

The [bounded local executor](../../python/README.md#run-a-bounded-local-executor), APP-014, adds reviewed task authorization, durable attempt history and shared concurrency limits through a separately initialized private companion to the execution ledger. The first adapter delegates candidate execution to APP-016, accepts explicit snapshots, permits no repository effects and returns host-derived content build identities. Execution, recovery and cancellation preserve pending verification work and cannot admit evidence or complete it. Existing binding/completion ledger formats remain unchanged. The guide describes the `0.7.0rc1` candidate; lifecycle and fresh observations remain separate.

REQ-031 covers the coordinator's explicit [backend contracts](../../docs/coordinator-interfaces.md) for attempt storage, invocation ownership and worker lifecycle. APP-014 consumes the composed protocols throughout execution, inspection and recovery; its local defaults retain the existing CLI, policy and history formats. Shared reference cases establish interface semantics, while the real local runtime and crash tests establish backend behavior. Distributed scope remains unsupported by protocol `0.1.0` and needs independently verified fencing and migration guarantees before another coordinator can be supported.

REQ-033 and APP-019 cover [reviewed output handoff](../../docs/output-handoff.md): selected artifacts from a retained successful attempt become an exact source commit through separately authorized local Git publication. The handoff pins initialized private journal and repository identities, approved base and destination paths, retained artifact bytes and commit metadata; prepared object bytes and compare-and-swap ref publication support replay and model-independent recovery. INFO-010 describes its provenance and INF-007 its separate private journal and bare Git storage. Binding replacement, remote publication, evidence admission and completion keep their separate authorization. CAP-004 and PROC-004 remain proposed; a tested local handoff does not by itself establish the whole automation loop or a live remote producer.

When implementing a proposal, update its lifecycle and planned references, review the acceptance tests and observation bindings, and update the [verification policy](../../ci/verification.yaml) in the same change. CI checks that this policy covers every active Requirement; successful verification still requires matching observations.

## State

[`state/main.json`](state/) is the state for main, written only by `exacly refresh` and committed only from main by CI. On each push to main, the workflow's `state` job reruns the checks, refreshes the state from the test results, validates it with the config, and commits it with `[skip ci]`. It doesn't commit if a check fails. Every push to main starts this job, whatever it changed, so if main moves on before the state is pushed, the newer push's job writes the state instead. Any other push failure, such as branch protection, fails the job.

`mise run check` runs the same refresh locally and on pull requests, writing to `reports/state/main.json` without committing anything. A proposed change can show its effect on state this way, but never stores it. Validation in CI reads that fresh state rather than the committed one, so a change that removes a record isn't blocked by state from before it.

The optional `mise run check-core` developer subset writes a separate test report and runs without provisioning the local Bubblewrap backend. It omits selected local execution, ownership and handoff test modules, does not refresh state or enter the verification gate, and establishes neither REQ-025/027 verification nor the complete REQ-031/033 contracts. Controlled coordinator substitution cases remain in the subset; local ownership, handoff contract and process-fault cases using POSIX process identity, and the real isolated handoff pilot require the complete Linux checks.

Refresh and manual-QA observation use a shared conditional atomic writer for local state files. A command captures the earlier file before computing observations, then checks that it is unchanged under a parent-directory lock before replacing it. A conflict leaves the newer state in place and requires another observation. This local file protocol is separate from the workflow's Git push check and from execution-ledger transactions.

`--distribution` says that main is how Exacly is distributed, since people install it from the repository. So an implementer present at the revision counts as released there.

## Bindings

A `tests` binding tells refresh which results verify a record:

```yaml
observe:
  tests:
    workflow: .github/workflows/spec.yml   # the workflow that runs the tests
    cases:                                 # optional: glob patterns over case IDs
      - "python/tests/*/cases.yaml::7.*"
```

A case ID is `<cases file>::<description>` for a data-driven case, and `<test file>::<test name>` for any other test. Patterns are case-sensitive. Without `cases`, the job's own result is the evidence. Each test reports its case ID in the JUnit report, as the `exacly-case` property.

## Validating

```sh
mise run model
```

This runs the tests, refreshes state into `reports/state/main.json`, and validates the config with that state for the current commit. CI runs it on every change as part of `mise run check`. A binding may also name a `job`; Exacly's don't, because the checks run in both the `check` and `state` jobs.
