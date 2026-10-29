# Ledger history scaling

Decision date: 2026-10-28. Baseline: [`da21cd8`](https://github.com/whiteredhouse/exacly/commit/da21cd8ee446191fa04a107cb1a00cc0cada6586), including operation-scoped validation from PR #58.

Use a **profile-bound proof validation index in an explicitly activated version-4 ledger** for the measured semantic bottleneck. Operational validation checks every retained proof byte and all live/global invariants, then may reuse previously validated terminal payload semantics only when exact content, dependencies and the installed validator profile match. Full audit always re-derives those semantics.

The original conversion and audit foundation retains its **audit-only version-3 copy** and strict fresh audit. Separate operational activation creates a fully validated version-4 copy from supported version-2, version-3 or version-4 history, preserving the source and requiring explicit canonical cutover. Version 3 is never promoted through an ordinary read or write. Version-1/version-2 operational validation remains unchanged. The canonical acceptance contracts and lifecycle are in [`ledger-history.yaml`](../spec/exacly/ledger-history.yaml).

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

At the recorded baseline, the binding validator filtered all binding rows for each pending identity. Its candidate-comparison count was `pending identities × binding rows`, an analytical count rather than an instrumented counter. Separate binding-only runs measured that cost without semantic proof work. Released validation now groups binding membership once; the operational comparison in the [benchmark guide](../benchmarks/README.md#operational-semantic-reuse) measures the current engines separately from the historical baseline.

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

The version-3 report contract identifies checked scope, profile and the copy's ineligibility for operational use. Registration, show, reconciliation, completion, backup and recovery continue to reject version 3. Operational use requires separate activation into version 4, with its own validated copy and operator cutover.

The audit-only foundation fixes profile family `exacly-ledger-audit-v1` and typed transcript codec `typed-cells-v1`. The required baseline commits the complete retained-row roster, while each terminal verdict binds only its semantic dependency closure: its exact binding row, intent row and receipt row or explicit receipt absence. Global relationships remain separately checked. The profile independently fingerprints installed code, schemas, parser dependencies and runtime; version-3 audit requires that matching installation. The operational version-4 contract preserves this separation with explicit profile handling and transactional index maintenance.

REQ-028 covers the complete operational contract below, with dedicated reuse, CLI, maintenance and process-interruption tests. REQ-029 remains the separate version-3 conversion/audit contract, so passing its foundation tests alone does not establish operational reuse or index-aware completion. The earlier benchmark measurements above describe the version-2 bottleneck and hashing comparison; they are not measurements of the version-4 implementation.

## Operational activation and index contract

The [paired operational measurements](../benchmarks/results/ledger_history_reuse.json) cover all 16 published workload dimensions on the same retained rows. Each is one warm-cache measurement in this workspace. The timers cover the validation engines, including actual installed-profile and whole-byte checks, and exclude fixture setup, activation, lock/connection setup and live evidence admission. Both measured versions share the improved binding grouping and compiled constant binding schema; those improvements are not attributed to terminal reuse.

| Workload | Current v2 full validation (s) | V4 normal reuse (s) | V4 fresh full audit (s) |
| --- | ---: | ---: | ---: |
| 300 sequential completions | 5.970 | 0.367 | 6.073 |
| 300 initially queued completions | 15.711 | 0.423 | 16.229 |
| 10 completions, 1,000 cases / 100 Requirements per report | 3.041 | 0.093 | 3.265 |
| 3,000 open bindings, no terminal proof | 0.273 | 1.739 | 1.835 |

Normal validation reuses all matching terminal payloads in these completed-history scenarios while checking every retained source byte and global association. Ledgers without terminal proof have no semantic work to reuse: profile fingerprinting, required baselines and full binding-contract validation add overhead relative to v2's basic open-binding checks. The raw artifact retains every repetition, work count, byte volume and scope check. These results establish neither an end-to-end completion latency nor constant latency, storage or production-scale capacity.

[`executions activate`](../python/README.md#activate-an-operational-indexed-ledger) performs explicit copy-only conversion or profile refresh into version 4. It preserves `raw/ledger.sqlite3`, creates `operational/ledger.sqlite3` and publishes a manifest after full source validation, output validation and durable publication. Dry-run validates the same source and destination plan without writing output. A required integrity mismatch is never resealed. Source history stays unchanged and the operator explicitly selects one canonical operational copy.

Explicit activation can refresh well-formed stale version-3/version-4 profiles from supported families and codecs after baseline verification and fresh semantic validation. Unknown families and codecs are rejected. Strict matching-profile version-3 audit remains unchanged; the source does not gain operational status or a new profile through a read.

Initial version-2/version-3 conversion rejects unresolved prepared work. An existing version-4 ledger may be explicitly copy-refreshed with prepared work after complete integrity and fresh semantic validation, preserving the exact intent, state path, proof and absent receipt. This operation makes no HTTP request, publishes no state and performs no completion recovery. After explicit cutover, normal recovery evaluates the actual current state under its existing locks and retains exact-before abort, exact-after finalization and third-state conflict behavior.

### Required integrity baseline and optional semantic reuse

Keep two distinct concepts:

1. A required integrity baseline covers the exact retained proof and dependency bytes for the indexed format, with exact row-set coverage.
2. An optional current-profile semantic result records that a terminal payload and its derived facts passed validation for that exact baseline and independently selected validator profile.

Missing or extra required entries, duplicate identities, unknown references, malformed baseline records and digest mismatches are integrity errors. Normal reads and full audit must not interpret them as cache misses, validate whatever bytes survive and silently replace the baseline.

Only an absent or deliberately invalidated semantic result, or a recognized stale version-4 profile whose required integrity baseline still matches, may trigger fresh semantic validation in read-only memory. A malformed same-profile result or inconsistent dependency/verdict association is an error. Ordinary reads never insert entries, change the baseline, reindex or upgrade a ledger. Unsupported semantics produce an explicit unsupported result. Recognized stale profiles block operational writes until explicit activation refreshes a copy; no normal mutation silently mixes profiles. Profile refresh checks baseline consistency and full fresh semantics and cannot repair or bless an integrity mismatch.

### Exact coverage and trusted profiles

The versioned transcript needs a domain separator, typed length framing, field names, stable order and explicit absent/null markers. It binds raw retained TEXT/BLOB bytes and every dependency that influences validation: binding contract, request and evaluation date, model, policies, admitted state and receipt, archive/report, before/after state, phase, completion receipt and any reusable derived summary.

Static facts extracted to avoid repeated decoding, such as before/after serials and state digests, belong to the bound semantic result. A mutable summary cannot become an unchecked source of authority. Exact coverage must include every required historical identity; checking only index rows that happen to exist is insufficient.

Normal validation processes proof rows individually and retains compact digest and semantic metadata, rather than collecting all historical payload bytes in memory. A writer captures exact native before-values only for the rows its operation will change, then checks the complete digest-level delta and those exact after-values before atomically maintaining the index. Memory still grows with roster, binding and fact metadata and the largest individual proof.

The expected profile comes from trusted installed code and its compatibility registry, never from a caller's assertion or a ledger's declaration that it is trusted. Changes to relevant validation semantics invalidate reuse. Profile identity, dependency closure and compatibility rules are code-owned; recognized stale profiles permit fresh read-only fallback but do not permit stale-profile operational writes.

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

After writing the index, the transaction verifies its planned write count, exact resulting index rows and the complete source baseline and global associations again before commit. An unexpected write during index maintenance must roll back together with the source mutation; its observed change count cannot grant it authorization.

Failed statements, commits or rollback attempts must not leave partial usable entries. Crash tests need to cover preparation, state publication, receipt finalization and index maintenance together. SQLite hot-journal recovery and completion publication recovery remain separate explicit procedures; an index never authorizes replay of an archived or stale candidate into current state.

Version 4 is obtained through explicit copy-only activation into a new private destination after full source validation. Source files and exact historical proof remain unchanged, including binding/request identities, before/after bytes and original receipts. Unsupported inputs and unresolved prepared intents block initial conversion from version 2 or 3. Existing version-4 refresh can preserve prepared work under the explicit validation and recovery sequence above.

Validate the complete output, synchronize its files and directory, then atomically publish the new copy without overwrite. The operator explicitly selects one canonical version-4 ledger; old and copied files must not be parallel active authorities. A version-3 audit-only copy is ineligible for this operational cutover. Ordinary commands never migrate, activate or recover implicitly. Version-4 backup and copy-only recovery retain complete proof and index and freshly audit all semantics; copying a successful cache record alone cannot establish a valid ledger.

## Audit, retention and claims

| Validation path in version 4 | What is checked | What can be reported |
| --- | --- | --- |
| Normal operation, matching terminal profile | Whole-history byte consistency, index coverage, fresh live/global invariants; matching terminal semantics reused | All retained bytes checked; named terminal semantics reused under the stated profile. |
| Matching integrity baseline, missing/stale semantic result | The same checks plus fresh semantic validation of the affected payload, without writing the index | Fresh semantics checked for the named payload/profile; no durable refresh claimed. |
| Explicit full audit | Required baseline consistency and every retained semantic transition, ignoring positive reuse entries | Full audit passed only when both byte consistency and fresh semantics pass. |
| Copy/upgrade/profile refresh/backup/recovery | Coherent source/copy, complete proof, required baseline, index associations and full semantic audit | A complete validated copy or explicit supported maintenance result. |

Version-4 binding-show diagnostics and the public index-validation facts identify profile, all-byte coverage and fresh/reused semantic work. Immutable binding records and historical completion receipts retain their existing formats, and no completion report claims that cached history has received a fresh full audit. Activation/version-4 audit reports use history schema `0.2.0`; ordinary version-4 maintenance uses `0.3.0`, while legacy report formats remain unchanged. Audit success cannot erase an existing baseline mismatch or rewrite a historical receipt.

The first retention policy is **keep all historical bytes indefinitely**. There is no archival, garbage collection, expiry or deletion authority. Storage and whole-history byte checking therefore continue growing; this proposal makes no constant-space or constant-latency claim. Index associations should use indexed lookups, and global membership checks should group identities without rescanning unrelated rows per pending key.

Matching bytes, profiles and cached facts do not independently prove who produced the positive result. A coherent rewrite of proof and index by the operator can fabricate a cached semantic verdict. A fresh full audit can expose invalid semantics, but does not authenticate evidence origin or test honesty. Neither the index nor the current unsigned receipt protects against a hostile same-user operator or proves that an old coherent whole-ledger copy is the latest global generation. Strong rollback detection would require an independent monotonic trust anchor outside the restored ledger.

## Acceptance and measurement

[`REQ-028`](../spec/exacly/ledger-history.yaml) defines the operational acceptance contract. Its dedicated suites cover exact-byte integrity, matching terminal reuse, read-only fallback, profile refresh, prepared work, transactional writes, exact offline replay and crash recovery. REQ-029 and its conversion/audit tests retain the separate audit-only foundation contract.

Operational measurements must distinguish normal indexed validation from full audit on the same retained history. Repeat independent-binding, supersession, queued-backlog and large-proof workloads, preserve source fixtures, and report installed profile, byte coverage, fresh/reused counts and elapsed time. Regression assertions use deterministic work counts and index coverage rather than machine-dependent timing thresholds. Raw hashing measurements above remain a component comparison and cannot stand in for an operational speedup measurement.

Keep archival as a separate decision when representative physical storage/I/O measurements or an approved retention policy justify it. The original plan/apply research remains unchanged; this decision and the product model carry the follow-up.

## References

- [`execution_store.validate`](../python/src/exacly/execution_store.py): current SQLite, binding, chain and watermark checks.
- [`completion_store`](../python/src/exacly/completion_store.py): retained semantic proof checks, exact state transitions and operation-scoped reuse.
- [`execution_maintenance`](../python/src/exacly/execution_maintenance.py): protected copy publication, backup, recovery and upgrade.
- [`execution_history`](../python/src/exacly/execution_history.py) and [`history_maintenance`](../python/src/exacly/history_maintenance.py): the audit-only integrity format, conversion and fresh semantic audit foundation.
- [`execution_index`](../python/src/exacly/execution_index.py): operational version-4 validation and transactional index maintenance.
- SQLite [`quick_check`](https://www.sqlite.org/pragma.html#pragma_quick_check), [`data_version`](https://www.sqlite.org/pragma.html#pragma_data_version) and [online backup](https://www.sqlite.org/backup.html) document storage checks; none establishes semantic evidence validity or a globally monotonic ledger identity.
