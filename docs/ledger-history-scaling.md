# Ledger history scaling

Decision date: 2026-10-28. Baseline: [`da21cd8`](https://github.com/whiteredhouse/exacly/commit/da21cd8ee446191fa04a107cb1a00cc0cada6586), including operation-scoped validation from PR #58.

Use a **profile-bound proof validation index in a new opt-in ledger format** for the measured semantic bottleneck. The operational contract requires every retained proof byte and all live/global invariants to remain checked. Only previously validated terminal payload semantics may eventually be reused, when exact content, dependencies and the installed validator profile match. Full audit always re-derives those semantics.

The conversion and audit foundation creates an **audit-only version-3 copy** from a fully validated version-2 ledger. It records required integrity baselines and terminal semantic verdicts, then audits the copy read-only with fresh full semantics. It does not enable operational reuse, operational index-aware writers, ordinary version-3 maintenance or cutover, and establishes no performance improvement. Current version-1/version-2 operational validation remains unchanged. The canonical acceptance contracts and lifecycle are in [`ledger-history.yaml`](../spec/exacly/ledger-history.yaml).

## Workload and evidence

PR #58 removes duplicate validation within an unchanged protected database view. Each fresh view and successful mutation still checks all history: SQLite structure, binding contracts, supersession chains, watermarks, retained reports/models/states, observation derivation, exact transitions and receipts.

The earlier benchmark used one binding and additional aborted attempts. The expanded benchmark creates independently identified completed tasks, sequential state publication, optional queued pending work, supersession chains and larger semantic proof. Tasks have unique pending, binding, run, report, archive and completion identities while sharing an approved model and selected Requirement goal. This isolates history growth from changes to the goal; it is not a model of many distinct Requirement goals or output implementations.

These are controlled offline fixtures, not live GitHub evidence or production traffic. Fixture construction and its initial full validation are excluded from timing. Repeated runs use warm filesystem caches. Results record Python, SQLite, platform, baseline revision, workload parameters, byte sizes and repeat counts. Nested component timers are inclusive and must not be added together.

### Measurements

See [`benchmarks/README.md`](../benchmarks/README.md) for commands, raw JSON and profiling details.

The [raw baseline](../benchmarks/results/ledger_history_baseline.json) contains 16 scenarios. Completion histories use one measured repetition; binding-only rows use the median of three. Times below are seconds and sizes are decimal MB. These are diagnostic measurements from one workspace.

| Completed-task workload | Full validation | Terminal payload semantics | Payload read + SHA256 | Retained payload MB | SQLite MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10, sequential, 1 case / 1 Requirement | 0.308 | 0.299 | 0.000260 | 0.112 | 0.266 |
| 100, sequential, 1 case / 1 Requirement | 3.306 | 3.210 | 0.001983 | 1.121 | 2.191 |
| 300, sequential, 1 case / 1 Requirement | 9.515 | 9.238 | 0.005836 | 3.365 | 6.500 |
| 100, initially queued, 1 case / 1 Requirement | 4.563 | 4.445 | 0.003544 | 2.628 | 3.695 |
| 300, initially queued, 1 case / 1 Requirement | 18.974 | 18.529 | 0.017821 | 17.085 | 20.214 |
| 10, sequential, 100 cases / 10 Requirements | 0.567 | 0.549 | 0.000756 | 0.680 | 0.836 |
| 10, sequential, 1,000 cases / 100 Requirements | 3.456 | 3.352 | 0.005315 | 6.120 | 6.275 |

At 300 initially queued tasks, retained before/after state alone occupies 14.721 MB, versus 1.001 MB when queuing one task at a time. Every snapshot repeats the remaining pending list. The index does not remove these bytes or solve their storage growth.

| Binding-only workload | Full validation | Binding decode | Remaining base/SQLite work | Analytical group comparisons |
| --- | ---: | ---: | ---: | ---: |
| 1,000 bindings / 1,000 pending identities | 0.137 | 0.078 | 0.058 | 1,000,000 |
| 3,000 bindings / 3,000 pending identities | 0.759 | 0.218 | 0.541 | 9,000,000 |
| 3,000 bindings / 1,000 pending identities, depth 3 | 0.452 | 0.237 | 0.215 | 3,000,000 |

The remaining column is the median base/SQLite duration minus median binding decode, not an isolated membership timer. Equal binding counts with different pending-group counts support the code-level repeated-scan analysis; they do not isolate every other timing factor.

The measured bottleneck is semantic interpretation, especially model/state and binding-schema validation. Hashing retained bytes and SQLite quick checks are much cheaper at these sizes. This supports investigating semantic reuse; it does not measure the latency of a completed index implementation or establish production-scale capacity.

The binding validator also filters all binding rows for each pending identity. Its current candidate-comparison count is `pending identities × binding rows`. That is an analytical count from the implementation, not an instrumented counter. Separate binding-only runs measure the cost without semantic proof work. Future catalogue/index integration must avoid carrying this repeated unrelated-row scan forward.

## Alternatives and decision

| Approach | Benefit | Remaining cost or tradeoff |
| --- | --- | --- |
| Operation-scoped reuse, already implemented | Removes repeated checks within one unchanged protected view | Each fresh view still interprets all proof. |
| Compile/reuse schema validators and improve binding grouping | Reduces constant factors and repeated unrelated-binding scans while preserving fresh semantics | Necessary complementary work, but repeated historical semantic work remains. |
| Profile-bound validation index, selected first | Reuses terminal payload semantics while preserving whole-history byte checks | Requires a new explicit integrity/profile contract and trusted index writes. All-byte reads, metadata and retained storage still grow. |
| Copy-only archival with a permanent identity catalogue | Moves unrelated cold payload pages and semantic work out of the hot path | Adds multi-file maintenance, rotation cadence and selected/full-audit scope distinctions. Unrelated cold byte corruption may remain undetected until audit. |

An integrity index is the more proportionate next implementation for the measured CPU bottleneck. It also avoids introducing cold-proof availability and multi-file cutover obligations merely to remove repeated model/report interpretation. Archival remains a separate option when hot storage footprint or physical I/O becomes a demonstrated concern.

A properly bound index and an archive catalogue both rely on the controlled operator runtime and protected retained references. Neither a digest nor a stored success flag independently authenticates who performed validation. This decision does not dismiss indexing as inherently unsafe or claim that archives inherently establish stronger trust.

The raw SHA256 timing is a data-processing comparison only. It does not check criteria, establish semantic validity, implement the required transcript/index, or authorize completion. It must not be presented as the proposed index's measured speedup.

## Conversion and audit foundation

[`executions index` and `executions audit`](../python/README.md#create-and-audit-an-integrity-index-copy) implement the conversion and audit scope of REQ-029. Conversion accepts a clean version-2 source with no unresolved prepared intent and publishes a new bundle containing the exact raw source, an audit-only indexed copy and a manifest. Dry-run fully validates the source and destination plan without creating output. Audit validates required baselines and all semantic transitions regardless of matching stored verdicts; unsupported profiles and integrity mismatches are explicit failures, never read-only fallback or repair.

The report contract identifies checked scope, profile and the copy's ineligibility for operational use. Keep the version-2 source as the working ledger. Existing registration, show, reconciliation, completion, backup and recovery commands continue to reject version 3. This isolates the conversion and audit invariants before any command is allowed to rely on a durable semantic verdict.

The foundation fixes profile family `exacly-ledger-audit-v1` and typed transcript codec `typed-cells-v1`. The required baseline commits the complete retained-row roster, while each terminal verdict binds only its semantic dependency closure: its exact binding row, intent row and receipt row or explicit receipt absence. Global relationships remain separately checked. The profile independently fingerprints installed code, schemas, parser dependencies and runtime; audit requires that matching installation. This separation supplies the integrity foundation without enabling operational semantic reuse or writers.

REQ-028 retains the complete proposed operational contract below. Its dedicated reuse and integration test selectors remain separate from the conversion/audit tests, so passing the foundation cannot imply that operational reuse or index-aware completion has been verified. The earlier benchmark measurements describe the existing version-2 bottleneck and hashing comparison; they do not measure a completed index-assisted operational path.

## Proposed operational index contract

### Required integrity baseline and optional semantic reuse

Keep two distinct concepts:

1. A required integrity baseline covers the exact retained proof and dependency bytes for the indexed format, with exact row-set coverage.
2. An optional current-profile semantic result records that a terminal payload and its derived facts passed validation for that exact baseline and independently selected validator profile.

Missing or extra required entries, duplicate identities, unknown references, malformed baseline records and digest mismatches are integrity errors. Normal reads and full audit must not interpret them as cache misses, validate whatever bytes survive and silently replace the baseline.

Only an absent or deliberately invalidated semantic result, or a recognized stale profile whose required integrity baseline still matches, may trigger fresh semantic validation in read-only memory. A malformed same-profile result or inconsistent dependency/verdict association is an error. Ordinary reads never insert entries, change the baseline, reindex or upgrade a ledger. Unsupported semantics produce an explicit unsupported result. Profile refresh is explicit maintenance after baseline consistency and full semantics have been checked; it cannot repair or bless an integrity mismatch.

### Exact coverage and trusted profiles

The versioned transcript needs a domain separator, typed length framing, field names, stable order and explicit absent/null markers. It binds raw retained TEXT/BLOB bytes and every dependency that influences validation: binding contract, request and evaluation date, model, policies, admitted state and receipt, archive/report, before/after state, phase, completion receipt and any reusable derived summary.

Static facts extracted to avoid repeated decoding, such as before/after serials and state digests, belong to the bound semantic result. A mutable summary cannot become an unchecked source of authority. Exact coverage must include every required historical identity; checking only index rows that happen to exist is insufficient.

The expected profile comes from trusted installed code and its compatibility registry, never from a caller's assertion or a ledger's declaration that it is trusted. Changes to relevant validation semantics invalidate reuse. The implementation must define the profile identity, dependency closure and compatibility rules before accepting any stored result.

### Fresh checks on every operation

Normal validation still checks:

- Exact supported schema, SQLite integrity and foreign-key relationships.
- All retained proof bytes against their required baseline and all index associations.
- Binding and supersession membership, active bindings, current watermarks and required state identities.
- Receipt/phase uniqueness, prepared-intent count and cross-row consistency.
- Actual current state and operation authorization under the existing commit-time locks when the selected operation requires them. Historical show and exact receipt replay retain their existing offline behavior without a new current-state dependency.

Only terminal payload semantics may be reused. Prepared intents, new evidence evaluations and newly changed payloads receive fresh semantic validation. A cached historical result never substitutes for fresh live admission when completing new work, nor for checking the current merged state and exact pending item.

Completed pending identities remain permanently unavailable for reuse. Aborted intent IDs remain historical; a fresh-admission retry can use the same request digest with a new intent ID. Supersession, watermarks, exact historical receipts and offline replay retain their existing meanings.

### Writes, copying and recovery

New terminal integrity/semantic entries are created only after fresh validation, including for aborted intents. They commit in the same durable SQLite transaction as the corresponding phase, receipt and watermark changes. Known nonterminal changes must maintain the required byte baseline atomically and cannot leave a usable stale terminal verdict.

Failed statements, commits or rollback attempts must not leave partial usable entries. Crash tests need to cover preparation, state publication, receipt finalization and index maintenance together. SQLite hot-journal recovery and completion publication recovery remain separate explicit procedures; an index never authorizes replay of an archived or stale candidate into current state.

The new format is obtained through explicit copy-only maintenance into a new private destination after full source validation. Source files and exact historical proof remain unchanged, including binding/request identities, before/after bytes and original receipts. Unsupported inputs and unresolved prepared intents initially block conversion; operators use the existing explicit recovery first.

Validate the complete output, synchronize its files and directory, then atomically publish the new copy without overwrite. Operational format integration will require explicit selection of one canonical ledger; old and copied files must not be parallel active authorities. The current audit-only copy is ineligible for this cutover. Ordinary commands never migrate or recover implicitly. Future index-aware backup and copy-only recovery must retain the complete proof and index and validate both; copying a successful cache record alone cannot establish a valid ledger.

## Audit, retention and claims

| Validation path in the proposed format | What is checked | What can be reported |
| --- | --- | --- |
| Normal operation, matching terminal profile | Whole-history byte consistency, index coverage, fresh live/global invariants; matching terminal semantics reused | All retained bytes checked; named terminal semantics reused under the stated profile. |
| Matching integrity baseline, missing/stale semantic result | The same checks plus fresh semantic validation of the affected payload, without writing the index | Fresh semantics checked for the named payload/profile; no durable refresh claimed. |
| Explicit full audit | Required baseline consistency and every retained semantic transition, ignoring positive reuse entries | Full audit passed only when both byte consistency and fresh semantics pass. |
| Copy/upgrade/profile refresh/backup/recovery | Coherent source/copy, complete proof, required baseline, index associations and full semantic audit | A complete validated copy or explicit supported maintenance result. |

Reports must identify the profile, all-byte coverage, fresh/reused semantic counts and any failed or unsupported checks. They must distinguish byte integrity with reused semantics from a fresh full audit. Audit success cannot erase an existing baseline mismatch or rewrite a historical receipt.

The first retention policy is **keep all historical bytes indefinitely**. There is no archival, garbage collection, expiry or deletion authority. Storage and whole-history byte checking therefore continue growing; this proposal makes no constant-space or constant-latency claim. Index associations should use indexed lookups, and global membership checks should group identities without rescanning unrelated rows per pending key.

Matching bytes, profiles and cached facts do not independently prove who produced the positive result. A coherent rewrite of proof and index by the operator can fabricate a cached semantic verdict. A fresh full audit can expose invalid semantics, but does not authenticate evidence origin or test honesty. Neither the index nor the current unsigned receipt protects against a hostile same-user operator or proves that an old coherent whole-ledger copy is the latest global generation. Strong rollback detection would require an independent monotonic trust anchor outside the restored ledger.

## Implementation sequence

[`REQ-028`](../spec/exacly/ledger-history.yaml) defines the complete future acceptance contract. Its dedicated operational-reuse tests select no current implementation. REQ-029 and its conversion/audit tests cover the foundation, while benchmark tests validate fixture construction and the measured version-2 store.

1. Establish the index/transcript/profile schema and explicit copy-only conversion, integrity validation and full audit, preserving v1/v2 behavior. The audit-only foundation implements this scope under REQ-029.
2. Add semantic reuse for unchanged terminal payloads and read-only fallback, retaining all-byte and global checks. Test missing/extra index rows, altered dependencies, profile changes and corrupt proof/index entries.
3. Integrate terminal index writes with real completion, abort, backup and recovery transactions. Run process-kill/restart, exact offline replay, supersession and current-state race cases against the new format.
4. Repeat the independent-binding, supersession, queued-backlog and large-proof workloads. Report normal indexed validation and full audit separately; verify semantic work counts and index coverage without machine-dependent timing assertions.

Promote only behavior whose implementation and acceptance tests exist, updating active verification policy coverage in the same change. Keep archival as a separate decision when representative physical storage/I/O measurements or an approved retention policy justify it. The original plan/apply research remains unchanged; this decision and the product model carry the follow-up.

## References

- [`execution_store.validate`](../python/src/exacly/execution_store.py): current SQLite, binding, chain and watermark checks.
- [`completion_store`](../python/src/exacly/completion_store.py): retained semantic proof checks, exact state transitions and operation-scoped reuse.
- [`execution_maintenance`](../python/src/exacly/execution_maintenance.py): protected copy publication, backup, recovery and upgrade.
- [`execution_history`](../python/src/exacly/execution_history.py) and [`history_maintenance`](../python/src/exacly/history_maintenance.py): the audit-only integrity format, conversion and fresh semantic audit foundation.
- SQLite [`quick_check`](https://www.sqlite.org/pragma.html#pragma_quick_check), [`data_version`](https://www.sqlite.org/pragma.html#pragma_data_version) and [online backup](https://www.sqlite.org/backup.html) document storage checks; none establishes semantic evidence validity or a globally monotonic ledger identity.
