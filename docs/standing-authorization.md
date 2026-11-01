# Standing authorization

`exacly.automation_authorization` evaluates an independently approved standing scope and invokes
exact native effects through trusted installed adapters. It builds on the objective accounting in
[`unattended-automation.md`](unattended-automation.md#objective-accounting). Scope validation,
authorization and accounting do not establish the physical worker boundary, live producer evidence
or completion of the full REQ-034–039 target.

This Python API supports one local coordination domain. Candidate code must have no access to its
configuration, accounting, revocations, coordination files or adapter objects. There is no provider
loader, approval-bypass flag or new CLI permission switch.

## Approval and initialization

Create the accounting and revocation stores explicitly in owner-private `0700` directories outside
Git. `standing_authorization.initialize(path)` returns a report containing `store_id`;
`automation_revocations.initialize(path)` returns the revocation store UUID. Retain those exact
identities and canonical paths. Configuration is a private `0600` JSON file selected by the trusted
operator. A copied template, filename or content hash cannot approve itself.

Construct the native components first, without an authorizer identity. Their checked content digest
becomes the required delegation `native_configuration_digest`. Independently review the complete
delegation, including that pin, the issuer and approval identity. Only then construct the authorizer
and a controller contract containing its identity. This ordering avoids making the approved
component digest depend circularly on the authorizer.

```python
from exacly.automation_authorization import Authorizer

# These values and installed objects come from trusted operator configuration.
# The digest and approval identity were selected independently of candidate inputs.
authority = Authorizer(
    protected_config_path,
    approved_digest=independently_approved_delegation_digest,
    issuer=approved_issuer,
    approval_id=approved_review_identity,
    effects=reviewed_native_effects,
    readers=reviewed_readers,
)
```

Construction checks the protected configuration and pinned stores, enrolls the original objective
with immutable limits, and initializes its derived private invocation mutex. It does not establish
that an external adapter or sandbox is usable. `identity()` exposes the pinned authorizer identity;
`check_available()` reports whether its protected delegation is current and not revoked.

The closed `0.1.0` delegation requires these fields. Unknown or missing fields stop authorization.

| Fields | Required scope |
| --- | --- |
| `schema_version`, `id`, `issuer`, `approval_id`, `config_path` | Exact delegation and independent approval selection, including its canonical configuration path |
| `accounting`, `revocations` | Canonical `path` and initialized `store_id` for each protected store |
| `objective` | Version, stable objective `id` and separate `original_goal` digest |
| `limits`, `not_before`, `expires_at` | Finite objective-wide budgets and timezone-aware lifetime |
| `contract` | Approved `model`, `criteria`, `bindings`, `readiness`, `trust` and `observer` digests |
| `native_configuration_digest` | Independently approved checked native component configuration |
| `repository`, `base_revision`, `actions` | Immutable repository ID/name, exact source commit and eligible original action IDs |
| `allowed_paths`, `protected_paths`, `destinations` | Explicit fixture paths, protected model/verifier/policy paths and exact permitted repository/ref destinations |
| `effects`, `reads` | Installed adapter identities, capabilities, native stores, resource ceilings and separately permitted recovery/read contracts |

The first scope confines worker paths to selected files beneath
`fixtures/unattended-pilot/inputs/` and `fixtures/unattended-pilot/results/`. Protected paths take
precedence. Native destinations must be explicitly selected nonproduction pilot branch refs in the
approved repository. Merge, release, deployment, destruction and model-service authority are excluded.

## Native effects and current facts

The closed native effect registry contains `dispatch`, `execute`, `handoff`, `publish`, `associate`
and `complete`, with their matching `native-…-v1` adapter IDs in `ADAPTERS`. Each configured effect
requires `adapter_id`, `capabilities`, `native_store`, `max_cost`, `cancel_inflight`,
`recover_after_revocation` and `recover_after_expiry`. The installed objects come from trusted
bootstrap; candidate JSON cannot supply another object or provider.

An `Effect` supplies `adapter_id`, a recomputed `native_configuration_digest`,
`bound_ms(method, request)`, `inspect(request)`, `invoke(request, authorization)` and
`recover(request, authorization)`. Explicit capabilities may additionally permit `resume` and
`cancel`. Its inspection derives current facts and the exact native grant from native APIs and
protected inputs. Facts pin the objective, original action, six approved contract digests, source,
paths, destination, canonical native store/operation, capabilities, costs and current preconditions.
The required `preconditions.native_configuration_digest` must match the independently approved pin.

The authorizer checks the installed object's actual configuration digest before native calls,
including historical recovery, prepared resume and cancellation. A retained grant's pin must also
match. A component document hashing its own changed marker refs, keys, timers or store identities
is insufficient. The integration must recompute its checked configuration rather than echo a
caller-supplied digest.

`execute(kind, request, effect_id=...)` reserves the exact grant and records uncertainty before
invocation. It rechecks applicability, current facts, lifetime and revocation before releasing a
new call. Native APIs still enforce their own state, active-binding and authorization checks.
The authorizer does not replace dispatch, executor, handoff or completion journals.

`reconcile(kind, request, effect_id=...)` discovers the retained native outcome. It does not launch
another invocation. Separately approved revocation handling may request native cancellation.
An exact authoritative journal proof may report `not_started`; an exact prepared native intent
may report `recoverable`. Empty remote discovery never establishes either proof. `execute` may
subsequently retry that same grant or resume its exact prepared intent only under its current
approved semantics and additional time reservation.

Preexisting native work without a retained standing decision requires review of its original
authority. Lost or conflicting native history cannot turn a settled decision into retry permission.

## Budgets, reads and the objective write fence

`attempts`, `wall_time_ms`, `model_requests` and `model_spend_microunits` are finite integer budgets
owned by the original objective's accounting store. The first contract requires both model-service
budgets to be zero. New plans, revisions, actions, policy files or workflow aliases cannot replenish
consumed or uncertain capacity. Reservations are never refunded, including failed or cancelled work.

The first invocation reserves at least its adapter's enforced whole-call maximum. Every inspection,
recovery, cancellation, prepared resume and repeated invocation reserves its own additional time.
Proof that a native operation never started does not prove that its earlier call spent no time.
A concrete adapter must bound the entire call, including nested requests, polling, backoff and
subprocesses. Independent per-request timeouts alone do not establish that ceiling. Only explicitly
approved purely local receipt reads may declare zero time; external reads require a positive bound.
CPU, memory and process containment remain the runtime's responsibility.

`call_metered(purpose, request, operation_id=...)` invokes a trusted read-only adapter selected by
`reads`. Supported purposes are `producer-observe` / `native-producer-observer-v1` and
`baseline-admission` / `native-baseline-admission-v1`. Their scopes independently pin the exact
read contract, repository, installed adapter and maximum time. Reader inspection is local-only and
returns the actual native configuration pin and enforced whole-call bound; these facts are checked
again immediately before the external read.

Every actual read receives a fresh authorizer-minted slot, atomically reserved and marked uncertain
before the request. Caller `operation_id` correlates work; repeating it never releases a cached grant
or presents an old result as fresh. Interrupted reads retain the full reservation. Reads share the
same objective accounting across processes and approved policy versions.

A separate checked private file mutex, derived from the canonical accounting store UUID and stable
objective, serializes native write admission and reconciliation across policy files. Under that
mutex, any other approved `reserved` or `uncertain` native write blocks a fresh write. A new action,
native journal or policy cannot forget a possibly sent request. The report identifies the exact old
operation and store to reconcile. If its original adapter is unavailable, escalate. Only its actual
native terminal outcome releases the fence. Uncertain read-meter slots do not block fresh pure reads.

The mutex does not hold the accounting directory lock across native calls. Its immutable identity
and private file permissions are checked, waits are bounded, and a missing mutex after native
authority is not silently recreated. Changing or deleting it is not recovery.

## Cancellation, revocation and reports

`automation_revocations.revoke(path, store_id=..., delegation_id=..., reason=...)` monotonically
records operator revocation. New grants and producer reads stop. Historical native discovery after
revocation or expiry requires the corresponding explicit recovery flag and remaining metered budget.
Revocation neither recalls an already sent request nor erases its reservation.

`request_cancel(kind, request, effect_id=...)` requires an exact existing native grant and explicit
inflight cancellation capability. It meters the discovery and cancellation calls. It deliberately
avoids the launch mutex so that an active owner can be stopped; it creates no new execution grant.
An absent native intent or a cancellation request alone cannot manufacture a terminal cancellation
or clear the fence. The controller must also retain its cancelled state and stop scheduling phases.

Reports contain `status`, `reason`, the reservation reference and any native `receipt`.
`completed`, `failed` and `cancelled` describe established native outcomes. `not_started` and
`recoverable` describe permitted discovery states. `denied`, `paused` and `uncertain` require the
caller to preserve history and handle the stated reason. Generic effect completion, worker exit or
a successful metered read does not establish verification; native fresh admission and completion
remain required.

Normal operations refuse unresolved SQLite sidecars, store replacements and unknown configuration.
Preserve those inputs and escalate rather than deleting history or selecting fresh stores. There is
no distributed coordination, cross-runner recovery, budget-reset migration or approval mechanism
for the final live pilot in this API.
