# Product delivery implementation checkpoint

Recovery point: 2026-11-01. This is the **second target**, REQ-040–043 under
CAP-005/PROC-005 from [roadmap #87](https://github.com/whiteredhouse/exacly/pull/87).
The [verification checkpoint](automation-progress.md) covers REQ-034–039
separately. Neither target inherits the other's operational authority.

## Current implementation and acceptance

| Requirement | Source implementation | Verification | Operational acceptance |
| --- | --- | --- | --- |
| REQ-040 | Merged [PR #113](https://github.com/whiteredhouse/exacly/pull/113): advisory planner and immutable proposal CLI | All 149 bound cases and full supported CI passed; 146 installed checks passed | Advisory planning only; no coding-agent or delivery effect implied |
| REQ-041 | Merged [PR #114](https://github.com/whiteredhouse/exacly/pull/114): private journal, bounded native calls, exact GitHub merge adapter and fixed CLI | 233 new controlled cases; full CI passed 4,496 tests and 37 active gates; independent review and installed QA passed | No runtime delegation approved; genuine positive observer unconfigured and authentic negative lease/protection acceptance pending |
| REQ-042 | [PR #115](https://github.com/whiteredhouse/exacly/pull/115), draft: exact prebuilt bytes through a separate local owning publisher | Role correction passed all 93 controlled publication/CLI cases, independent review and 100 fresh installed checks; new full hosted CI is required | No public release or operational publisher destination approved |
| REQ-043 | Static deployment, independent HTTP observer/evaluator and separately bounded rollback implemented in an isolated worktree | Review corrections implemented; final 100 native/observer cases and 15 CLI cases await the publication dependency freeze, installed QA and full CI | No environment, observer or rollback grant approved |

CAP-005/PROC-005, REQ-041–043 and APP-025/INFO-014 remain proposed. Merged
source and controlled tests do not establish authentic operational acceptance.
Approved requirement statements and acceptance criteria remain unchanged.
No general coding-agent integration is claimed; the snapshot-inventory worker
hashes fixture inputs.

## Merged source and exact evidence

Planning PR #113 merged reviewed head
`63d7ac47f40d2e9fe1c5519be176c2103c54ee2a` as
`c09bc04684a69bb7cfef354f7d5a240776935343`.
[Spec run 41301035807](https://github.com/whiteredhouse/exacly/actions/runs/41301035807)
passed 4,263 tests, all 149 REQ-040 cases and all 37 active readiness gates.
Its 15 skips were unconfigured live cases for still-proposed first-target
contracts. Genuine Bubblewrap and five mandatory worker/cgroup cases passed.
Independent review covered genesis resets and malformed documents; fresh
installed artifacts passed 146 checks across 29 isolated CLI invocations.

Merge PR #114 merged reviewed head
`39ddd94695aa84dbf48e9000fd0c0e32486f0076`, tree
`5496799d976e9717cdaafa2880a43bcdc79c6431`, as
`43bd44add2814032c3560cacd976c612481e2c36`.
[Spec run 41305511088](https://github.com/whiteredhouse/exacly/actions/runs/41305511088)
passed 4,496 tests, 16 explicit unconfigured live skips and all 37 active gates,
with no model findings. Independent source review passed 50 guarded cases;
fresh installed wheel/sdist QA passed 99 assertions across 18 isolated CLI
calls and matched all 80 runtime/schema payloads and complete artifact rosters.
Landing passed. Normal expected-head merging was accepted without bypass;
no blocking reviews or threads remained.

The required local full checks encountered the existing managed-container
Bubblewrap UID-map restriction after 1,617 passing tests and one live skip
for the merge slice. No isolation fallback or coverage waiver was substituted;
supported hosted CI supplies the full verification result.

## Main observations and first-target preservation

Post-planning [Spec run 41302839911](https://github.com/whiteredhouse/exacly/actions/runs/41302839911)
and [verification-evidence run 41302839974](https://github.com/whiteredhouse/exacly/actions/runs/41302839974)
passed. The CI-owned state writer published serial 68 as
`27b3284d9d5a4623751ceafe4e39186a81be627c`, config revision
`c09bc04684a69bb7cfef354f7d5a240776935343`, at
`2026-11-01T12:17:32Z`. The exact file was reread: REQ-034/035/036 and
all 149 REQ-040 cases passed. REQ-037/038/039 remained inconclusive because
their live cases were skipped. The first target is not complete.

After PR #114, main's material source revision was
`43bd44add2814032c3560cacd976c612481e2c36`.
[Spec run 41309250343](https://github.com/whiteredhouse/exacly/actions/runs/41309250343)
passed its check job with 4,496 tests, 16 explicit live skips and all 37 active
gates, with no model findings. Its separate state writer and
[verification-evidence run 41309250329](https://github.com/whiteredhouse/exacly/actions/runs/41309250329)
also passed. The exact CI-owned file was reread at state commit
`a8479dcc0ccea8ed40e8473dfeb9fa6a012fb596`: serial 69, config revision
`43bd44add2814032c3560cacd976c612481e2c36`, written
`2026-11-01T13:33:53Z`. REQ-034/035/036/040 passed; REQ-037–039 and
REQ-041 remain inconclusive due to required unconfigured live cases. REQ-042/043
were not checked at that earlier source revision. No state was hand-edited.

The pilot ref `refs/heads/pilot/req039-consumer` still points to
`7533c740df148caf4ed7b972e3b04f2e6f384260`. Its policies, markers,
journals, keys and retained evidence are preserved. Changed protected source,
model or observer revisions require newly generated and independently approved
pins before another pilot invocation. Old proofs are not transferable authority.

## Publication work and current blocker

Current branch: `codex/product-delivery-publication`.
Worktree: `/workspace/work/exacly-product-delivery/publication`.
PR #115 targets main after PR #114's merge. Its original failed draft head was
`5ea4b7546be09fdd7e6e1fa6871b1479af81eea6`.

[Spec run 41307075648](https://github.com/whiteredhouse/exacly/actions/runs/41307075648)
failed: 21 failures, 4,559 passes and 16 unconfigured live skips.
An earlier real isolation test loads libseccomp into the controller. The fresh
owning publisher has a different actual native-library profile and incorrectly
recomputed the controller's profile as its own. The unmodified sequence and an
independent unrelated-library load both reproduced the failure.

The correction keeps full actual runtime fingerprints. Controller and owning
workflow receive separate runtime pins; the owner's fixed read-only bootstrap
probe measures its actual role without receiving an expected digest. The writer
checks that sealed owner pin before effects, and signed receipts retain the role
identity. Closed scope, artifact, verification, policy and exact intent bindings
remain mandatory. A historical consumer authenticates recorded identity without
equating it to today's consumer runtime. The original real isolation-before-
publication sequence now passes. Independent review passed 18 focused cases
and a separate actual libzstd-controller publication/consumer probe with
unchanged source guards. Fresh installed wheel/sdist QA passed 100 assertions,
14 isolated CLI invocations, eight independent consumers and three real owner
dispatches; a wrong owner pin was refused before version reservation. All 82
runtime/schema payloads and complete artifact/RECORD rosters matched source.
The full publication/CLI matrix passed all 93 cases in 293.87 seconds, with all
five owned source/test/guide blobs unchanged. The package has 76 Python files;
its frozen roster digest is
`dfe451ab85644a5f37bf318c984ecf66c2935297336565fffbb92d2e5c8d560e`.
The fresh wheel is
`a64f5bd90405bac8a15498e4057194b87b3b337d0a95c60b658f62104fe1b345`
and sdist
`cc35c7a885facf1f1eaa7035e3ef05e369bb9f9706e9d76e92176bb48dc0fa75`.
Global lint/format, strict typing (234 files), model validation and impact review
passed; approved REQ-040–043 statements and criteria are unchanged. Runtime-role
source is retained in commit `54918007994a26c798cd1ba3bbb5f117fa2f2c63`.

The required local full attempt stopped after 1,059 passes and one live skip at
an unchanged watchdog timing assertion while several checks ran concurrently.
An unchanged focused rerun also missed its backend start marker within the short
native window. Read-only journal evidence showed the original one-second bound,
full reservation, successful cleanup and zero writes. After the other large runs
finished, the original testcase passed in 3.56 seconds without code, cap or
assertion changes. Separate phase instrumentation showed actual backend entry,
cleanup at approximately 755 ms within the one-second bound, and slower outer
validation/bookkeeping. The first failed fixture was pruned before phase analysis;
its exact breakdown is not claimed. The full local attempt remains failed.
Fresh supported hosted CI remains mandatory before this draft can merge.

Earlier publication review corrected future signed timestamps and genuine
other-intent receipt replay. Those regressions remain. The reference owns one
private immutable version and exact prebuilt bytes; it does not implement PyPI,
GitHub Releases or arbitrary workflow dispatch. The existing package release
workflow remains unchanged. A separately approved owner config is additional to
engine accounting, not proof of its approval pin or reservation.

## Deployment integration and recovery

Deployment worktree: `/workspace/work/exacly-product-delivery/deployment`,
branch `codex/product-delivery-deployment`, currently based on publication
head `5ea4b754…`. Preserve its uncommitted owned files when adopting the
publication fix. Earlier frozen worktrees remain under
`/workspace/work/exacly-product-delivery/{implementation,merge}`.

The reference is one Linux host with local POSIX storage and a nonproduction
loopback static-byte service. It consumes authenticated published bytes,
uses immutable owner records and an active-generation CAS, and samples actual
HTTP responses through a separately keyed observer. Artifact bytes are not
executed. Applied effects, historical measurements and fresh readiness have
separate meanings. The native readiness gate establishes no runtime health.

Review corrections preserve applied effects after health expiry, sample actual
UTC after fallible authentication, reject observer-key/owner lock aliasing, and
verify generation applicability inside the charged native call. CLI presentation
retains that observation point and only ages its TTL; it performs no implicit
provider read. Rollback needs separate current authority, a fresh unhealthy
report, exact current generation and remaining original rollback capacity.
Ambiguity never permits resend or counter reset.

After adopting the publication commit, freeze all package sources and run the
complete native/observer and CLI matrices, independent review, installed artifact
QA, model/impact checks and supported full CI. Create a focused deployment PR,
retarget it to main after its dependency merges, and merge only its exact
reviewed and tested head. Preserve real crash and concurrency cases.

## Authority, blockers and next actions

Implementation authorization covers these source PRs, approved nonproduction
checks and normal exact-head merges after required checks/reviews. It excludes
runtime delegation, public releases, production deployment, destructive effects,
new paid infrastructure and repository-rule changes. No runtime grant is
available. The managed environment has no configured runtime credentials or
outbound identities.

Existing nonproduction repository/environment, owning workflow, trusted observer
and retention decisions have been requested while independent source work
continues. Prepare exact operational packets from the final installation,
selected destinations, identities, artifact hashes, journal original scope,
separate stage authorities, finite limits/expiry and recovery boundaries before
requesting an additional grant. Do not fabricate approval from a template.

Next: update PR #115 with its reviewed runtime-role correction and the truthful
local-check results, then require a fresh full hosted pass. Adopt that
dependency into deployment without losing work; finish its verification and PR.
Verify each post-merge main observation refresh. Keep operational gaps explicit
and preserve proposed lifecycle until authentic acceptance justifies activation.

External recovery reports are under `/workspace/work/exacly-product-delivery/`,
including `state.json`, source-freeze manifests, CI investigation, review logs,
QA reports and ownership checkpoints. Read current remote heads/checks and any
operator reply before resuming. Never merge unrelated dependency PRs.
