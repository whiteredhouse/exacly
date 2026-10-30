# Bounded local executor

The executor supervisor implements [REQ-027](../spec/exacly/automation.yaml): one reviewed verification task can run through an isolated local command with durable attempt, time and concurrency limits. See the [release notes](../CHANGELOG.md) for package availability. The first adapter is `local-bubblewrap-v1`, built on the existing [Linux Bubblewrap isolation contract](candidate-isolation.md#linux-bubblewrap-v1-backend-contract).

The adapter receives explicit input snapshots and returns collected output files. It has no host repository or network access and permits no repository effects. It does not create a branch or PR, push a commit, close a task or change verification state. To work on source, supply its reviewed bytes as inputs and have the command write its result under `/outputs`; applying that result to a repository remains a separately authorized operation.

The coordinator calls explicit attempt-storage, invocation-ownership and worker-lifecycle interfaces. Its default local adapters retain the policy and storage formats described here. Trusted Python applications can compose these interfaces through a checked backend bundle; see the [coordinator backend contracts](coordinator-interfaces.md) for their conformance, ownership and migration obligations. Protocol version `0.1.0` supports one local-machine coordination domain.

## Prepare one authoritative execution journal

Keep the execution-binding ledger, executor journal and output storage in protected operator directories outside Git working trees, model directories and candidate-accessible storage. The storage parents must be owned by the operator with POSIX mode `0700`; database files must be owned regular single-link files with mode `0600`. Candidate isolation supplies the enforced boundary; same-user file permissions alone do not isolate candidate code.

Use one canonical executor journal to account for attempts and concurrent work against the selected binding ledger. Initializing another journal is not a supported way to reset exhausted limits or recover uncertain work. Independent journals and machines do not establish a distributed execution guarantee.

```sh
EXACLY_EXECUTOR_DIR="$HOME/.local/state/exacly/executor"
mkdir -p "$EXACLY_EXECUTOR_DIR" "$EXACLY_EXECUTOR_DIR/outputs"
chmod 0700 "$EXACLY_EXECUTOR_DIR" "$EXACLY_EXECUTOR_DIR/outputs"
EXACLY_EXECUTOR_JOURNAL="$EXACLY_EXECUTOR_DIR/executor.sqlite3"

exacly executors init --journal "$EXACLY_EXECUTOR_JOURNAL" \
  --ledger "$EXACLY_LEDGER_FILE" --max-concurrency 1 --json
```

Initialization is explicit and refuses an existing destination. It fixes the journal's concurrency capacity and returns its unique initialization identity. Pin the canonical journal, binding ledger, actual state and output-root paths and the journal identity in the executor policy before reviewing execution. Executor attempt history is an explicit companion extension of private execution storage; existing version-2 and version-4 binding/completion databases retain their formats. The companion has SQLite application ID `0x45584541` (`EXEA`), schema version `1` and a UUID4 initialization identity.

## Review snapshots, runtime and limits

First prepare a saved verification plan and its exact readiness and producer trust policies, an input implementation target, the current model and actual state, and one active execution binding for its pending verification task. Dispatch can create and register that task through its [separate protocol](durable-dispatch.md); an existing task can instead be registered with `exacly executions register`.

The CLI reads the saved plan, policies, target, inputs and authorization as strict JSON. Model documents selected with `-m` may be YAML or JSON. Keep runtime and executor policies, input manifests and authorization files outside the recursively loaded model directory.

The executor rechecks the selected action, binding, approved criteria and observation bindings, input target, policies and actual current state before a new attempt. Changed contracts require fresh planning and registration. Work already satisfied or no longer pending does not gain execution authority from an old plan.

Prepare and independently review a runtime using the [isolation guide](candidate-isolation.md#prepare-a-trusted-runtime). Put the reviewed runtime policy inside the executor policy. The adapter policy is closed: it cannot add mounts, network access, arbitrary runtime providers or host execution as a fallback.

This example uses placeholder paths, identities and digests. Replace them with the exact canonical storage values and independently reviewed runtime identity:

```json
{
  "schema_version": "0.1.0",
  "name": "product-local-verification",
  "version": "1",
  "adapter": "local-bubblewrap-v1",
  "storage": {
    "journal": "/absolute/operator/executor/executor.sqlite3",
    "journal_id": "00112233445566778899aabbccddeeff",
    "ledger": "/absolute/operator/ledger/ledger.sqlite3",
    "state": "/absolute/product-observations/current.json",
    "output_root": "/absolute/operator/executor/outputs"
  },
  "runtime": {
    "schema_version": "0.1.0",
    "backend": "linux-bubblewrap-v1",
    "rootfs": "/srv/exacly/runtimes/python-rootfs",
    "rootfs_digest": "sha256:<64 lowercase hexadecimal characters from inspection>",
    "python": "/usr/bin/python3",
    "bubblewrap": "/usr/bin/bwrap"
  },
  "argv": ["/usr/bin/python3", "-I", "-S", "/inputs/verify.py"],
  "expected_outputs": ["result.json"],
  "max_attempts": 2,
  "timeout_seconds": 30,
  "permitted_repository_effects": []
}
```

`max_attempts` is between 1 and 10. `timeout_seconds` is finite, greater than zero and at most 3,600 seconds. The supervisor's wall-clock budget covers the worker's staging, isolated execution and output collection. Initialization sets a shared concurrency capacity between 1 and 16. Policies cannot expand that capacity or grant repository effects.

Supply explicit UTF-8 snapshots through the same `0.1.0` input manifest used by `isolate`; the Python API also accepts binary snapshots. There is no implicit repository traversal. The supervisor adds approved task metadata at `/inputs/.exacly-task.json`, including the task, selected goal, criteria and binding identities, dependencies and expected observations. Callers cannot replace that reserved path. The combined snapshots and metadata must fit the launcher's 1,024-file and 8 MiB input limits. Candidate code receives these copied inputs, never the operator's policies, ledger, state handles or credentials.

```json
{
  "schema_version": "0.1.0",
  "files": {
    "verify.py": "from pathlib import Path\nPath('/outputs/result.json').write_text('{\"result\": \"example\"}\\n')\n"
  }
}
```

The candidate can read `/inputs` and write private `/outputs` and temporary filesystems according to the isolation contract. Logs and artifacts retain the launcher's bounded collection and safe-file rules. `expected_outputs` is a nonempty, sorted list of unique normalized relative paths beneath the collected artifact directory. Additional safely collected artifacts are included in the build identity. Candidate-supplied absolute host paths or revision claims grant no authority.

## Preview and authorize one attempt

Render the read-only proposal with the actual current state:

```sh
exacly execute local -m 'spec/product/*.yaml' \
  --plan approved-plan.json --action "$EXACLY_ACTION_ID" \
  --policy trusted/verification.json --trust-policy trusted/producer.json \
  --executor-policy trusted/executor.json --target target.json \
  --state observations/current.json --date 2026-10-30 \
  --binding "$EXACLY_BINDING_ID" --inputs approved-inputs.json \
  --dry-run --json > executor-preview.json
```

Dry-run is the default. It launches no worker or candidate and changes no state, binding, watermark, attempt or output destination. It returns the selected contract, execution identity, next attempt, current preconditions, intended output location and an `authorization_template`. A saved plan, readiness result or copied template is not approval.

Independently review that exact proposal and save its authorization object in a protected file. The closed authorization format contains `schema_version`, `execution_id`, `attempt`, `effect: "execute_local_snapshot"`, the policy's `storage` object, initialized `max_concurrency`, `contract_digest`, `history_head`, `state_digest`, `evaluation_date` and `output`. These fields pin the initialized journal and storage scope, selected execution contract, actual history head, next attempt ordinal, current state and output destination. Apply rejects a changed precondition; obtain and review a fresh preview instead of editing identities or limits to bypass it. Authorization digests identify reviewed content and are not signatures.

```sh
exacly execute local -m 'spec/product/*.yaml' \
  --plan approved-plan.json --action "$EXACLY_ACTION_ID" \
  --policy trusted/verification.json --trust-policy trusted/producer.json \
  --executor-policy trusted/executor.json --target target.json \
  --state observations/current.json --date 2026-10-30 \
  --binding "$EXACLY_BINDING_ID" --inputs approved-inputs.json \
  --journal "$EXACLY_EXECUTOR_JOURNAL" --ledger "$EXACLY_LEDGER_FILE" \
  --apply --authorization trusted/executor-authorization.json --json
```

Before launching, the supervisor durably reserves the attempt and shared concurrency slot. Its stable execution identity follows the pending task, selected action and adapter; changing a policy does not reset an existing execution's attempt history. Each attempt has its own retained identity and output destination. Workers sharing the canonical journal cannot exceed its capacity, repeat a successful attempt or consume one approval as permission for a later attempt.

The active supervisor invocation also holds an exclusive ownership lock on a private `.attempt-N.owner` file beside that attempt's output destination. Retain these files with the execution storage. They distinguish an active invocation from an idle API process that remains alive after returning an error; deleting a file or waiting for a time limit is not a way to release capacity.

Retries are explicit. After a failed attempt has been reconciled or terminated, preview the next ordinal and separately authorize it within the original budget. No background retry loop infers additional authority from failure. Exhausted budgets stop execution and report the reason.

## Inspect, cancel and recover

These commands operate from retained journal history and do not require the original model, policies or unchanged state. A model update must not prevent an operator from stopping or inspecting an existing attempt.

```sh
exacly executors show --journal "$EXACLY_EXECUTOR_JOURNAL" \
  --execution "$EXACLY_EXECUTION_ID" --json

exacly executors cancel --journal "$EXACLY_EXECUTOR_JOURNAL" \
  --execution "$EXACLY_EXECUTION_ID" --json

exacly executors recover --journal "$EXACLY_EXECUTOR_JOURNAL" \
  --execution "$EXACLY_EXECUTION_ID" --json
```

Cancellation and recovery reconcile or stop the retained attempt before releasing its concurrency slot. They do not start another attempt. A possible worker or candidate execution remains accounted for until termination is established; an uncertain process identity is not permission to signal a reused numeric PID or launch replacement work. Timeout and ordinary failure preserve the outcome and diagnostic references rather than erase consumed attempts.

While the supervisor invocation holds its ownership lock, `cancel` durably records the request and may return `cancellation_requested` while the supervisor stops its worker. This result does not claim immediate termination. `recover` reports an actively supervised attempt without launching or replacing it. Once the invocation releases its lock or dies, recovery can reconcile the retained worker and namespace identities even if a long-lived API process still exists. An absent or invalid ownership file leaves recovery unresolved. Inspect the execution again to obtain its terminal receipt; its concurrency slot remains occupied until termination is established.

If an interrupted SQLite write leaves a rollback sidecar, ordinary operations refuse it. Explicit journal-only repair preserves a protected backup before asking SQLite to roll back the interrupted transaction:

```sh
exacly executors recover-journal --journal "$EXACLY_EXECUTOR_JOURNAL" \
  --authorize-recovery --json
```

This repairs journal storage without launching candidate code, changing the model or state, admitting evidence or resetting attempts. Inspect the recovered execution and reconcile any existing attempt before considering another reviewed run. Retain the canonical history and backup; restoring an older journal is not evidence that execution never occurred. Lock waits are bounded; a timeout leaves work intact and reports an actionable retry error.

## Interpret outputs independently

A successful attempt retains a task reference, immutable host-derived build identity, bounded output references and execution outcome. The build identity is computed from the collected artifact paths, lengths and content digests; it identifies bytes rather than trusting a candidate's claimed commit or revision. Missing required outputs or ambiguous output identity prevent a successful result. Replaying an already successful request returns its retained receipt rather than launching the candidate again.

Candidate exit status, logs and artifact contents remain untrusted. The build identity does not authenticate tests, establish acceptance coverage or prove that a later repository revision was produced from these artifacts. Applying outputs, obtaining trusted producer evidence for the selected implementation, live [evidence admission](../python/README.md#admit-github-actions-evidence) and [verification completion](../python/README.md#complete-registered-verification-work) remain separate operations. The executor never changes verification assessments, approved criteria, completion receipts or pending work.

The local backend still relies on a trusted host kernel, runtime and operator. Attempt, wall-clock and concurrency limits complement its filesystem and syscall boundary; they do not claim cgroup containment, general resource-exhaustion protection, a remote execution service or coordination across independent journals.

`execute local` exits 0 for a ready preview or successful execution, 1 for retained work that is still running, exhausted limits or an unsuccessful outcome, and 2 for invalid input or operational errors. `executors show` exits 0 for valid retained history. Cancellation and recovery exit 1 while work is still running or cancellation is pending, and 0 when the retained outcome is terminal or no attempt has started. Initialization and successful journal repair exit 0. Candidate logs remain in the private bundle rather than being rendered directly to the terminal.

Real executor integration and interruption tests require the supported Linux runtime. The optional `mise run check-core` development subset omits local execution and ownership test modules and establishes neither REQ-025/027 verification nor the complete REQ-031 contract; its separate report never enters the verification gate. The [backend guide](coordinator-interfaces.md#conformance-and-physical-verification) distinguishes the controlled substitution cases from local backend coverage.

## Python API

`exacly.executor.Request` supplies the saved plan and action, readiness and trust policies, executor policy, input target, evaluation date, binding ID and explicit `Mapping[str, bytes]` inputs. `preview(documents, state_path, request)` returns the read-only proposal; `apply(journal, ledger, documents, state_path, request, authorization=...)` performs that reviewed attempt. Both accept `lock_timeout` and an optional trusted `backends=` composition; omission selects the supported local adapters.

`show(journal, execution_id)`, `cancel(journal, execution_id)` and `recover(journal, execution_id)` inspect or reconcile retained attempts independently of current model inputs. `recover_journal(journal, authorize=True)` is the separate storage-repair operation. These functions also accept the trusted backend composition for the same canonical journal. Reports describe local execution history and carry no evidence or completion authority.
