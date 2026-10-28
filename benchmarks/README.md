# Ledger validation benchmark

From the repository root, with the development dependencies installed:

```sh
PYTHONPATH=python/src:python/tests uv run --locked python benchmarks/ledger_validation.py \
  --counts 1 10 30 --repeats 3
```

The script measures the public completion `show`, exact offline replay and exact-after-state recovery paths. It emits JSON with median elapsed seconds, proof-derivation counts for every repetition, and ledger size. Fixture setup and restoration are outside the timed operation; all evidence comes from local test fixtures.

Each fixture starts with a normal completion against controlled GitHub/JUnit test data and retains its admission proof. A history of size N contains one selected committed intent for show/replay, or one prepared intent after state publication for recovery, plus N−1 distinct aborted attempts for the same binding with recomputed intent metadata. The builder validates the entire ledger before measurement. These synthetic histories isolate proof revalidation using one binding and a small report and model; many independent bindings or larger payloads will change the timings.

Elapsed time is diagnostic. The regression suite in [`test_completion_validation.py`](../python/tests/test_completion_validation.py) checks deterministic proof-derivation counts instead of machine-dependent timing thresholds, and checks that a later operation rejects corrupt unrelated history. Every fresh protected database view still validates all retained history, so validation cost continues to grow with history size.

## Example measurement

Measured on 2026-10-28 in the same managed workspace with Python 3.12.14 and `--counts 1 10 30 --repeats 1`. The baseline is commit `9b78e284fa89288d0874f162460aad6d325ab572`; the after column uses this change. Times are seconds, rounded to three decimal places.

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

[`ledger_history.py`](ledger_history.py) profiles full store validation using independently identified completed tasks, open bindings and supersession chains. It also measures model/state/schema work and separate retained-payload read/SHA256 and SQLite `quick_check` passes. The [design decision](../docs/ledger-history-scaling.md) interprets these measurements and specifies the proposed validation-index contract.

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

Component timings are inclusive: completion-history time includes payload semantics and proof loading; payload semantics includes model loading, state-schema decoding, binding-schema validation, archive unpacking and observation derivation. Do not sum nested timers. `base_and_sqlite_seconds` excludes the completion-history callback. The group-comparison field is the analytical `pending identities × binding rows` count implied by the current membership algorithm, not an instrumented counter.

The read/SHA256 pass covers the listed retained document/BLOB columns and binding/receipt JSON. It does not validate SQL associations, implement the proposed framed transcript, interpret criteria or establish authority; it is not an index prototype or a measured index speedup. These workloads do not measure cold-storage performance, live GitHub throughput or archival behavior. The fixture tests in [`test_ledger_history_benchmark.py`](../python/tests/test_ledger_history_benchmark.py) check distinct identities, state continuity, supersession shape, proof corruption rejection and deterministic measurement counts.
