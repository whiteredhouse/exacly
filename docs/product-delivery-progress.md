# Product delivery implementation checkpoint

Checkpoint: 2026-11-02. This is the **second target**, REQ-040–043 under
CAP-005/PROC-005 from [roadmap #87](https://github.com/whiteredhouse/exacly/pull/87).
The [verification checkpoint](automation-progress.md) covers REQ-034–039
separately. Neither target inherits the other's operational authority.

## Source, verification and operational acceptance

| Requirement | Source implementation | Controlled verification | Operational acceptance |
| --- | --- | --- | --- |
| REQ-040 | Merged [PR #113](https://github.com/whiteredhouse/exacly/pull/113): advisory planner and immutable proposal CLI | 149 bound cases; full supported CI and 146 installed checks passed | Advisory planning only; no coding-agent execution or delivery authority implied |
| REQ-041 | Merged [PR #114](https://github.com/whiteredhouse/exacly/pull/114): durable journal, bounded calls and exact GitHub merge adapter | 233 controlled cases; supported full CI, independent review and 99 installed checks passed | Required genuine positive observer is unconfigured; authentic negative lease/protection cases and runtime delegation remain pending |
| REQ-042 | Merged [PR #115](https://github.com/whiteredhouse/exacly/pull/115): exact prebuilt bytes through the separately owned local publisher | 93 bound cases, independent review, 100 fresh installed checks and supported full CI passed | No operating publisher destination or public-release authority approved |
| REQ-043 | Merged [PR #116](https://github.com/whiteredhouse/exacly/pull/116): static owner, independent HTTP observer/evaluator and separately bounded rollback | 100 native/observer cases and 15 CLI cases verified; independent review and 25 installed checks passed. Final-head full CI is recorded on the linked delivery PR | No selected deployment environment, observer or rollback grant approved |

The named source PRs retain their exact checked heads, reviews and merge
identities. Their historical verification counts do not include the App-grant
and required-condition compatibility changes below. Do not reapply merged work
or infer operational approval from this checkpoint. Authentic acceptance remains
separate.

CAP-005/PROC-005, REQ-041–043 and APP-025/INFO-014 remain proposed. The merged
App-grant change added explicit token-bound capability evidence and local
credential-use criteria to REQ-041. This changeset adds direct required-condition
evidence for the supported `UNSTABLE` readiness path. Source tests, workflow
success and native readiness gates establish no runtime health.
The fixed snapshot-inventory worker hashes fixture inputs; it is not a coding
agent. No general coding-agent integration is claimed.

## GitHub App actor compatibility

The dedicated public repository
[`whiteredhouse/exacly-delivery-acceptance`](https://github.com/whiteredhouse/exacly-delivery-acceptance)
now provides a nonproduction canary with separately selected verifier and merger
Apps. The owner-approved
[diagnostic run 41422406506](https://github.com/whiteredhouse/exacly-delivery-acceptance/actions/runs/41422406506)
authenticated the selected merger installation, observed exactly one repository
and the expected canary main revision, and retained `admin: false`,
`maintain: false`, `push: false`. The pinned token action successfully requested
Contents write, but the old helper rejected the repository's `push` role flag
after three reads. The old native core adapter shared that assumption.
The token was revoked. Protection, signatures and effective-rule reads were
not reached; no required check or merge effect was performed by this diagnostic.

The [merge profile](product-delivery-merge.md#github-app-capability-evidence)
now adds an explicit App-grant actor alongside the unchanged legacy actor
shape. Its independently reviewed mint evidence is bound to the exact token,
App, installation, selected repository and exact limited permissions. A finite
local use window is checked before each fresh provider request. The profile
retains raw role flags, original history and every scope, protection, review
and no-bypass check. It does not treat requested action inputs as observed
provider response metadata or infer GitHub's token expiry.

Merged [PR #117](https://github.com/whiteredhouse/exacly/pull/117) covers this
authentic response shape through controlled provider cases, invalid grant
evidence, live lease checks and historical replay. The later owner-approved
[read-only preflight 41469016096](https://github.com/whiteredhouse/exacly-delivery-acceptance/actions/runs/41469016096)
used the reviewed native implementation, accepted the actual role flags,
verified the one-repository scope and configured protection, and completed six
reads with zero writes. Its token was revoked. This establishes that selected
read-only compatibility result, not protected `updateRefs` or authentic merge
acceptance.

## GitHub aggregate readiness compatibility

The canary's [PR #5](https://github.com/whiteredhouse/exacly-delivery-acceptance/pull/5)
has a genuine human approval on its exact head. The separately trusted verifier
[run 41477611855](https://github.com/whiteredhouse/exacly-delivery-acceptance/actions/runs/41477611855)
ran seven business cases and published a successful required App-owned check on
the exact generated merge commit. The ordinary head CI
[run 41477202018](https://github.com/whiteredhouse/exacly-delivery-acceptance/actions/runs/41477202018)
initially awaited workflow approval, then passed 56 tests in its approval-enabled
second attempt. PR content review and workflow execution approval are separate.

The owner reported an eligible merge box with both checks passing, while the
API continued to report `APPROVED / UNSTABLE / MERGEABLE`. Both head and merge
commit rollups reported success. The previous literal `CLEAN` gate therefore
blocked this candidate. Empty queued App suites alone did not establish the
cause: source PR #4 was `CLEAN` with the same Apps' empty suites. No successful
native merge or provider-enforcement guarantee is inferred from those displays.

The [merge profile](product-delivery-merge.md#aggregate-state-and-required-condition-evidence)
now permits that documented aggregate state only with complete direct evidence:
an explicitly reviewed six-permission App grant includes Commit statuses read;
all actual server-required and approved-policy App-bound CheckRuns succeed on
exact `M`; complete `H/M` legacy-status inventories contain no required-name
collision; explicit branch-policy and resolved-conversation evidence excludes
unsupported deployments or restricted-push requirements; and the exact comparison establishes current
head/base ancestry. Every existing authority, protection, identity, review and
reference condition remains required. Unknown or unsupported conditions block.

The original five-permission grant remains supported for `CLEAN`; it cannot
authorize the additional status reads. The six-permission grant uses schema
`0.2.0`, retaining the same credential kind and adding only `statuses: read`.
The App owner and trusted mint must explicitly grant that access. Changing the
schema or permission map changes stable operation authority and cannot be
performed as read-only token rotation. No App setting has been changed by this
source change, and the earlier five-permission diagnostic remains its original
limited evidence.

The conditional fresh path uses 18 inspection reads and 40 invocation reads.
An initial `UNSTABLE` preflight must fit the selected invocation capacity before
marker preparation; the adapter never raises approved caps. The existing `CLEAN`
protocol, retained record shapes and terminal replay remain unchanged. Newly
installed code still needs newly selected operation authority. The changeset's checks report actual
test results; source implementation does not establish operational acceptance.

## Merged dependencies and supported CI

Planning PR #113 merged reviewed head
`63d7ac47f40d2e9fe1c5519be176c2103c54ee2a` as
`c09bc04684a69bb7cfef354f7d5a240776935343`.
[Spec run 41301035807](https://github.com/whiteredhouse/exacly/actions/runs/41301035807)
passed 4,263 tests, all 149 REQ-040 cases and all 37 active gates. Its 15 skips
were explicit unconfigured live cases for still-proposed first-target contracts.
Fresh installed artifacts passed 146 checks across 29 isolated CLI invocations.

Merge PR #114 merged reviewed head
`39ddd94695aa84dbf48e9000fd0c0e32486f0076` as
`43bd44add2814032c3560cacd976c612481e2c36`.
[Spec run 41305511088](https://github.com/whiteredhouse/exacly/actions/runs/41305511088)
passed 4,496 tests, 16 explicit live skips and all 37 active gates, with no
model findings. Independent review passed 50 guarded cases. Fresh installed
wheel/sdist QA passed 99 assertions across 18 isolated CLI calls and matched all
80 runtime/schema payloads and complete artifact rosters.

Publication PR #115 merged reviewed head
`0d1e44d785b80a8fbc75db600f32e6a88abe715b` as
`8fb9d568fe5a5c6cf39d786ecc9d2a37f35b1bae`.
[Spec run 41312340199](https://github.com/whiteredhouse/exacly/actions/runs/41312340199)
passed 4,589 tests, 16 explicit unconfigured live skips and all 37 active gates, with no model findings. Genuine Bubblewrap, syscall verification and all five mandatory worker/cgroup cases passed.
[Landing run 41312340158](https://github.com/whiteredhouse/exacly/actions/runs/41312340158)
passed. Each source merge used normal expected-head merging after required
checks/reviews, without rule changes or bypass.

## Deployment implementation snapshot (2026-11-01)

This section retains the deployment changeset's original source and verification
snapshot. Its worktree was `/workspace/work/exacly-product-delivery/deployment`.
Branch: `codex/product-delivery-deployment`. The publication dependency is
`0d1e44d785b80a8fbc75db600f32e6a88abe715b`; refresh current main and inspect
the delivery PR before performing any recovery. Preserve unrelated worktrees.
The frozen implementation is commit
`2b76339700a65e6b23fe043c7e6cf6185cfe555e`, tree
`43e59ef6a94762c2a6098edeabbb12557796baa9`. A subsequent checkpoint update
changes this document only; the linked PR identifies the final tested head.

The fixed reference runs on one trusted Linux host with local POSIX storage
and a nonproduction loopback static-byte service. It consumes authenticated
published bytes through the default publisher consumer. Opaque artifacts are
never executed. Immutable owner intent/rollout files precede an active-generation
CAS; unresolved intents fence conflicting work. Discovery reads actual active
ancestry and authenticates activation, not merely a prepared signed record.

A separately keyed observer measures actual HTTP bytes, status, latency,
application/environment, source, rollout, nonce, generation and sampling window.
Applied effects, historical measurements and fresh operational readiness are
separate. Expired/unavailable/unhealthy observations preserve a known applied
effect and grant no implicit rollback.

Generation applicability is checked inside the charged native call. Retained
CLI presentation keeps that native observation point and only ages its TTL;
it performs no implicit provider read. A fresh live API query needs an explicit
deadline and caller-owned read reservation. Rollback requires its own current
authority, exact current generation, fresh trusted failure, permitted prior
publication and remaining original rollback capacity, followed by new observation.

The 79 production Python files remained unchanged across verification. Their
frozen roster digest is
`d8ac9238c226637b9fa294839af7cfd3ac7a8ba8be937d990f7ad0170c7310b5`.
The isolated native/observer matrix passed 100 cases in 307.73 seconds; all 15
CLI cases passed in 335.90 seconds. Independent review passed 27 other critical
cases, then cleared the two corrected crash fixtures through a guarded three-case
sequence beginning with actual seccomp loading and ending in genuine SIGKILL
before/after CAS plus authenticated read-only reconciliation. Only those two
test/helper files changed after the isolated 100-case run; production did not.
The final-head hosted run covers the complete final test snapshot.

Fresh runtime-only wheel/sdist QA passed 25 assertions across 12 isolated CLI
calls, two real owning publications, three actual environment mutations and ten
HTTP reads. It verified rollback reservation before CAS, denial after original
capacity exhaustion, actual short-window expiry without effect/history mutation,
offline replay without clock/factory/keys/service and authenticated consumption
of the prior installation's publication. All 85 runtime/schema payloads, complete
artifact rosters and 171 dependency RECORD entries matched actual build inputs.

Wheel:
`733dca904e0cb75f9484e23c61403119efaed072624ccee3e1e5ba0a663534af`.
Sdist:
`565751bc6479a0bedc7006070b898906a68b072899e469fcde88cf74af05f1ba`.

The fresh frozen local full check passed 1,678 tests with one unconfigured live
skip in 1,196.80 seconds, then stopped at the existing managed-container
Bubblewrap UID-map restriction before candidate release. Lint/format passed
283 files and strict typing passed 241 files. No isolation fallback, test
exclusion, cap change or coverage waiver was substituted. Supported hosted
verification remains the merge gate.

Global lint/format, strict typing, model validation and impact review accompany
this changeset. Changed records are REQ-043, APP-025 and INFO-014; affected
REQ-041/042, PROC-005, CAP-005 and OUT-001 are covered by the full supported run.
Independent documentation/model review found no remaining substantive mismatch,
and every declared exact/wildcard test binding resolves a final test.

## Retained corrections and trust boundaries

Publication's original full-CI failure came from recomputing a controller
runtime in a distinct owner process. The corrected profile keeps actual complete
fingerprints, separately pins the owner's config-free measured role, and retains
its signed identity. Closed policy/scope/intent/artifact/verification checks remain.
Historical consumers authenticate the recorded role without equating it to
today's consumer runtime. The original isolation-before-publication sequence,
wrong-pin rejection and independent unrelated-library probes pass. The existing
package release workflow is unchanged; this is not a PyPI/GitHub Releases adapter.

The owner's private one-version configuration is additional to trusted engine
accounting; it does not prove the engine's external ApprovalPin, reservation or
revocation. The same-user host is trusted. Local HMAC purpose separation alone
does not establish independence from a dishonest host administrator.

A local publication full attempt stopped at an unchanged watchdog timing
assertion under concurrent checks. Retained evidence and separate instrumentation
showed timely cleanup within its one-second bound, zero writes and full reserved
capacity; the original testcase then passed unchanged. Its failed full attempt
remains failed, and the original pruned fixture's exact timing is not claimed.
No cap or assertion was weakened. Supported hosted verification is authoritative.

## Main observations and first-target preservation

Use the [CI-owned state](../spec/exacly/state/main.json) and linked workflow runs
for current assessments; do not infer current applicability from historical
snapshots in this document. Post-merge state jobs rerun verification with separate
write authority. Never hand-edit observed state or copy controlled results into
live acceptance.

The earlier App-grant compatibility change started from main commit
`8257a98e8433826fbee35ef531e1f18e0002b38f`, with CI-owned state serial 70 and
config revision `e315018aee684b260dcc6e0c30332d319b867d33`. REQ-041 remains
inconclusive because its required authentic acceptance case is unconfigured.

The required-condition readiness change starts from main commit
`b868263fd76e222800ab72b5c817034684de5c31`, with CI-owned state serial 71 and
config revision `7e17a72d13cd4fcc5caeab37e3601cdb104ee01f`. That state is preserved;
REQ-041 remains proposed and its required authentic acceptance is still pending.

The earlier publication dependency snapshot was serial 69, state commit
`a8479dcc0ccea8ed40e8473dfeb9fa6a012fb596`, config revision
`43bd44add2814032c3560cacd976c612481e2c36`, written
`2026-11-01T13:33:53Z`.
[Main Spec run 41309250343](https://github.com/whiteredhouse/exacly/actions/runs/41309250343)
and [verification-evidence run 41309250329](https://github.com/whiteredhouse/exacly/actions/runs/41309250329)
passed. REQ-034/035/036/040 passed. REQ-037–039 and REQ-041 remained
inconclusive because required live cases were unconfigured; REQ-042/043 were not
implemented at that earlier revision. The first target is not complete.

The pilot ref `refs/heads/pilot/req039-consumer` was retained at
`7533c740df148caf4ed7b972e3b04f2e6f384260`. Its refs, policies, keys,
journals and evidence were not changed. New protected source/model/runtime
revisions require newly generated and independently reviewed pins before any
new invocation. Historical proofs are not transferable authority.

## Operational handoff and recovery

The [nonproduction acceptance plan](product-delivery-acceptance.md) specifies
positive/negative cases and the exact packet inputs. The selected canary and
authenticated diagnostic establish the compatibility finding above; a complete
merge packet, genuine positive observer and runtime delegation remain pending.
The local managed environment has no configured runtime credentials/outbound
identities. App key access is controlled by the selected GitHub environments
and their reviewed workflows. This document grants no merge, public-release,
production-deployment or repository-administration authority.

Next work is review of the App-grant configuration and mint evidence, corrected
native preflight, and independent approval of concrete exact stage packets from
the final installation.
A first-target dependency with unverified live prerequisites stays blocked.
Do not manufacture a grant from fixture issuers, matching hashes or templates.

Before resuming, read current main, the delivery PR's exact head/checks/reviews,
CI-owned state and any operator reply. Verify each post-merge observation refresh,
preserve all original goals/counters and reconcile uncertainty without resending.
External recovery evidence is under `/workspace/work/exacly-product-delivery/`:
`state.json`, source guards, review logs, QA reports and
`watchdog-investigation/REPORT.md`. The selected diagnostic evidence is retained
under `merge-acceptance-bootstrap/`; it is distinct from runtime merge authority
and from the controlled implementation evidence.
