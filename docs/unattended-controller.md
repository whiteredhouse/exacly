# Local unattended verification controller

`exacly.automation_controller.NativeWorkflow` connects the existing dispatch, executor, local Git
handoff, remote publication, producer selection, output association and completion APIs. It is a
trusted Python integration, with a durable controller journal and an independently selected standing
authorizer. A saved plan, candidate file or configuration digest grants no authority.

The supported compiler profile is `single-producer-local-v1`: one verification requirement, one
required producer and one local coordination domain. The pilot uses one dedicated coordinator job
and protected storage on the same runner. Process recovery on that runner is supported. Runner loss,
a new boot, new-runner recovery, distributed ownership, additional targets and native
composition-to-consumer completion mapping are unsupported. Unknown component fields and additional
required members are rejected before effects.

## Bootstrap and approval

Keep journals, state, policy originals, source-object storage, output storage and credentials outside
worker authority. Use canonical private directories and distinct component journal parents. Keep the
remote operation signing key in its own private directory, separate from the journals and Git
repository. Archive reviewable configuration, identities and receipts; do not export raw keys or
tokens.

Bootstrap in this order:

1. Load the independently selected immutable model, producer and observer definitions, original
   source target and verification readiness policy. Produce an honest native refresh and plan.
   Missing observations remain unchecked; bootstrap does not manufacture passing evidence or pending
   work. Prepare the fixed worker snapshot from the exact approved repository object and model.
2. Establish the real resource guard and configure the executor policy with its exact descriptor.
   Initialize the native dispatch, executor, handoff, remote, association, accounting and revocation
   stores and any publication signing key. Retain their actual initialized identities.
3. Initialize a ledger anchor with `controller_store.initialize_ledger_anchor(anchor_path,
   ledger=ledger_path, create_ledger=True)` when the ledger is new. For an existing supported ledger,
   omit `create_ledger`. This creates no pending work and performs no migration.
4. Preallocate the controller's canonical path and UUID. Construct the closed components document,
   including that identity, the ledger anchor, exact native stores, policies, permitted destination,
   producer contract, resource envelope, deadlines and costs. Its `configuration_digest` hashes all
   component fields except that one digest field. This exclusion avoids self-reference; it is not an
   approval mechanism.
5. Construct the closed delegation with that exact `native_configuration_digest`, the original
   objective, protected and permitted paths, installed adapters, canonical accounting and revocation
   identities, finite budgets and explicit recovery permissions. Build the workflow contract using
   `native_contract(...)` and the matching authorizer identity. Initialize the controller at its
   preallocated path and UUID. The concrete pilot can retain these resources while it waits for
   bounded independent approval on the same runner.
6. Obtain independent approval of the actual delegation and configuration. Supply the approved
   delegation digest, issuer and approval identifier independently to `Authorizer`; deriving a digest
   from candidate content does not approve it. Install only trusted, explicitly selected Python
   services and reader objects. Compare `authority.identity()` with the workflow's retained identity
   before scheduling.

The legacy native executions ledger has no initialized UUID. Its explicit companion anchor pins a
canonical ledger path, private file, device and inode, supported schema, and its own initialized
identity and digest. `check_ledger_anchor` validates the actual ledger before native operations and
absence proofs. Replacing the ledger or anchor does not initialize a fresh workflow. This is a
cooperating local-user boundary, not protection against a privileged owner rewriting both resources.

Use `initial_binding_id=None` for a fresh consumer. The first phase then performs approved native
dispatch and registration; its authoritative receipt establishes the original binding correlation.
No manual pending-state edit or invented binding ID is required. An already registered task may
supply its original binding ID and start at execution. The ledger anchor remains the completion
store identity in both cases; binding identity is a separate correlation.

## Configuration and trusted services

The workflow contract pins the objective and original goal, original input target, model, criteria,
observation bindings, readiness and trust policies, plan, canonical component stores, selected
authorizer, host boot/run/job, deadline and polling bounds. Generated output targets never replace
the original goal, plan or input identity.

The components document requires `schema_version: "0.1.0"`,
`profile: single-producer-local-v1`, `required_members: [producer]`, `controller`,
`configuration_path`, `configuration_digest`, `date`, `state`, `ledger`, `ledger_anchor`,
`initial_binding_id`, `executor_policy`, `handoff_policy`, `handoff`, `remote`, `association`,
`producer`, `native_stores` and `costs`. Optional fields are `dispatch_policy`, `control_bounds_ms`,
`producer_timeout_ms`, `publisher`, `authority_preflight`, `baseline` and `negatives`. The compiler
does not ignore declarations of unsupported composition or distributed requirements.

Construct `NativeServices` with a trusted admission `Source`, remote transport, prepared worker,
resource guard, producer selector and optional native dispatch client. The default boundary is
`controller_runtime.bounded_native_call`. Candidate data and policy documents cannot import or
select another provider. A typical integration constructs:

```python
workflow = NativeWorkflow(
    contract=contract,
    documents=approved_documents,
    plan=approved_plan,
    readiness_policy=readiness,
    trust_policy=trust,
    components=components,
    services=trusted_services,
)
authority = Authorizer(
    delegation_path,
    approved_digest=independently_approved_digest,
    issuer=approved_issuer,
    approval_id=approved_identifier,
    effects=workflow.effect_registry(),
    readers={**workflow.reader_registry(), **trusted_runtime_readers},
)
```

The six native effect adapters derive actual current native grants and receipts. They expose their
recomputed configuration digest, which the standing authorizer compares with its independent pin
before inspection, invocation, recovery, prepared resume and cancellation. Rehashing changed
components cannot replace that approval. Store identity checks use the actual native journal before
claiming an operation is absent. Repository and destination facts describe the actual request;
cross-repository configuration is rejected by this profile.

Optional publisher metadata pins the environment selector, availability, checked repository scope,
App and installation IDs, expiry and protected workflow mint evidence. It explicitly states that
the issuer identity is not server-attested by this metadata. Human approval must review that
evidence. The concrete transport authenticates the same publishing token against the exact single
approved repository. A job-token identity cannot provide producer-triggering publication authority.

The optional closed `authority_preflight` contract pins the coordinator run, attempt, job, boot,
nonce, workflow and revision; consumer repository; producer and resource guard digests; publisher
descriptor; approval channel; 45-second whole-call ceiling; and a maximum of 20 producer reads.
Its trusted runtime reader uses the separate `authority-preflight` standing scope. Repeated approval,
fence or credential-scope network checks after approval must run through that metered reader rather
than unmetered object loading. The controller validates this descriptor; the pilot runtime supplies
the actual reader. The native producer reader counts committed `producer-observe` slots in the
original objective's authoritative accounting history, including uncertain calls across restarts.
Excess calls may retain conservatively charged denied slots; they cannot make a twenty-first
producer request.

## Scheduling, recovery and cancellation

Call `step(journal, journal_id, workflow=workflow, authority=authority)` from a bounded scheduler. A
step advances at most one native effect. Duplicate invocations coalesce under workflow ownership.
The phase order is dispatch, execution, handoff, publication, producer selection and polling, output
association, fresh completion, and reobservation. For existing tasks, dispatch is omitted.

Each new phase persists the exact request, stable effect identity and correlations before authority
is released. Native component journals remain authoritative for their effects. There is no atomic
transaction spanning the controller, authorizer and component stores. Controller `released` and
`native_released` records describe the request release boundary, not a successful effect.

An unfinished request is reconciled before any new request. Only exact authoritative absence or
explicitly supported prepared recovery can permit the same retained request and effect identity to
proceed under current authority. An ambiguous remote outcome remains uncertain. A changed workflow,
policy version or journal alias cannot reset the objective budget or forget a live supervisor.
`recover(...)` is discovery-only: it does not invoke or resume an effect. `resume(...)` explicitly
resumes a paused workflow, but stale contracts still require independent review and replanning.

Before each new effect the workflow reassesses the current native plan, state, active binding,
criteria, policies and delegated authority. The planner selects evidence collection, failure
investigation or configuration/model escalation. Missing evidence does not imply missing
implementation. The fixed approved recipe may run only for its approved action classification; a
changed classification pauses for a reviewed plan and recipe. Configuration or model escalation does
not edit the verifier. Already-satisfied native work is durably superseded without dispatch or
execution, preserving its history.

The authorizer owns all objective-wide budgets. Every actual producer poll, preflight or verification
read and every bounded inspect/recovery/cancellation call reserves time in that same accounting
store. Prepared resume and repeated invocation consume new time reservations. Reusing caller IDs,
changing revisions or superseding bindings refunds nothing. The controller records accounting
references instead of maintaining resettable budget counters. Deadlines are refreshed after blocking
calls and immediately before new release; backoff starts from the completed call.

`pause(..., reason=...)` and `cancel(..., reason=...)` persist their state independently of an active
step. Cancellation before delegated release prevents the call; native gates also veto a cancelled
request before a new native start. Cancellation after actual release preserves in-flight
uncertainty and prevents later phases. To request supported cancellation of an existing attempt,
pass the selected `authority` to `cancel`. The authorizer requires the exact retained grant,
separate cancellation permission and a fresh time reservation. An absent intent or controller veto
is not a manufactured native cancellation receipt. Revocation and expiry prevent new invocation;
historical recovery uses only its separately approved finite permissions.

The default physical boundary requires Linux, single-threaded invocation, pidfds, readable process
ownership information and subreaper support. Its independently owned watchdog establishes exact
process ownership before opening the effect gate. It observes parent death and an absolute deadline,
then terminates and reaps owned or adopted descendants, including detached subprocesses. Kernel
alarms and bounded nonblocking result publication prevent a stopped controller from extending the
call indefinitely. Supervisor ownership and cleanup receipts remain in the controller journal;
unproved cleanup blocks new absence or retry authority. Setup failure before the gate produces no
effect. A boundary timeout gives an uncertain outcome that must be reconciled with native history.
The separate resource guard establishes real CPU, memory, swap and process ceilings; a timeout alone
does not establish those limits.

## Evidence and completion

Producer discovery selects and retains one exact approved repository/workflow/revision/run/attempt
before polling it. Only attempt one is supported in this profile; a rerun needs a separately reviewed
contract. Missing, ambiguous, interrupted or failed producers cannot become a latest-green fallback.

Before association and completion, a bounded actual transport read must still match the retained
publication and exact handoff commit. An external ref move pauses the workflow and preserves the
conflict. Output association uses the exact native published handoff, original binding and current
state; it does not admit evidence or grant completion. Only fresh native admission and conditional
completion may change pending work. Completion must then be followed by native current-state
reobservation. Worker exit, publication, a selected run, admitted failure, raw JUnit or a controller
status cannot establish verification.

Optional `baseline-admission` reads authenticate a literal independently approved original-input
run and attempt through native reconciliation preview. They retain actual admitted archive/report
bytes and distinguish an authentic failed producer from missing evidence while preserving pending
state. They never invoke completion.

Optional `negative-admission` reads have their own closed contract, adapter and budget scope. They
run only for the retained published target and selected producer before association. Each fresh
read first admits the actual selected producer, then asks native admission to evaluate one declared
unavailable-run, wrong-revision, wrong-attempt, mismatching-model-digest or
mismatching-observer-digest request. Results retain native receipts, actual control artifact bytes,
state digests and explicit probe semantics. Digest-mismatch probes do not demonstrate a deployed
verifier edit. These read-only probes neither mutate a remote ref nor demonstrate another actor's
live ref drift. Such a live mutation requires its own approved effect and evidence.

## Operational limits and evidence

Inspect `show(...)` for phase, retained pending request, reason, native correlations, accounting
references, producer selection and supervisor cleanup. Preserve all authoritative stores and the
publication key during process restart. On missing credentials, unavailable infrastructure, failed
CI, budget exhaustion, stale configuration, unknown ownership or conflicting state, stop scheduling
new effects and review the retained escalation. Do not replace a journal or ledger to clear it.
Keep the coordinator's service lifetime, host resource unit and scheduling deadline finite.

The controlled tests in `test_unattended_controller.py`, `test_unattended_controller_faults.py` and
`test_unattended_completion.py` cover durable scheduling and release races, real process-supervisor
failures, anchored fresh dispatch, native Git handoff, controlled producer admission and conditional
completion. Fixtures that replace the worker, resource facts, source or transport establish no
physical isolation or live GitHub claim. The opt-in pilot must independently retain its actual
resource/runtime, publishing, producer, negative-case, restart and completion evidence. Verification
completion grants no automatic merge, release or deployment authority.
