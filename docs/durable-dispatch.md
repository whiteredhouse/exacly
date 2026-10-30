# Durable GitHub task dispatch

Exacly dispatches one explicitly approved verification action to a GitHub issue and associates the confirmed task with local pending verification work. Its contract is [REQ-026](../spec/exacly/automation.yaml). Dispatch creates work; independent evidence admission and [local completion](../python/README.md#complete-registered-verification-work) still decide whether that work satisfies the approved goal.

This is unreleased functionality on main. The first adapter is `github-issue-v1`; it accepts one reviewed repository and issue title/body. It does not launch candidate code, assign an executor, close issues, merge pull requests, publish releases or grant completion authority. Bounded executor supervision remains the separate proposed REQ-027 contract.

## Choose canonical operator storage

Use one canonical dispatch journal for the coordinator and approved destination. Concurrent workers sharing it serialize durable dispatch transitions and registration through ordered directory locks. Separate journals or machines do not establish a distributed exactly-once guarantee. The operator must consistently select the same authoritative journal rather than start another one to retry ambiguous work.

Keep the dispatch journal and execution-binding ledger in protected operator storage outside Git working trees, model directories and candidate-accessible storage. Each storage parent must be owned by the operator with mode `0700`; the SQLite files must be owned, regular, single-link files with mode `0600`. [Candidate isolation](candidate-isolation.md) must prevent candidate processes from reaching these paths; same-user file permissions alone do not establish that boundary.

Explicitly create a new journal before reviewing the storage policy or applying a dispatch:

```sh
EXACLY_DISPATCH_DIR="$HOME/.local/state/exacly/dispatch"
mkdir -p "$EXACLY_DISPATCH_DIR"
chmod 0700 "$EXACLY_DISPATCH_DIR"
EXACLY_DISPATCH_JOURNAL="$EXACLY_DISPATCH_DIR/dispatch.sqlite3"
exacly dispatches init --journal "$EXACLY_DISPATCH_JOURNAL" --json
```

Initialization refuses an existing destination and returns its unique `journal_id`. Copy that exact initialization identity into the reviewed dispatch policy's `storage` object together with the canonical absolute journal, execution-ledger and actual state paths. The authorization mirrors those storage values. Apply checks the selected paths and initialized journal instance before remote effects; an authorization for this journal cannot be replayed against a freshly initialized replacement.

The dispatch journal has its own application ID `0x45584450` (`EXDP`) and SQLite schema version `1`. It is separate from the existing execution-binding ledger; dispatch does not migrate, upgrade or activate that ledger. The selected storage contract is retained with each intent so recovery cannot silently select another instance, ledger or state file. Preserve the canonical history when recovering; an earlier journal snapshot is not proof that a remote create did not happen.

## Review the action and effect

Prepare a saved verification plan, its exact readiness and producer trust policies, an explicit source target, the model config and the actual current state file. The plan, selected policies, target and authorization inputs are strict JSON; model documents selected with `-m` may be YAML or JSON. If the reviewed readiness policy is stored as YAML, supply an equivalent reviewed JSON object without changing its meaning or canonical policy digest. Dispatch rechecks the saved action, approved config, criteria, observation bindings, policies and target against current state. A retired or changed contract blocks dispatch. If the selected goal is already satisfied, apply retains supersession history without creating another task.

Keep the dispatch policy outside the recursively loaded model directory. After initialization, review the repository and storage contract together. This example uses placeholder repository and storage identities; replace them with the independently reviewed destination, canonical absolute paths and exact `journal_id` returned by initialization:

```json
{
  "schema_version": "0.1.0",
  "name": "product-verification-dispatch",
  "version": "1",
  "adapter": "github-issue-v1",
  "repository": {
    "full_name": "example/product",
    "id": 12345
  },
  "storage": {
    "journal": "/absolute/operator/dispatch/dispatch.sqlite3",
    "ledger": "/absolute/operator/ledger/ledger.sqlite3",
    "state": "/absolute/product-observations/current.json",
    "journal_id": "00112233445566778899aabbccddeeff"
  },
  "issue": {
    "title": "Verify the approved requirement",
    "body": "Collect the observations required by the reviewed action."
  },
  "max_attempts": 3
}
```

The adapter checks the live repository name and numeric ID. Settings come from this reviewed policy; there are no library defaults for a repository, workflow or test path. Labels, assignees and arbitrary GitHub request fields are not supported by this first adapter.

Render a dry run using the actual selected state destination:

```sh
exacly dispatch github-issues -m 'spec/product/*.yaml' \
  --plan approved-plan.json --action "$EXACLY_ACTION_ID" \
  --policy trusted/verification.json --trust-policy trusted/producer.json \
  --dispatch-policy trusted/dispatch.json --target target.json \
  --state observations/current.json --date 2026-10-30 --dry-run --json \
  > dispatch-preview.json
```

Dry-run is the default when `--apply` is omitted. It requires no GitHub token and creates no client, task, journal, binding, state update or watermark. The report includes the selected action and verification goal, target, storage contract, actual state preconditions, proposed effect and an `authorization_template`. Saving the preview or its template does not approve it.

Independently review the action, repository ID/name, canonical storage paths and journal instance, issue contents, policies and target. Then save the reviewed authorization object in a protected file. The closed authorization format contains `schema_version`, `dispatch_id`, `action_id`, `adapter`, `repository`, `storage`, `effect: "create_github_issue"`, `policy_digest` and `effect_digest`. Its `storage` object must exactly mirror the reviewed policy. Supply that exact object to apply; passing a saved plan or the whole preview report does not grant dispatch authority. Digests identify reviewed bytes and are not signatures or self-authenticating approval.

The authorization has this shape; replace every example identity and digest with the values from the independently reviewed preview, including its exact storage contract:

```json
{
  "schema_version": "0.1.0",
  "dispatch_id": "sha256:<64 lowercase hexadecimal characters>",
  "action_id": "sha256:<64 lowercase hexadecimal characters>",
  "adapter": "github-issue-v1",
  "repository": {"full_name": "example/product", "id": 12345},
  "storage": {
    "journal": "/absolute/operator/dispatch/dispatch.sqlite3",
    "ledger": "/absolute/operator/ledger/ledger.sqlite3",
    "state": "/absolute/product-observations/current.json",
    "journal_id": "00112233445566778899aabbccddeeff"
  },
  "effect": "create_github_issue",
  "policy_digest": "sha256:<64 lowercase hexadecimal characters>",
  "effect_digest": "sha256:<64 lowercase hexadecimal characters>"
}
```

The dispatch identity combines the namespace, stable action ID, adapter and immutable numeric repository ID. It excludes the mutable repository name, state serials, dates, diagnostic reasons and the dispatch-policy digest. The repository name remains pinned in policy, authorization and receipt checks: a reviewed rename keeps the same dispatch identity and conflicts with an older retained contract rather than authorizing another create. Refreshing observations or editing a policy therefore cannot silently give the same action and destination another create opportunity. Exacly appends a stable qualified marker to the approved issue body for later discovery.

## Apply and retain the association

For a remote operation, supply an approved GitHub token with permission to read the selected repository and create issues. The CLI uses the first nonblank `GH_TOKEN`, then `GITHUB_TOKEN`; keep it in the operator environment rather than policy or authorization JSON. Dry-run, initialization, inspection, supersession and local registered replay need no token. Apply explicitly selects the initialized journal and binding ledger:

```sh
exacly dispatch github-issues -m 'spec/product/*.yaml' \
  --plan approved-plan.json --action "$EXACLY_ACTION_ID" \
  --policy trusted/verification.json --trust-policy trusted/producer.json \
  --dispatch-policy trusted/dispatch.json --target target.json \
  --state observations/current.json --date 2026-10-30 --apply \
  --journal "$EXACLY_DISPATCH_JOURNAL" --ledger "$EXACLY_LEDGER_FILE" \
  --authorization trusted/dispatch-authorization.json --json
```

Apply persists the reviewed intent before any remote create request and durably records `creating` before sending the one POST. Discovery walks complete paginated issue results rather than relying on eventually consistent search. Incomplete discovery, redirects, invalid receipts, mismatched repository identity or multiple matching markers stop progression.

After confirming the issue, dispatch retains its immutable receipt, prepares the exact local state transition and associates one unambiguous verification pending entry with its execution binding. Registration preserves unrelated observations and pending work, records `dimension: verification`, and uses conditional state publication. Changed pending entries, conflicting bindings or changed current-state preconditions block registration rather than selecting another task. The retained protocol reconciles state and SQLite resources after interruption; they are not one atomic transaction.

The journal retains the initial intent, bounded attempts, remote receipt, registration preparation and final receipt or supersession. These are protected local records, not proof that an executor produced a revision or that tests honestly establish acceptance. Issue closure, an agent completion report and a merged PR leave verification pending until independently admitted evidence satisfies the separate completion contract.

## Resolve interruption without duplicating work

GitHub issue creation has no suitable idempotency key. A failed or timed-out request may already have created the issue. Once a create might have been sent, Exacly never sends another create for that dispatch identity: one discovered match can be reconciled, multiple matches report conflict, and zero matches remain ambiguous. Retrying discovery is safe; interpreting absence as permission to create again is not.

Inspect the retained dispatch:

```sh
exacly dispatches show --journal "$EXACLY_DISPATCH_JOURNAL" \
  --dispatch "$EXACLY_DISPATCH_ID" --json
```

Ordinary apply and show reject an interrupted SQLite rollback journal or unsupported WAL/SHM sidecars. Explicit recovery takes the same full reviewed inputs and authorization, so current contracts are checked again:

```sh
exacly dispatches recover -m 'spec/product/*.yaml' \
  --plan approved-plan.json --action "$EXACLY_ACTION_ID" \
  --policy trusted/verification.json --trust-policy trusted/producer.json \
  --dispatch-policy trusted/dispatch.json --target target.json \
  --state observations/current.json --date 2026-10-30 \
  --journal "$EXACLY_DISPATCH_JOURNAL" --ledger "$EXACLY_LEDGER_FILE" \
  --authorization trusted/dispatch-authorization.json --json
```

Before resolving a recognized interrupted rollback transaction, recovery preserves the exact database and rollback-journal bytes in a new private sibling `.dispatch-recovery-*` backup with a checksummed receipt and synchronization. It validates ownership, file type, header and journal protections before letting SQLite recover the transaction. Retain that backup for operator inspection. WAL and SHM recovery are unsupported; recovery never changes an execution ledger's format or selects another state destination.

The initially retained policy's `max_attempts` bounds remote reconciliation invocations, including those whose GET or discovery requests fail. Retrying or editing the policy cannot expand that dispatch's budget, and another journal cannot replay its storage-pinned authorization. Exhaustion stops further remote work; it does not erase a confirmed task or prevent purely local registration finalization. Inspect retained progress and use explicit supersession when abandoning unresolved work; supersession retains the history and never grants another create for the same identity.

Lock waiting is separately bounded by `--lock-timeout`, which accepts finite nonnegative seconds and defaults to 30. Each HTTP request has a 30-second timeout and a 2 MiB response limit; discovery permits at most 100 pages of 100 items and rejects incomplete results. The lock timeout is not a whole-command deadline. Issue titles are limited to 256 UTF-8 bytes and expanded bodies to 64 KiB, including the dispatch marker. Each dispatch retains at most 256 journal events, with an 8 MiB bound on each intent or event JSON payload.

When abandoning an unresolved dispatch, explicitly retain supersession instead of deleting its history:

```sh
exacly dispatches supersede --journal "$EXACLY_DISPATCH_JOURNAL" \
  --dispatch "$EXACLY_DISPATCH_ID" --reason "Replaced by separately reviewed work" \
  --authorize-supersession --json
```

Supersession does not close or delete a remote issue, remove pending work, supersede an execution binding or authorize a replacement task automatically. Review and authorize replacement work separately; resolve any ambiguous remote outcome before treating the old issue as absent.

## Reports and API

Text and JSON describe the same operation. For preview, apply, recovery and supersession, exit `0` reports a ready preview, registration or supersession; exit `1` reports an unresolved ambiguous, conflicting, blocked, missing or exhausted result; exit `2` reports invalid input or an operational failure. Show exits `0` for any valid retained phase, including ambiguous work, and `1` when the selected dispatch is missing. Reports remain unsigned and carry no evidence-admission or completion authority. JSON inputs are bounded and reject duplicate keys, nonfinite numbers and excessive nesting.

The Python API uses `exacly.dispatch.Request(plan, action_id, readiness_policy, trust_policy, dispatch_policy, target, date)`, where `target` is an explicit `Target`. `preview(documents, state_path, request)` is read-only. `apply(journal, ledger, documents, state_path, request, authorization=..., client=..., lock_timeout=30)` performs the reviewed effect or reconciles its retained progress. `recover(...)` uses the same inputs and explicitly permits protected rollback-journal recovery. `show(journal, dispatch_id)` inspects history, and `supersede(journal, dispatch_id, reason=..., authorize=True)` records explicit abandonment. `as_text(report)` renders the result.

The supported concurrency boundary is the canonical private journal and its ordered local locks. It does not claim remote transactions with GitHub, protection against a hostile operator, or candidate execution containment. The operator's journal, ledger, policy originals, credentials and authoritative state must remain outside candidate authority.
