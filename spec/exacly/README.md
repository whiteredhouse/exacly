# Exacly's own model

Exacly models itself, following the layout in section 13 of the specification. It is the first model whose state will come from real observations.

| | |
| --- | --- |
| Namespace | `exacly` |
| Owner | `exacly-maintainers` |
| Specification | [0.2.0](exacly-0.2.0.md), an unmodified copy of [`../versions/0.2.0.md`](../versions/0.2.0.md) |
| Boundary | [`model.yaml`](model.yaml) |
| Extensions | None |

## Config

- [`validation.yaml`](validation.yaml) covers checking a model. The validator, CI and the repository exist today, so these records are active.
- [`observation.yaml`](observation.yaml) covers observing a model's reality: `exacly refresh` and the CI job that runs it on main are active, and the measure they feed is proposed.
- [`understanding.yaml`](understanding.yaml) covers answering questions about a model: the `exacly` command's show, trace, impact and gaps.
- [`planning.yaml`](planning.yaml) covers target-specific verification plans.
- [`admission.yaml`](admission.yaml) covers live GitHub evidence admission under an approved producer policy.
- [`reconciliation.yaml`](reconciliation.yaml) covers read-only completion previews.
- [`executions.yaml`](executions.yaml) covers operator registrations and the private execution ledger.
- [`execution-maintenance.yaml`](execution-maintenance.yaml) covers bounded access, diagnosis, backup and copy-only ledger recovery.
- [`automation.yaml`](automation.yaml) records the remaining automation roadmap as proposed Requirements, implementers and data concepts.

## Implemented automation foundations

These foundations are active in the model:

| Record | Implemented work |
| --- | --- |
| REQ-010 | Separate model and implementation identities in refresh. |
| REQ-018 | Target-specific saved verification plans. |
| REQ-019 | Live GitHub evidence admission under an approved producer policy. |
| REQ-020 | Read-only reconciliation previews. |
| REQ-021 | Immutable operator registrations for existing pending tasks. |
| REQ-022 | Ledger diagnosis, backup, copy-only recovery and bounded lock waits. |
| REQ-023 | Conditional atomic state publication shared by current observers. |

## Proposed automation

The [plan/apply research](../../docs/automated-plan-apply-research.md) informs these proposals. Their traceability runs through `PROC-004` and `CAP-004` to `OUT-001`.

| Requirement | Proposed work | Implementer |
| --- | --- | --- |
| REQ-024 | Complete one registered verification task from fresh admitted evidence, with durable intent and receipt recovery. | APP-012 |
| REQ-025 | Isolate candidate code from operator policies, ledger and credentials. | APP-014, using INF-004 |
| REQ-026 | Dispatch authorized work idempotently, including recovery after an uncertain external response. | APP-013 |
| REQ-027 | Run an executor within explicit time, retry and authority limits, retaining output identities. | APP-014 |

Implement controlled local completion first, then task dispatch, then bounded candidate execution. Isolation is required before candidate execution; trusted local completion does not itself run candidate code. `INFO-004` and `INFO-005` describe future journal content requiring an explicit ledger schema upgrade.

All records in `automation.yaml` have `lifecycle: proposed`, and implementation and runtime references are marked planned. Their test case patterns name future suites that do not exist yet; refresh currently selects no matching cases and reports `not_checked`. They carry no verification or delivery claim. The CI readiness policy continues to require only the 23 active Requirements. Promote each proposal when its behavior and matching tests are implemented and observed.

## State

[`state/main.json`](state/) is the state for main, written only by `exacly refresh` and committed only from main by CI. On each push to main, the workflow's `state` job reruns the checks, refreshes the state from the test results, validates it with the config, and commits it with `[skip ci]`. It doesn't commit if a check fails. Every push to main starts this job, whatever it changed, so if main moves on before the state is pushed, the newer push's job writes the state instead. Any other push failure, such as branch protection, fails the job.

`mise run check` runs the same refresh locally and on pull requests, writing to `reports/state/main.json` without committing anything. A proposed change can show its effect on state this way, but never stores it. Validation in CI reads that fresh state rather than the committed one, so a change that removes a record isn't blocked by state from before it.

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
