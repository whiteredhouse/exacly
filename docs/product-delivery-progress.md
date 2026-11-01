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
passed. At that starting point, committed main state was serial 67, config revision `c27a098…`, with
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
| REQ-040 | Merged PR #113: separate implementation planner and immutable proposal CLI | All 149 bound cases passed in supported hosted CI; 146 installed checks passed | Advisory planning only; no coding-agent or delivery effect implied |
| REQ-041 | Implemented in PR #114: private delivery journal, exact-identity GitHub merge adapter and fixed CLI | 233 controlled cases plus all 149 planning/contract compatibility cases passed; 50 independent review cases and 99 installed checks passed; full hosted CI pending | No merge runtime delegation approved; genuine positive observer unconfigured and real negative lease/protection acceptance still pending |
| REQ-042 | Owning workflow and exact-byte consumer integrated with the shared engine and fixed publication CLI | 82 controlled publication/CLI cases, 43 prior delivery cases and 175 earlier CLI compatibility cases passed; integrated independent review, installed QA and full hosted CI pending | No public release or operational publisher destination approved |
| REQ-043 | Static deployment, separate HTTP observer/evaluator and bounded rollback reference implemented; shared integration pending | 83 controlled cases passed; independent review cleared the health-expiry fix with 22 targeted cases | No deployment environment, observer or rollback grant approved |

CAP-005/PROC-005 remain proposed while the whole delivery process is incomplete.
REQ-041–043 and APP-025 remain proposed until their complete implemented contracts
and matching acceptance justify a different lifecycle. Schemas and controlled
tests alone do not establish authentic delivery acceptance. Keep approved
Requirement statements and acceptance criteria unchanged.

## Current work and file ownership

Current branch: `codex/product-delivery-publication`, stacked on merge-slice commit `39ddd94695aa84dbf48e9000fd0c0e32486f0076` while its hosted CI runs.
Current worktree: `/workspace/work/exacly-product-delivery/publication`. The frozen merge worktree remains `/workspace/work/exacly-product-delivery/merge`; the frozen first-slice worktree remains `/workspace/work/exacly-product-delivery/implementation`.
Base: `3d8e608b3f2e7f6bd30c47699c7bbcbff1e1b992`.
First-slice [PR #113](https://github.com/whiteredhouse/exacly/pull/113) merged as `c09bc04684a69bb7cfef354f7d5a240776935343` from exact reviewed head `63d7ac47f40d2e9fe1c5519be176c2103c54ee2a` and tree `2d6d8533dab0c454c2a86e7a56a3a66cbf8de570`. [Spec run 41301035807](https://github.com/whiteredhouse/exacly/actions/runs/41301035807) passed **4,263 tests and all 37 active readiness gates**, including all 149 REQ-040 cases, genuine Bubblewrap and the five mandatory worker/cgroup cases. Its 15 skips are unconfigured live cases for the still-proposed first-target contracts; they establish no live acceptance. [Landing run 41301035793](https://github.com/whiteredhouse/exacly/actions/runs/41301035793) passed. Independent review passed all 149 new tests; a fresh wheel passed 146 checks across 29 isolated CLI invocations, with 71 runtime/schema payloads matching source and RECORD. Local full checks passed 1,423 tests before the existing managed-container UID-map restriction; supported hosted CI supplies the full result. GitHub reported clean ordinary merge eligibility, no blocking reviews or threads, and accepted the expected-head merge without bypass. Post-merge [Spec run 41302839911](https://github.com/whiteredhouse/exacly/actions/runs/41302839911) and [verification-evidence run 41302839974](https://github.com/whiteredhouse/exacly/actions/runs/41302839974) are pending; canonical main observations have not yet been claimed refreshed.

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

1. Verify the post-merge main CI-owned observation refresh; never hand-edit state.
2. Finish and independently review the private delivery journal, bounded native
   process watchdog, exact GitHub merge adapter and fixed CLI. Exercise crashes,
   uncertain outcomes, concurrency, revocation and read-only credential recovery.
3. Continue the owning-publication and deployment/observer reference profiles in
   isolated worktrees while operational destinations remain unapproved.
4. For each slice, preserve approved criteria, update truthful references and
   bindings, validate model/impact, run affected coverage and full supported CI,
   and merge only the exact independently reviewed/tested head after required
   GitHub checks/reviews. Keep live gaps explicit and incomplete contracts proposed.
5. Record each PR, commit, exact CI/evidence and remaining operational approval
   packet here. Do not stop after the first PR or treat planning as code execution.

Do not merge unrelated dependency PRs. Preserve original worktrees and unrelated
changes. If a session ends, inspect this branch, current remote main, PR/check
identities, operator replies and all pending journal/effect histories before
continuing. Local research and baseline query snapshots are under
`/workspace/work/exacly-product-delivery/`; committed source and actual remote
evidence are authoritative.

## Merge slice in progress

The next isolated slice implements a distinct private delivery journal, canonical original-goal budgets, durable pre-effect intent, exact historical replay and discovery-only uncertain recovery. Its concrete GitHub merge adapter uses atomic exact-base/head leases, required non-bypass checks/reviews, authenticated delivery markers and a distinct delivery credential/key. The CLI will expose fixed `delivery init/show/key-init/merge/reconcile` commands; it will not load providers from candidate data.

A structural preview performs no provider I/O and does not claim current readiness. Operational apply uses actual UTC, an externally selected approval pin and actual installed configuration. Historical replay is resolved before credentials or current policy files are loaded. The current repository's existing force/deletion-only protection does not establish the stronger runtime merge profile; unsupported policies must block rather than change repository rules.

Final merge-slice source verification passed **382 tests, one unconfigured genuine
live skip**, including all 149 existing planning/common-contract cases and 233
new controlled merge cases. Independent review passed 50 source-guarded cases,
including actual concurrency, withdrawal, parent death, expiry and recovery.
Fresh installed wheel/sdist QA passed 99 assertions across 18 isolated CLI calls;
all 80 code/schema payloads and complete artifact/RECORD rosters matched source.
Ruff/format passed 268 files; strict typing passed 229 files. The original
publication-key compatibility suite passed 20 cases. Model validation has no
errors or warnings, impact is reviewed and approved REQ-040–043 statements and
criteria are unchanged. The required local full check is running; genuine full
hosted Linux verification and required GitHub reviews remain merge gates.

Substantive review corrections are retained in the implementation: uncertain
merges fence the actual repository/base branch across different pull requests;
watchdog cleanup facts are create-only and imported into SQLite by the parent;
grants cap real invocation lifetime without refunding reserved slots; runtime
identity includes bounded actual stdlib/native/TLS/proxy inputs; and policy
explicitly states GitHub's current server-protection semantics plus trusted
administrator stability. `updateRefs` leases refs, not check conclusions or a
protection-policy version. Local preflight still requires literal success; a
merged effect is never relabelled fresh verification.

The first-slice post-merge check job passed 4,263 tests, 15 first-target live
skips and all 37 active gates, including all 149 REQ-040 cases. Its separate
verification-evidence workflow also passed. Main's state writer deliberately
reran verification in a separate write-authorized job and published state serial
68 as `27b3284d9d5a4623751ceafe4e39186a81be627c`, config revision
`c09bc04684a69bb7cfef354f7d5a240776935343`, written at
`2026-11-01T12:17:32Z`. The exact CI-owned file was reread: REQ-034/035/036 and
all 149 REQ-040 cases are passed; REQ-037/038/039 remain inconclusive because
their live cases were skipped. No state file was edited by this work.

## Owning-publication slice in progress

[PR #114](https://github.com/whiteredhouse/exacly/pull/114) is open at exact
reviewed head `39ddd94695aa84dbf48e9000fd0c0e32486f0076`, tree
`5496799d976e9717cdaafa2880a43bcdc79c6431`.
[Spec run 41305511088](https://github.com/whiteredhouse/exacly/actions/runs/41305511088)
is running; [landing run 41305511064](https://github.com/whiteredhouse/exacly/actions/runs/41305511064)
passed. Its required local full check passed 1,617 tests with one unconfigured
live skip before the existing managed-container Bubblewrap UID-map restriction.
No isolation fallback or coverage waiver was substituted.

The next slice integrates the independently reviewed local owning publisher with
the frozen shared journal/runtime profile and fixed `delivery publish` command.
It invokes the exact installed workflow in a separate process, verifies an
independently signed artifact prerequisite and publishes only the approved
prebuilt bytes to one private immutable version. A signed original owner window
and retained verification window make reconciliation and downstream consumers
agree on historical publication times. Current expiry does not invalidate a
valid historical publication. The owner config is a separate one-version
predelegation; structural caller fields do not prove engine reservation,
revocation or an external approval pin.

Independent publication review cleared the future-timestamp inconsistency with
seven targeted cases and an actual re-signed receipt probe. Subsequent integrated
review found and corrected a genuine other-intent child-reply replay: the native
boundary now binds the signed receipt to its supplied retained intent, keeps
foreign facts only as diagnostics and retains uncertainty without resending.
The original reproduction passes after the fix. Integrated fixed CLI and owning
workflow tests cover real subprocess execution,
durable-before-effect, private intent, lost reply/discover-only, missing or
substituted bytes/proof, original offline history and unrelated-version
preservation. All 76 package Python sources stayed unchanged during those tests.
Original delivery CLI passed 43 cases and earlier CLI compatibility passed 175.
Integrated source review, installed artifact QA and full hosted CI remain
required before merging this slice.
