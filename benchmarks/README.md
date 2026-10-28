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
