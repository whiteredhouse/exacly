# Unattended verification implementation checkpoint

Recovery point: 2026-11-01, following roadmap [#87](https://github.com/whiteredhouse/exacly/pull/87).
**The REQ-034–039 target is incomplete.** Source implementation and controlled
acceptance have been delivered and merged; the independently approved live pilot
remains outstanding. Current main observations have published successfully, as
rechecked below. This checkpoint replaces the
earlier historical snapshots. Merged code, failed baseline CI and skipped live cases are
not successful live acceptance.

## Current revisions and requirement status

The rechecked main baseline is
[`3d8e608b3f2e7f6bd30c47699c7bbcbff1e1b992`](https://github.com/whiteredhouse/exacly/commit/3d8e608b3f2e7f6bd30c47699c7bbcbff1e1b992),
the state refresh after #103 at `c27a098572629c9a741bf2f0bef5a8bc168dd68d`.
Coordinator/retained-proof [#100](https://github.com/whiteredhouse/exacly/pull/100),
semantic binding [#101](https://github.com/whiteredhouse/exacly/pull/101), and
workflow/history correction [#102](https://github.com/whiteredhouse/exacly/pull/102)
are merged. #102 final head
[`48cf0762e40f07878a15b1b47b4722429e92c277`](https://github.com/whiteredhouse/exacly/commit/48cf0762e40f07878a15b1b47b4722429e92c277)
passed [CI 41265791427](https://github.com/whiteredhouse/exacly/actions/runs/41265791427):
4,114 tests, 15 explicitly unconfigured live skips, 1,222.91 seconds;
plan and validation also passed. Current reviews/threads were empty and merge
state was clean. Those historical checkpoint PRs changed documentation only;
their protected code/model closure matched the consumer seed below. Re-read current
main, its actual state job, the consumer ref and operator replies before resuming.

| Requirement | Lifecycle / evidence boundary |
| --- | --- |
| REQ-034 | Active: durable native controller and same-domain process recovery delivered; supported-runtime CI and current-main observations passed. |
| REQ-035 | Active: independently selected standing scope, exact native grants and original-goal authoritative budgets delivered; CI and current-main observations passed. |
| REQ-036 | Active: fixed worker, immutable inputs, kernel resource envelope and real supported-runtime cases delivered; CI passed. No coding-agent support is claimed. |
| REQ-037 | Proposed: concrete atomic Git publication and exact producer selection delivered; actual approved live publication evidence pending. |
| REQ-038 | Proposed: supported single-consumer/single-producer association and native completion delivered; actual approved live acceptance pending. #101 added the authenticated live E2E observation before the pilot's semantic pin. |
| REQ-039 | Proposed: coordinator and retained-evidence verifier delivered; actual approved live acceptance and all declared cases pending. |
| REQ-040–043 | Separate product-delivery target; implementation and acceptance are tracked in [its own checkpoint](product-delivery-progress.md), under separate authority. |

Independent review mapped all six unchanged REQ-038 criteria to the declared
single-consumer/single-producer profile. Composition is conditional on the
selected workflow under `spec/exacly/README.md`; unsupported members, targets,
aggregate mappings and foreign namespaces fail before effects. No aggregate,
multi-target or distributed support is claimed. Controlled tests alone cannot
establish the live-bound requirements.

## Delivered PRs and validation

Each substantial slice received independent technical review. Final exact-head
checks, current GitHub reviews and unresolved threads were inspected before
merging; no blocking findings remained and no rule/check/review bypass was used.
Effective main ruleset `26028163` enforces deletion and non-fast-forward
protection only, with no required status-check or GitHub review rule. Agent review does not replace any required
GitHub approval.

| Slice | PR and merge | Final exact-head evidence |
| --- | --- | --- |
| Objective accounting | [#88](https://github.com/whiteredhouse/exacly/pull/88), [e9d03a7](https://github.com/whiteredhouse/exacly/commit/e9d03a721b497907aedc29cb1566d87921cec456) | Full CI passed; accounting alone grants no authority. |
| Immutable worker inputs | [#89](https://github.com/whiteredhouse/exacly/pull/89), [4fa40c9](https://github.com/whiteredhouse/exacly/commit/4fa40c95ec8567ed592a8c084036b002d368c40d) | Full CI passed. |
| Native output association | [#90](https://github.com/whiteredhouse/exacly/pull/90), [748487f](https://github.com/whiteredhouse/exacly/commit/748487f5d9c1d4078091cfa6384763acf2df742a) | Full CI passed. |
| Correlated remote protocol | [#91](https://github.com/whiteredhouse/exacly/pull/91), [54536a9](https://github.com/whiteredhouse/exacly/commit/54536a90408ca2d1fa78b5cd3e9291bfb7012d8f) | Full CI passed. |
| Controlled native integration | [#92](https://github.com/whiteredhouse/exacly/pull/92), [ac76acf](https://github.com/whiteredhouse/exacly/commit/ac76acfdbfe78e4dfed587b74a314b1da9f20fab) | 3,531 tests passed, including real isolation with controlled remote/CI responses. |
| Host/consumer decision checkpoint | [#93](https://github.com/whiteredhouse/exacly/pull/93), [361fd1a](https://github.com/whiteredhouse/exacly/commit/361fd1ac910f13efe48422182233720676a5d80c) | Documentation only; no live approval implied. |
| Kernel resource integration | [#94](https://github.com/whiteredhouse/exacly/pull/94), [b2e429c](https://github.com/whiteredhouse/exacly/commit/b2e429c195d4e73ef8f30e79e72f373b03919d54) | [41256034548](https://github.com/whiteredhouse/exacly/actions/runs/41256034548): 3,585 passed, including mandatory real resource/lifecycle cases. |
| Bounded runtime installation | [#99](https://github.com/whiteredhouse/exacly/pull/99), [0f7bf45](https://github.com/whiteredhouse/exacly/commit/0f7bf455c710797ea900f9a2daa72dd941d41bb4) | [41261428463](https://github.com/whiteredhouse/exacly/actions/runs/41261428463): 3,593 passed. Noninteractive finite apt/profile setup preserves fail-closed isolation checks. |
| Standing scope | [#95](https://github.com/whiteredhouse/exacly/pull/95), [f085c11](https://github.com/whiteredhouse/exacly/commit/f085c11533ac1adcae8c1f36878bf49b867996af) | [41262683169](https://github.com/whiteredhouse/exacly/actions/runs/41262683169): 3,717 passed. |
| Concrete Git transport | [#96](https://github.com/whiteredhouse/exacly/pull/96), [689a377](https://github.com/whiteredhouse/exacly/commit/689a3774424a17043274c4b2b48ad300473b64ec) | [41262720550](https://github.com/whiteredhouse/exacly/actions/runs/41262720550): 3,807 passed; one separate live case unconfigured. |
| Fixed fixture producer | [#97](https://github.com/whiteredhouse/exacly/pull/97), [578bc03](https://github.com/whiteredhouse/exacly/commit/578bc0398e5a47f195824579250143fb443e1706) | [41262757758](https://github.com/whiteredhouse/exacly/actions/runs/41262757758): 3,816 passed; one separate live case unconfigured. |
| Native workflow controller | [#98](https://github.com/whiteredhouse/exacly/pull/98), [8ec168e](https://github.com/whiteredhouse/exacly/commit/8ec168e5b062a4b3bcd3c21d766f566c09716611) | [41262883879](https://github.com/whiteredhouse/exacly/actions/runs/41262883879): 3,915 passed; one separate live case unconfigured. |
| Bounded coordinator and retained proof | [#100](https://github.com/whiteredhouse/exacly/pull/100), [19e8c7b](https://github.com/whiteredhouse/exacly/commit/19e8c7b6f131d2ecee3ea99523ff398a592a1932) | [41263153766](https://github.com/whiteredhouse/exacly/actions/runs/41263153766): 4,090 passed; 15 explicitly unconfigured live skips. |
| Final REQ-038 live observation binding | [#101](https://github.com/whiteredhouse/exacly/pull/101), [1bb8081](https://github.com/whiteredhouse/exacly/commit/1bb80813ce0e04ae7fcb0a5afbac9377363d5026) | [41264524764](https://github.com/whiteredhouse/exacly/actions/runs/41264524764): 4,090 passed, 15 unconfigured live skips, 1,042 seconds; all PR checks green. |
| Workflow initialization and rejected-history correction | [#102](https://github.com/whiteredhouse/exacly/pull/102), [25f075a](https://github.com/whiteredhouse/exacly/commit/25f075ad680116a0ad3cd4d084ab021014ff2be2) | [41265791427](https://github.com/whiteredhouse/exacly/actions/runs/41265791427): 4,114 passed, 15 unconfigured live skips; all PR checks passed. |

The final coordinator's controlled positive uses actual production bootstrap,
fixed worker, native phases and real standing authorization with all 12 declared
case proofs. Its public verifier succeeds after the private stores and keys are
hidden. Physical/API/approval seams remain controlled in that test: this is not
live GitHub acceptance. Real supported-runtime tests remain mandatory in full CI;
the development subset is not a substitute.

#101's GitHub merge state was `unstable` solely because the same source SHA was
also used for the intentionally failed fixture baseline. All PR checks were
green; no effective required-status/review rule was bypassed. Its PR body records
that distinction. The fixture failure is not a regression in the Exacly suite.

## Fixed pilot and actual failed baseline

The selected nonproduction consumer is `whiteredhouse/exacly`, immutable repository
ID `1411700094`, exact branch `refs/heads/pilot/req039-consumer`. It currently
points to seed
[`7533c740df148caf4ed7b972e3b04f2e6f384260`](https://github.com/whiteredhouse/exacly/commit/7533c740df148caf4ed7b972e3b04f2e6f384260),
which has the exact merged #102 tree and no generated result. It fast-forwards
the previous seed through an ordinary two-parent commit; the expected old ref
was checked and no force update was used. Do not overwrite branch drift.
Trusted branch setup is separate from delegated runtime publication authority.

The dedicated push producer's actual [run 41267206554, attempt 1](https://github.com/whiteredhouse/exacly/actions/runs/41267206554)
failed at the fixed fixture test because
`fixtures/unattended-pilot/results/source-digests.json` is absent
(`FileNotFoundError`). Packaging and upload succeeded. The retained baseline
artifact is `13066964392`, named `exacly-req039-evidence-41267206554-1`, digest
`sha256:726c8a7081710c8a8595638b081ed533db1961a97b75a50b006d36d35c279608`.
This is authentic failed-CI evidence, not completed work or live target acceptance.
These are the final prepared base and failed producer inputs for this closure.
Earlier baseline selections are superseded. Never seed a manufactured successful result.

Worker authority is restricted to the reviewed `snapshot-inventory-v1` recipe,
reading only `fixtures/unattended-pilot/inputs/source.txt` and writing the exact
result path above. Engine, verification tests, workflows, policies, consumer model
and observer closure remain protected. The existing main-only
`.github/workflows/verification-evidence.yml` remains unchanged. The separate
`.github/workflows/req039-evidence.yml` is push-only for the exact pilot branch,
with one `observe` job and the fixed verifier.

Only the dedicated pilot policy proposes `branch_protection: not_required`.
Defaults, every other admission check and repository rules remain intact.
Admission receipts must report `verified: false` and no protection-check timestamp.
The concrete candidate policy is
[`req039-producer-policy.candidate.json`](req039-producer-policy.candidate.json),
prepared by native `admission.propose_policy` from actual immutable GitHub objects
and independently checked against the seed. It is **not approved**.

- Policy digest: `sha256:878a0123ca39acafe5a72d35f1ac672396bae5a3d5a0f398c18ccc39cfff06e4`.
- Observer digest: `sha256:2013ef17a6bd710b334da85d02ef8782fd60ed945e2014e2f1476b51b0f4bd2b`.
- Model digest: `sha256:280ffeb05ca09775b558a493e3dd627636f7c5ab875fadf0e7f7c43af6ec23f5`.
- Producer workflow digest: `sha256:4725b3ac34180542f84022ec19b54d89b87264c2d45899f25ef2438b4434e82d`.

This supersedes #101's historical candidate. Final per-run delegation needs the
actual initialized stores, host boot/cgroup and publishing identity; those facts
can only be produced by protected setup on the actual runner. The coordinator
retains that concrete packet for independent human approval before effects.
Hashes, templates and technical review grant no authority.

The coordinator also produced [run 41265307970](https://github.com/whiteredhouse/exacly/actions/runs/41265307970)
as an immediate push-event workflow failure: its job-level environment referenced
the unavailable `runner` expression context. #102 initializes the paths in a
first step with valid step environment and `GITHUB_ENV`. Its narrowly closed
history handler recognizes only the exact known rejected workflow digest, a
fresh terminal first push attempt, zero jobs and zero artifacts; it does not
classify an executed or uncertain launch as absent. All five actual historical
API probes passed the production helper.

Independent review cleared the correction. Eight workflow cases and 79 runner
cases pass; the combined regression run passed 169 cases with 14 explicitly
unconfigured live skips. Ruff, formatting, strict mypy, model conformance and
impact review pass. Final exact-head supported CI passed 4,114 tests with 15
explicitly unconfigured live skips before merge, including mandatory real
isolation/resource cases.
The correction source is in `/workspace/work/exacly-workflow-fix` and
`/workspace/work/exacly-history-fix`; copied older dependencies are not authoritative.
The corrected consumer producer ran successfully through packaging/upload and
failed only at its expected missing-result check. No new invalid coordinator
push run appeared. The updated operations proposal remains non-authorizing.

## Precise live blockers and approval procedure

The operational proposal in [unattended-pilot-approval.md](unattended-pilot-approval.md)
lists the full refs, bounds, credentials, approval and recovery contract.
The remaining access and authority requirements are:

1. Configure Actions secret `EXACLY_REQ039_PUBLISH_TOKEN` as a non-GitHub-Actions
   App installation token scoped to `repository_ids: [1411700094]`, with
   `contents: write`, `actions: read`, `issues: write` and implicit metadata read.
   Do not add actions-write, workflow-write or administration permissions.
   GitHub's contents-write access is repository-wide; exact refs and the absence
   of merge/release/deployment authority are enforced by approved native grants,
   not a narrower mint scope.
   Configure nonsecret variables `EXACLY_REQ039_PUBLISH_APP_ID`,
   `EXACLY_REQ039_PUBLISH_INSTALLATION_ID` and `EXACLY_REQ039_PUBLISH_EXPIRES_AT`.
   Independent review must confirm actual mint permissions and expiry covering
   the whole unit. This session's GitHub connection is not an Actions credential.
   Do not send the token in chat or put credentials/keys in artifacts or arguments.
2. On the final reviewed main revision, manually dispatch the dedicated
   `req039-coordinator.yml` job with exact `base` and optional `baseline_run`
   inputs: `base=7533c740df148caf4ed7b972e3b04f2e6f384260` and
   `baseline_run=41267206554`. The current tools do not provide workflow dispatch.
   First confirm the consumer still points to this exact base and current main
   has the same protected code/requirement semantics. Never use stale pins.
3. Download the generated public review artifact and independently review the
   actual producer policy, delegation, component configuration, original goal,
   host boot/cgroup, initialized stores, exact refs, budgets and credential mint facts.
   The configured human approver is GitHub user ID `1679024` (`p1c2u`). Post the
   exact generated original approval body in [PR #93](https://github.com/whiteredhouse/exacly/pull/93).
   Approval waits at most 900 seconds, so start only when the reviewer is ready.
   An agent/App must not approve on the human's behalf. Original unedited approval
   and immutable retained artifact fence are verified before native authority.

Missing credentials or approval block live effects only. No actual delegated
objective has run and no live acceptance has been claimed. No routine per-phase
approval is needed after the exact standing contract is independently approved.
The publisher may update only the existing pilot branch and individually granted
`refs/tags/req039-operations/...` marker. No main update, arbitrary tag, deletion,
merge, release or deployment authority is granted.

The coordinator is one dedicated `ubuntu-24.04` job. Its 45-minute job envelope
contains a 1,800-second service with one CPU, 1 GiB memory, no swap and 128 tasks;
approval waits at most 900 seconds. The proposed original-goal accounting capacity
is 160 conservative 45-second units (7,200,000 ms), with at most 20 paid producer
reads including uncertain calls. Model requests/spend are zero. Capacity units
are not an API request count and do not replace the physical deadline.

Protected journals remain on the same runner through controller-process recovery.
Runner loss ends this supported domain: do not recreate journals, accounting or
the approved original objective on another runner. A persistent Linux host is not
required for this explicitly bounded acceptance. The current cloud environment
cannot enforce the full Bubblewrap/cgroup boundary; never relax it for a live run.

## Current-main observed state

Rechecked on 2026-11-01: CI-owned committed state is serial `67`, config
revision `c27a098572629c9a741bf2f0bef5a8bc168dd68d`, with observations at
`2026-11-01T08:41:13Z` from successful
[Spec run 41267330287](https://github.com/whiteredhouse/exacly/actions/runs/41267330287).
REQ-034/035/036 have passed observations. REQ-037/038/039 remain inconclusive
because required live cases were unconfigured/skipped; successful component
coverage does not establish their authentic live acceptance.

The old runtime-installation queue blockage, including run `41253365811`, is a
historical incident, not a current state-publication blocker. Do not cancel runs
based on an old checkpoint. Recheck actual heads and jobs before recovery and
preserve the canonical state writer; never hand-edit observed state.

## Exact recovery and completion sequence

1. Re-read remote main, consumer branch, open PRs, actual CI and operator replies.
   Preserve local changes and the original `/workspace/exacly` checkout; use
   isolated worktrees under `/workspace/work` and never reset the original
   checkout. Its local branch is not a substitute for rereading remote main.
   Native read-only Git fetch works through the configured
   proxy; authenticated repository operations use the GitHub connector.
2. All first-target implementation/checkpoint PRs through #103 are merged.
   The historical final handoff worktree is
   `/workspace/work/exacly-final-handoff`; original implementation/review worktrees
   remain under `/workspace/work`. Remote committed code is authoritative.
   Preserve #101's semantic bindings and the exact fixed verifier.
3. Confirm the current-main canonical state job publishes the current config
   revision/coverage, or retain the precise queue blocker. Publication is CI-owned.
4. Confirm the exact consumer base, failed baseline and policy above. If protected
   code, verifier, criteria or bindings changed, regenerate the seed/baseline/pins
   and obtain new independent approval. Never overwrite branch drift or substitute
   a latest-green run. This documentation-only checkpoint does not change those pins.
5. Configure the publisher, dispatch the single bounded coordinator and obtain
   independent approval of its actual packet before effects. Preserve all private
   journal identities and uncertain reservations through same-runner process recovery.
6. Retain real isolated execution, native dispatch/handoff, exact atomic publication,
   selected producer run/attempt, fresh admission and native completion/reobservation.
   Verify all 12 declared cases, including restart/duplicate handling, lost-response
   discovery, failed/unavailable/wrong revision or attempt evidence, changed expected
   criteria/verifier, read-only ref precondition conflict, revocation, genuine expiry
   and original-account exhaustion. Controlled cases and skips do not satisfy this.
7. Only after successful authentic live verification, commit the non-authoritative
   `verification-references/req039.json` selector, re-verify current code/requirement
   semantics, original approval and fresh native admission, and activate delivered
   REQ-037–039 and related capability/process records based on that evidence. Let CI
   publish observations. That verification approval grants no product-delivery
   authority. Separately authorized implementation of REQ-040–043 proceeds under
   [its own checkpoint](product-delivery-progress.md); its operational approvals
   and evidence remain independent.

Current final verification checklist:

- [x] Model conforms and impact reviewed for the implementation, #101 and #102.
- [x] Final #100 exact-head supported suite passed, with real isolation/resource cases mandatory.
- [x] #101 final exact-head CI passed and merged.
- [x] #102 final supported CI passed and the correction merged.
- [x] Final consumer seed, authentic failed baseline and concrete policy proposal are retained.
- [ ] Actual runner packet and delegation are independently approved before native effects.
- [x] Current-main observed state is published by canonical CI (serial 67 at the recheck above).
- [ ] Required authentic live “Check again” acceptance passes with retained evidence.
- [x] Scoped PR descriptions identify changed records and affected OUT-001.
