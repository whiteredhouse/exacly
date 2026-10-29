# Ledger validation benchmark

From the repository root, with the development dependencies installed:

```sh
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_validation.py \
  --counts 1 10 30 --repeats 3
```

The script measures the public completion `show`, exact offline replay and exact-after-state recovery paths. It emits JSON with median elapsed seconds, proof-derivation counts for every repetition, and ledger size. Fixture setup and restoration are outside the timed operation; all evidence comes from local test fixtures.

Each fixture starts with a normal completion against controlled GitHub/JUnit test data and retains its admission proof. A history of size N contains one selected committed intent for show/replay, or one prepared intent after state publication for recovery, plus N−1 distinct aborted attempts for the same binding with recomputed intent metadata. The builder validates the entire ledger before measurement. These synthetic histories isolate proof revalidation using one binding and a small report and model; many independent bindings or larger payloads will change the timings.

Elapsed time is diagnostic. The regression suite in [`test_completion_validation.py`](../python/tests/test_completion_validation.py) checks deterministic proof-derivation counts instead of machine-dependent timing thresholds, and checks that a later operation rejects corrupt unrelated history. These historical measurements concern version-2 ledgers, whose fresh protected views still interpret all retained proof. Operational version 4 can reuse eligible terminal semantics while checking retained bytes and current global invariants; its separate comparison appears below.

## Example measurement

Measured on 2026-10-28 in the same managed workspace with Python 3.12.14 and `--counts 1 10 30 --repeats 1`. The baseline is commit `9b78e284fa89288d0874f162460aad6d325ab572`; the after column uses the operation-scoped validation change in PR #58. Times are seconds, rounded to three decimal places.

| Retained intents | Operation | Before | After | Proof derivations before | Proof derivations after |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | show | 0.063 | 0.035 | 2 | 1 |
| 1 | replay | 0.254 | 0.078 | 6 | 1 |
| 1 | recover | 0.343 | 0.108 | 10 | 3 |
| 10 | show | 0.670 | 0.305 | 20 | 10 |
| 10 | replay | 2.093 | 0.375 | 60 | 10 |
| 10 | recover | 3.291 | 1.260 | 100 | 30 |
| 30 | show | 2.178 | 0.900 | 60 | 30 |
| 30 | replay | 5.933 | 0.953 | 180 | 30 |
| 30 | recover | 10.210 | 3.045 | 300 | 90 |

Show and exact replay now derive each retained proof once. Exact-after-state recovery needs three full validations: the initial read, the current journal after acquiring commit-time locks, and the changed journal after the receipt commits. Those separate views preserve fresh validation across lock boundaries and mutations.

## Independent history and payload scaling

[`ledger_history.py`](ledger_history.py) profiles full store validation using independently identified completed tasks, open bindings and supersession chains. It also measures model/state/schema work and separate retained-payload read/SHA256 and SQLite `quick_check` passes. The [design decision](../docs/ledger-history-scaling.md) interprets these measurements and specifies the implemented validation-index contract. The operational comparison below measures that implementation separately from this historical baseline.

Run from the repository root with development dependencies installed. The six commands below reproduce the workloads in [`results/ledger_history_baseline.json`](results/ledger_history_baseline.json):

```sh
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history.py \
  --bindings-only --counts 10 100 300 1000 3000 --repeats 3
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history.py \
  --bindings-only --supersession-depth 3 --counts 100 300 1000 --repeats 3
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history.py \
  --counts 10 100 300
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history.py \
  --backlog --counts 10 100 300
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history.py \
  --counts 10 --cases 100 --requirements 10
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history.py \
  --counts 10 --cases 1000 --requirements 100
```

Each invocation emits its parameters, runtime/source metadata, byte sizes, medians and individual repetitions as JSON. The committed artifact groups these outputs under workload labels. Completion scenarios use one repetition; binding-only scenarios use three. Timings are diagnostic warm-cache measurements without machine-dependent pass thresholds. A 300-task run or large proof can take tens of seconds, including excluded fixture setup.

The fixtures use distinct task, pending, binding, completion, run, artifact, job, report and archive identities, while sharing an approved model, selected Requirement goal and output target. They begin with a normal completion against controlled GitHub/JUnit fixtures. Subsequent history is constructed directly with recomputed canonical associations to avoid timing repeated writes; every generated binding and complete ledger is validated before measurement. Backlog mode queues all tasks initially and preserves exact before/after state continuity. Sequential mode inserts one new pending task through an intervening serial increment. Larger reports contain real passing testcases with matching observation bindings across the declared model Requirements.

Component timings are inclusive: completion-history time includes payload semantics and proof loading; payload semantics includes model loading, state-schema decoding, binding-schema validation, archive unpacking and observation derivation. Do not sum nested timers. `base_and_sqlite_seconds` excludes the completion-history callback. The preserved historical artifact's group-comparison field describes the old repeated membership scan. Current `ledger_history.py` labels that analytical `pending identities × binding rows` value `legacy_binding_group_comparisons`; `current_binding_membership_rows` records the rows visited by the replacement grouping pass. Neither is an instrumented timing counter or a bound on the whole validator's cost.

The read/SHA256 pass covers the listed retained document/BLOB columns and binding/receipt JSON. It does not validate SQL associations, implement the proposed framed transcript, interpret criteria or establish authority; it is not an index prototype or a measured index speedup. These workloads do not measure cold-storage performance, live GitHub throughput or archival behavior. The fixture tests in [`test_ledger_history_benchmark.py`](../python/tests/test_ledger_history_benchmark.py) check distinct identities, state continuity, supersession shape, proof corruption rejection and deterministic measurement counts.

## Operational semantic reuse

[`ledger_history_reuse.py`](ledger_history_reuse.py) compares three validation engines against the same exact retained history: version-2 full semantic validation, operational version-4 normal validation, and version-4 full semantic audit. The default matrix contains all 16 workload dimensions above. Run it after freezing production source files, because the installed profile includes their exact content:

```sh
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_history_reuse.py \
  --out benchmarks/results/ledger_history_reuse.json
```

Use `--workloads completed-sequential-small --counts 10 --repeats 3` for a selected workload with additional repetitions. The default uses one repetition per workload and checkpoints complete workload results after each iteration. `complete: true` marks the finished artifact. These are diagnostic measurements from one workspace with warm filesystem caches, without a cold-storage or production-capacity claim.

Each fixture receives full validation before measurement. Fixtures that originally produce version-1 open-binding ledgers are explicitly upgraded to version 2 before this comparison. Their earlier version-1 metadata-only timings remain historical context and are not the new baseline. Both current engines share linear grouping and the compiled binding-schema helper, but they perform different amounts of schema work: version 4 freshly validates every binding; version 2 reaches full binding schemas through retained payload interpretation. Open version-2 bindings receive metadata checks without those full schema calls. Actual schema-call counts remain in the artifact. The comparison measures the complete implemented paths; improvements over the historical artifact must not be attributed solely to reuse when shared grouping and schema setup also changed.

Copy-only activation produces the version-4 copy, and independent typed-cell comparisons verify that all original SQL values match. Fixture setup, version-1 upgrade, activation, directory locks and connection setup are outside the validation timers; the artifact reports upgrade and activation durations separately. Each timed engine runs within a protected coherent read transaction. Normal and audit timings include their actual installed-profile computation, all required historical byte checks and fresh global/binding checks. Component timings are inclusive and must not be summed.

The artifact records returned fresh/reused/prepared counts, observed semantic and observation calls, checked byte/cell/row volume, profile identity, per-repetition timings and preserved file hashes. Source-volume counters cover the original retained typed cells. Index metadata and installed-profile reads are additional work included in elapsed time; these counters are not measured total physical I/O. Normal version 4 can avoid terminal interpretation but still has format, profile, byte, metadata and fresh binding-schema costs. Workloads with no terminal proof may regress; those rows remain in the comparison. Full audit intentionally re-derives terminal semantics and can cost more than version 2 because it also checks the required baseline and index associations.

[`test_history_reuse_benchmark.py`](../python/tests/test_history_reuse_benchmark.py) checks actual reuse/fresh-audit counts and byte coverage, explicit version-1 upgrade, published-matrix coverage, fresh prepared-intent interpretation and rejection of changed terminal bytes. Benchmark diagnostics never grant completion authority, dispatch work or close external tasks.

### Measured comparison, 2026-10-29

All 16 published workload dimensions ran once under the same frozen implementation and runtime. The [complete raw artifact](results/ledger_history_reuse.json) contains exact parameters, per-mode repetitions, observed work, source-volume checks and profile identity. Times below are seconds, rounded to three decimal places; these measure validation engines rather than whole-command latency.

| Workload | Tasks | Bindings | v2 full | v4 normal | v4 full audit |
| --- | ---: | ---: | ---: | ---: | ---: |
| Open bindings | 10 | 10 | 0.004 | 0.097 | 0.091 |
| Open bindings | 100 | 100 | 0.015 | 0.137 | 0.140 |
| Open bindings | 300 | 300 | 0.030 | 0.263 | 0.260 |
| Open bindings | 1,000 | 1,000 | 0.102 | 0.626 | 0.638 |
| Open bindings | 3,000 | 3,000 | 0.273 | 1.739 | 1.835 |
| Open tasks, depth 3 | 100 | 300 | 0.032 | 0.264 | 0.277 |
| Open tasks, depth 3 | 300 | 900 | 0.097 | 0.572 | 0.550 |
| Open tasks, depth 3 | 1,000 | 3,000 | 0.289 | 1.423 | 1.532 |
| Sequential completions | 10 | 10 | 0.192 | 0.083 | 0.318 |
| Sequential completions | 100 | 100 | 2.033 | 0.163 | 2.091 |
| Sequential completions | 300 | 300 | 5.970 | 0.367 | 6.073 |
| Initially queued completions | 10 | 10 | 0.219 | 0.093 | 0.317 |
| Initially queued completions | 100 | 100 | 2.886 | 0.171 | 2.992 |
| Initially queued completions | 300 | 300 | 15.711 | 0.423 | 16.229 |
| Completions, 100 cases / 10 Requirements | 10 | 10 | 0.428 | 0.087 | 0.527 |
| Completions, 1,000 cases / 100 Requirements | 10 | 10 | 3.041 | 0.093 | 3.265 |

Every completion workload reported zero fresh terminal interpretations and N reused terminal results during normal validation, versus N fresh interpretations and zero reuse during full audit. Prepared counts were zero in this matrix; the focused tests separately verify that unresolved prepared payloads are freshly interpreted in both paths.

Installed-profile computation remained inside the timed indexed operations and took 0.071–0.089 seconds once per normal check. Open-binding workloads have no terminal interpretation to avoid: the 3,000-binding normal check took 1.739 seconds versus 0.273 for version 2, including 1.098 seconds of fresh binding-schema work. The 300-task backlog case took 0.423 seconds normally and 16.229 for full audit, while checking the same 18.580 MB of original typed cells. Its SQLite file grew from 20.226 MB to 21.254 MB after activation. These samples show the implemented tradeoff; they do not establish constant latency, bounded storage or production capacity.
