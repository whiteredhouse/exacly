# Product delivery implementation checkpoint

Recovery point: 2026-11-01. This is the **second target**, REQ-040–043 under
CAP-005/PROC-005 from [roadmap #87](https://github.com/whiteredhouse/exacly/pull/87).
The [verification checkpoint](automation-progress.md) covers REQ-034–039
separately. Neither target inherits the other's operational authority.

## Current source and evidence

Work started from main
`3d8e608b3f2e7f6bd30c47699c7bbcbff1e1b992`, the CI-owned state refresh after
implementation merge `c27a098572629c9a741bf2f0bef5a8bc168dd68d` (#103).
[Main Spec run 41267330287](https://github.com/whiteredhouse/exacly/actions/runs/41267330287)
and [verification evidence run 41267330058](https://github.com/whiteredhouse/exacly/actions/runs/41267330058)
passed. Committed main state is serial 67, config revision `c27a098…`, with
passed observations for REQ-034/035/036. REQ-037/038/039 observations remain
inconclusive because their required live cases were unconfigured/skipped.
The earlier state-publication blockage is resolved; it is not a current blocker.

The pilot ref `refs/heads/pilot/req039-consumer` was independently reread and
still points to `7533c740df148caf4ed7b972e3b04f2e6f384260`. Its policies,
markers, approval procedure, journals and retained evidence are not changed by
this work. New CLI/model/source revisions are not covered by an old pilot pin.
Before applying a pilot delegation to changed protected code or semantics,
regenerate its exact pins and obtain independent approval; do not copy successful
component observations into live acceptance.

| Requirement | Source implementation | Verification | Operational acceptance |
| --- | --- | --- | --- |
| REQ-040 | First slice in progress: separate implementation planner and immutable proposal CLI | Focused tests and supported CI pending | Advisory planning only; no coding-agent or delivery effect implied |
| REQ-041 | Research complete; exact-identity merge adapter follows common contracts | Pending | No merge runtime delegation approved |
| REQ-042 | Research complete; separate owning-publisher contract required | Pending | No public release or publisher destination approved |
| REQ-043 | Research complete; separate operational observer/evaluator required | Pending | No deployment environment, observer or rollback grant approved |

CAP-005/PROC-005 remain proposed while the whole delivery process is incomplete.
REQ-041–043 and APP-025 remain proposed until their complete implemented contracts
and matching acceptance justify a different lifecycle. Schemas and controlled
tests alone do not establish authentic delivery acceptance. Keep approved
Requirement statements and acceptance criteria unchanged.

## Current work and file ownership

Current branch: `codex/product-delivery-planning`.
Worktree: `/workspace/work/exacly-product-delivery/implementation`.
Base: `3d8e608b3f2e7f6bd30c47699c7bbcbff1e1b992`.
No implementation PR has been opened for this target yet.

The first slice implements REQ-040's advisory planning contract and introduces
closed, separately scoped delivery approval/intent/receipt documents. It does not
invoke merge, publication or deployment adapters.

Independent review identified a revision-reset edge. The planning policy now
requires a genesis-contract pin over the complete initial approved inputs,
including observations; logical proposal identity includes that pin. Same-series
changes require explicit predecessor/supersession and a revision increment.
New independently approved initial contracts have distinct series identities.
These advisory artifacts do not select a globally active execution head.

- Planner owner: `implementation_planning.py` and its acceptance tests.
- Contract owner: `delivery_automation.py` and pure contract tests.
- CLI owner: `implementation propose/check`, private proposal-bundle retention
  and CLI tests.
- Main agent: shared architecture, model/lifecycle/bindings, documentation,
  independent review, PRs, merging and this checkpoint.

The existing verification planner and pilot-specific authorizer stay separate.
Implementation proposals derive bounded tasks and dependencies from independently
approved intent/findings; missing verification alone does not establish missing
code. A proposal grants no execution or later delivery authority. There is no
general coding-agent integration in this slice, and the inventory worker is not
one.

## Delivery adapter decisions

Use a distinct product-delivery profile and private journal. Reuse proven private
filesystem, canonical JSON and locking primitives without retyping verification
or handoff journals as delivery history. Persist exact intent and authoritative
budget reservations before effects. Ambiguous outcomes block conflicting work
and require discovery/reconciliation; absence is not permission to resend.
Local locks do not fence independent external writers.

- **Merge:** ordinary GitHub merge requests pin the head but not the base.
  Investigate the documented atomic GraphQL `updateRefs` transaction for the
  GitHub-generated merge commit, exact base/head leases and a distinct delivery
  operation marker. The first profile must reject unsupported forks, methods,
  queues and protection semantics. Establish the no-op head-lease behavior and
  server-enforced reviews/checks with authentic nonproduction acceptance before
  claiming support. Never fall back to a weaker API.
- **Publication:** the current tag-to-PyPI workflow rebuilds source and is not
  an approved promotion workflow for already selected bytes. Keep its ownership
  unchanged. A new explicit adapter must select an independently approved owning
  publisher, exact workflow execution, immutable destination/version and exact
  artifact inventory. Current workflow-dispatch APIs can return a run ID, but
  lost replies still require reconciliation and have no generic retry grant.
- **Deployment:** introduce a separate observation/evaluation contract for the
  exact application, environment, rollout, artifact and fresh sampling window.
  Source tests, publication, workflow success and native verification/delivery
  gates do not establish runtime health. A bounded local static-service profile
  can demonstrate actual byte deployment and independent HTTP measurements in
  controlled tests; it does not establish arbitrary application health.
  Rollback needs its own exact or predelegated bounded authority and observations.

## Authority and acceptance blockers

The user authorizes isolated branches, implementation, tests, approved
nonproduction checks, PR creation/updates and merging the exact independently
reviewed/tested head after required GitHub checks and reviews, without bypass or
rule changes. This is implementation authorization, not an Exacly runtime grant.

Real public releases, production deployments, destructive effects, paid services
and repository-rule changes remain outside that authorization. The configured
cloud environment has no runtime secrets or outbound identities for these
effects. Existing nonproduction destinations, owning workflows and trusted
observers have been requested while independent implementation continues.

Prepare concrete operational packets before asking for a live grant: exact
repository/environment IDs, protected refs, source/workflow/observer revisions,
artifact hashes, approval identity, allowed effects, limits/expiry, storage and
recovery identities, receipt verification and rollback boundaries. Never include
credentials or private keys. Pending operational access must not be represented
as passing acceptance or prevent independent source implementation.

## Next actions and recovery

1. Finish planner, immutable output and common-document contracts. Independently
   review their actual behavior and approval boundaries.
2. Preserve REQ-040's approved criteria; add truthful implementation references
   and executed observation bindings. Activate only its delivered advisory
   contract, update the active verification policy and validate model/impact.
3. Run affected tests, lint/type/model checks and the repository's required full
   check. This cloud host has a known real isolation limitation; keep genuine
   supported Linux CI mandatory and separate skipped live cases from its results.
4. Open a focused PR, verify exact tested/reviewed head and required GitHub
   reviews/checks, then merge under the user's standing implementation approval.
   Verify the main CI-owned observation refresh; never hand-edit state.
5. Refresh from main and continue merge, publication and deployment/observer
   slices. Record each PR, commit, exact CI/evidence and remaining acceptance here.
   Do not stop after the first PR or treat planning as implementation execution.

Do not merge unrelated dependency PRs. Preserve original worktrees and unrelated
changes. If a session ends, inspect this branch, current remote main, PR/check
identities, operator replies and all pending journal/effect histories before
continuing. Local research and baseline query snapshots are under
`/workspace/work/exacly-product-delivery/`; committed source and actual remote
evidence are authoritative.
