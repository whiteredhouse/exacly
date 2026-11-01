# REQ-039 fixed-verifier pilot approval and operations

This is the operational proposal for the selected nonproduction consumer,
`whiteredhouse/exacly` (GitHub repository ID `1411700094`), branch
`refs/heads/pilot/req039-consumer`. It does not grant authority. The complete
run-specific JSON packet must receive independent approval before live effects.
REQ-039 remains proposed until actual retained live acceptance passes.

## Fixed scope and producer

The worker is the reviewed deterministic `snapshot-inventory-v1` recipe. It reads
only `fixtures/unattended-pilot/inputs/source.txt` and may publish only
`fixtures/unattended-pilot/results/source-digests.json`. There is no coding-agent,
model-service, merge, release or deployment authority. Model requests/spend are zero.
The protected model is `fixtures/unattended-pilot/model.yaml`, namespace
`req039-consumer`, record `REQ-001`, verification dimension. Original goal identity
is stable across run IDs, policy versions, revisions and journal paths.

The separately installed producer is `.github/workflows/req039-evidence.yml`,
triggered only by `push` to `pilot/req039-consumer`. Its sole job is `observe`,
named `Observe REQ-039 fixture`, on `ubuntu-24.04`. It executes only the fixed
`verification/req039_inventory_test.py` observer, packages JUnit through native
Exacly evidence packaging and uploads
`exacly-req039-evidence-<run_id>-<run_attempt>`. The existing main-only
`.github/workflows/verification-evidence.yml` is unchanged.

The concrete immutable-base policy proposal is
[`req039-producer-policy.candidate.json`](req039-producer-policy.candidate.json).
It grants no trust. Use the current base and failed-producer identities in
[`automation-progress.md`](automation-progress.md), then compare the coordinator's
actual generated policy and delegation before independently approving its packet.
Per-run host, journal, resource and credential identities are finalized by that
protected setup; a copied policy or this document cannot approve them.

The exact producer definition is in `ci/req039-producer.json`. The approval packet
pins its workflow ID and bytes, model/criteria/bindings, observer closure, source
base, installed native components and every initialized store identity. The
observer closure includes `.github`, `ci`, `python`, `spec`, `tests`, `verification`,
`skills`, the fixture model, `conftest.py`, `mise.toml`, `pyproject.toml` and `uv.lock`.
No worker authority extends to those paths. A successful worker report alone
cannot admit evidence or complete work.

Only the dedicated pilot trust policy proposes `branch_protection: not_required`,
as selected by the user. Default admission behavior and repository rules are
unchanged. All remaining repository, event, branch, single-job, workflow, exact
revision/run/attempt, artifact, observer and model checks remain mandatory.
Receipts must report branch protection as not verified, without inventing a
verified protection identity. A changed verifier, criterion, binding, workflow,
policy or delegation requires a new independent approval.

## Publishing identity and exact permissions

Configure a separate GitHub App installation token for this repository only.
Mint with `repository_ids: [1411700094]` and these permissions:

- `metadata: read` (implicit GitHub App access).
- `contents: write` for the checked pilot branch update and exact operation-marker tag.
- `actions: read` for authentic producer, attempt, job and artifact discovery.
- `issues: write` for native dispatch task creation and reconciliation.

Mint only these repository permissions. GitHub's `contents: write` access is
repository-wide and also authorizes merge and release APIs; the mint request
cannot restrict it to pilot refs. Exacly's independently approved native grants
permit only the exact pilot branch and allocated operation marker, with no main
update, arbitrary ref, merge, release or deployment authority. Workers receive no
publishing credential. No additional workflows, actions-write, administration,
secrets or deployment permissions are needed. The external mint request is:

```json
{"repository_ids":[1411700094],"permissions":{"contents":"write","actions":"read","issues":"write"}}
```

The independent approver must verify the actual mint response/installation settings;
the packet explicitly does not server-attest issuer or permission metadata, and
its human approval body affirms the real permission configuration.
A general PAT is unsupported: the adapter requires the same publishing token's
authenticated `/installation/repositories` response to contain exactly the one
numeric repository identity. The source push uses atomic Git receive-pack
commands with explicit old/new branch identities and an absent-to-exact signed
operation marker. Ordinary unchecked REST ref updates are insufficient.

Set the token as Actions secret `EXACLY_REQ039_PUBLISH_TOKEN`. It must come from
a non-GitHub-Actions App: a job `GITHUB_TOKEN` push would not trigger the separate
push producer. Configure nonsecret repository variables
`EXACLY_REQ039_PUBLISH_APP_ID`, `EXACLY_REQ039_PUBLISH_INSTALLATION_ID`, and
`EXACLY_REQ039_PUBLISH_EXPIRES_AT` (UTC timestamp). These are trusted operator
mint metadata for independent review, not a claim that the token issuer was
server-attested. The token must remain valid throughout the bounded unit.
Do not assume this chat's GitHub connection is installed inside Actions.
Do not put token values or private App keys into approval comments, packets,
command arguments, artifacts or journal exports. Missing credentials block live
execution only; implementation and review can continue.

The coordinator's separate job token has only `contents: read`, `actions: read`,
`issues: read`, and `pull-requests: read`. It reads approval/history and producer
metadata. The workflow's pinned artifact action uploads control metadata outside
the service; the service never receives `ACTIONS_RUNTIME_TOKEN`. Candidate workers
receive neither credential.

Permitted remote destinations are the exact existing
`refs/heads/pilot/req039-consumer` and the individually allocated
`refs/tags/req039-operations/...` marker bound in each native grant. Markers are
nonversioned operation receipts; they confer no release authority. The separately allocated `refs/tags/req039-operations/<nonce>-precondition` name is used only
by the read-only expected-ref precondition probe and is never created. No force
overwrite, ref deletion, main update, repository-rule change or arbitrary tag is
authorized. Initial branch creation/base-object import occurs in trusted setup
with its own explicit review; runtime publication cannot silently import a base
or create a missing consumer branch.

## One bounded runner and independent approval

The coordinator is `.github/workflows/req039-coordinator.yml`, a single job
key/name `coordinate` on `ubuntu-24.04`, started manually on the final reviewed
main revision. Supply the full existing consumer base SHA; optionally supply
the exact initial failed producer run ID. Checkout uses full history and no
persisted credentials. The fixed initial branch intentionally has no result file,
so its producer provides authentic failed-CI evidence before the worker repairs
the fixture result. Never commit a manufactured successful result as setup.

The job has a 45-minute envelope for bounded host setup, the service and artifact retention.
The transient service has one CPU, 1 GiB memory, no swap, 128 tasks and an
absolute 1,800 second lifetime. Approval waits at most 900 seconds. Native calls
and producer polls have finite bounds; objective-wide accounting is authoritative
and retains uncertain reservations. The proposed accounting wall capacity is
160 conservative 45-second reservation units (7,200,000 ms); these are capacity
units, not a hard count of API requests. The 1,800-second physical unit limit
remains independent. The exact producer-read cap is 20 retained paid calls,
including uncertain calls across restart. The packet states the exact conservative
call/time reservations, deadline and physical cgroup identity. There is global
coordinator serialization with no cancellation of the current run.

Before approval, setup prepares private 0700 directories, 0600 control files,
sealed source, native journals, revocations, accounting, the native completion
ledger anchor and an independently keyed publication marker identity. Refresh
without evidence honestly records unchecked work; setup never edits pending work
or observed state by hand. The public review artifact contains only `packet.json`
and `approval-comment.json`. It identifies actual run, attempt, job, host boot,
store UUIDs/ledger inode anchor, exact policy/delegation/component digests and nonce.

An independent human reviews that complete packet, then posts the exact generated
approval body on PR #93's conversation. The configured approver is GitHub user
ID `1679024` (`p1c2u`). No agent or App can substitute its own approval. The
reader verifies repository/issue/author, original unedited body and no App origin
through authenticated REST plus a fixed read-only GraphQL query. A filename,
content hash, policy self-approval or edited comment is insufficient.

After human approval, the workflow retains the exact receipt as an immutable v4
artifact named `exacly-req039-approval-<run_id>-<run_attempt>`, with overwrite
disabled and 90-day retention. The service independently downloads and checks
server digest, bytes, exact run/attempt/job and successful upload step before
releasing any authority. A new runner cannot resume this original approved goal
by reconstructing journals. A narrowly authenticated earlier job that never executed the launch step can
be classified as never launched only when its immutable workflow bytes and
server step history prove it. Failed or uncertain launch still requires the
actual stopped-unit no-authority proof. Prior approved-goal fences, expired/deleted/unknown
history or ambiguous cleanup block a new coordination domain. The original
coordinator definition was rejected by GitHub because it used `runner.temp` in
job-level `env`. Only that exact historical workflow digest may be classified as
server-rejected, and only with fresh terminal failed first-attempt push metadata,
zero attempt jobs and a fully empty artifact history. This exception grants no
authority and does not excuse unknown workflow bytes or executed/rerun histories.
A prior run can
be classified as never authorized only from its skipped fence step and authentic
immutable terminal no-authority receipt. Trusted operators deleting repository
history are outside this cooperating boundary and must escalate, never reset.

## Recovery and acceptance

Protected journals survive controller-process restart inside the same service
on the same runner. The controller persists intent before effects and reconciles
native operations before retrying. Its independent deadline supervisor cleans up
descendants even after controller death. The pilot declares a controller crash
after native dispatch settlement and loss of the first successful real publication
response, exercising exact discovery without duplicate effects. Declared fresh
admission probes cover unavailable producer, wrong revision/attempt and changed
expected criteria/verifier. A read-only native remote-precondition probe compares
the genuine previous base with the actual published ref and retains a conflict;
it performs no remote writes and makes no actor-induced CAS-race claim. After
completion and exact replay, local bounded state reads drain the remaining original
wall reservation capacity before a fresh read is denied. A genuinely expired
preapproved test policy version shares the same original goal/account/limits;
finally the main delegation is revoked and a fresh effect is denied. There are
no replacement verification goals, accounting stores, fake clocks or budget refunds. Recovery preserves
all goal budgets and native identities.

Runner loss ends the supported recovery domain. Do not restore journals, create
replacement stores or restart this objective on another runner. Retain public
evidence and report unresolved effects. A persistent Linux host is not required
for the explicitly bounded same-runner process-recovery acceptance.

Before declaring completion, retain authentic isolated lifecycle, task/attempt/
handoff/publication, exact producer selection, admitted report and native
completion receipts. Demonstrate duplicate/restart behavior, lost-response
discovery and conflicting-ref preservation. Exercise failed/unavailable/wrong
revision or attempt evidence, changed criteria/verifier, revoked/expired authority
and exhausted budgets; each must block the relevant effect/completion and preserve
history. Controlled tests are reported separately from live evidence. This
single-producer pilot establishes no composition, multi-target, coding-agent,
distributed ownership or runner-loss recovery claim.

Monitor the public status and Actions job, preserve protected control storage
through all process recovery, and stop the exact service for cancellation.
Cancellation prevents new effects and retains uncertain ones for separately
approved reconciliation. At deadline or any stale policy, unavailable runtime,
failed producer, budget exhaustion or conflicting remote state, retain the
actionable escalation and receipts. No live completion is claimed by this document.


After a successful actual pilot, retain its non-authoritative evidence selection
in `verification-references/req039.json` (repository/run/attempt/artifact IDs, no
credentials). This selector is outside the executed-code closure: it selects
which proof to authenticate and cannot approve an origin, policy or result.
The existing read-only CI job token permits fresh verification of that retained
proof. The live tests independently check actual server artifact bytes/digest,
code/requirement semantics, original approval and fresh native producer admission.
Absence is inconclusive; a configured invalid or incomplete proof fails.
Requirement semantic pins exclude lifecycle/evidence/review-date fields, allowing
an evidence-based lifecycle activation without invalidating its own proof.

The authenticated live-proof test contract is selected explicitly by
`EXACLY_REQ039_VERIFY_RETAINED_PROOF=1` in the Spec check/state jobs, or by direct
live run/artifact environment IDs. The selector file alone does not activate
this read capability in other producers. The unchanged main-only evidence
workflow does not configure this contract: its live cases are explicitly skipped
and cannot establish REQ-037/039. Spec's authenticated model/check jobs consume
the retained reference and gate the actual observations. Before a reference exists
verification is inconclusive; a selected malformed/incomplete proof or missing
read credential fails. There is no silent skip of a configured proof.
