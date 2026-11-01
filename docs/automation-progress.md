# Unattended verification implementation checkpoint

This checkpoint covers REQ-034–039 from roadmap PR [#87](https://github.com/whiteredhouse/exacly/pull/87).
It does not authorize or implement REQ-040–043. Incomplete contracts remain proposed.

## Pilot constraint confirmed by the user

Keep the independently approved verifier fixed for the first pilot. Worker changes are limited to
explicit fixture paths outside the protected observer scope. The worker must not change Exacly's
engine, verification tests, workflows or policies. This constraint is separate from the authorized
implementation PRs that develop Exacly itself.

Use a root-level fixture namespace such as `fixtures/unattended-pilot/inputs/` and
`fixtures/unattended-pilot/results/`, with exact selected files and output mappings in the future
approved delegation. Do not put mutable pilot fixture output beneath `ci/`: the current observer
closure protects that directory. Protect the consumer model and criteria too, even if their files
are stored beside fixture data. Candidate inputs cannot choose a different observer or policy.

The user selected consumer `whiteredhouse/exacly`, exact branch `pilot/req039-consumer`, and one
dedicated GitHub-hosted `ubuntu-24.04` coordinator job. Demonstrate controller-process recovery with
protected journals on that same runner. Runner-loss recovery and resuming the objective on another
runner by recreating journals are explicitly excluded. This bounded local-machine scope does not
require a persistent Linux host.

The user authorized a separate push-only, single-job producer for that exact branch using the fixed
verifier. Leave the existing main-only producer unchanged. Prepare concrete producer policy,
delegation and publishing-identity requirements for independent approval, listing exact permissions,
refs and credential configuration. The session's GitHub connector is not an Actions credential.
Missing publishing credentials block the live run only; continue independent implementation.
The user's consumer/host choice does not itself independently approve the final policy/delegation.

The user explicitly selected proposed `branch_protection: not_required` only for the dedicated
`pilot/req039-consumer` policy. Preserve every other check, the default behavior and repository rules;
receipts must report protection unverified with no check timestamp. Final policy/delegation still
require independent approval before live execution.

## Baseline and decisions

- Inspected remote main `461b32fe1e35a8a7f89dde3b74103c7856dbfb62` and open PRs on 2026-10-31; there were no open PRs.
- Original checkout `/workspace/exacly`, branch `work`, was clean and is preserved. Work uses isolated worktrees under `/workspace/work/`.
- Read `skills/exacly/SKILL.md`, `spec/exacly/unattended-automation.yaml`, `automation.yaml` and the model README. No `AGENTS.md` files were present.
- Baseline `show REQ-035`, `trace REQ-035`, `trace REQ-038` and `gaps` conform with no errors or warnings. REQ-034–043 have no executed acceptance coverage on baseline.
- Existing real local execution/handoff plus controlled admission/completion integration is in `python/tests/test_handoff_end_to_end.py`. Its controlled GitHub source is not live evidence.
- Keep component journals authoritative. New coordination cannot replace native current-state, binding, authorization, admission or completion checks.
- Start with objective accounting; develop exact remote publication, immutable worker snapshots and current-state output association independently. Integrate only after review.

## Current work

| Scope | Status | Next action |
| --- | --- | --- |
| REQ-035 / APP-021 / INFO-012 / INF-008 | [PR #88](https://github.com/whiteredhouse/exacly/pull/88) merged; 53 focused tests and exact-head full CI passed; independent technical review found no blockers | Implement independently selected standing scope, expiry/revocation and native effect grants; accounting alone grants no authority |
| REQ-037 / APP-023 / INFO-011 | [PR #91](https://github.com/whiteredhouse/exacly/pull/91) merged; 39 tests and exact-head full CI passed; independent review passed after directory durability correction | Implement concrete CAS transport with independently established operation proof, approved CI selection and live acceptance |
| REQ-036 / APP-022 | Fixed recipe now bound to native exact resource policy, checked kernel cgroup limits and mandatory real runtime cases; 86 unit/snapshot tests and independent review pass on `codex/unattended-resources` | Supported Ubuntu CI must execute the mandatory real resource/lifecycle cases before merge; lifecycle describes the delivered contract, not a successful observation claim |
| REQ-038 / APP-020 | [PR #90](https://github.com/whiteredhouse/exacly/pull/90) merged; 25 tests and exact-head full CI passed; independent review passed after directory durability correction | Integrate whole-workflow recovery and any required composition/output mapping; single-producer native completion now has controlled integration coverage |
| REQ-034 | Not implemented | Integrate native phases, approved scope, accounting and recovery after the primitives above |
| REQ-039 | Consumer/ref and same-runner coordinator selected; implementation and independent policy/delegation approval remain outstanding | Deliver separate fixed-verifier producer and concrete approval packet; unavailable publishing credentials block live execution only |

The focused code PR stack is [#88](https://github.com/whiteredhouse/exacly/pull/88)
accounting (`03798bc85b11375367e9c35385eb0d9934868991`),
[#89](https://github.com/whiteredhouse/exacly/pull/89) snapshots
(`7720508cd7af038b7700e2c81428aa5db53ec589`),
[#90](https://github.com/whiteredhouse/exacly/pull/90) association
(`4d1d00fe4bfddb2b96664c65e836e14a80bf506a`) and
[#91](https://github.com/whiteredhouse/exacly/pull/91) remote protocol
(`c4d8e1b4eb7b88fbce8b9072dc8fadb7f33aee78`), followed by
[#92](https://github.com/whiteredhouse/exacly/pull/92) integration
(`5bcceaeeaa9fb65bc291691f63db3744a3cd7b77`). All five final heads passed `check`,
`plan` and `validate`; all five PRs were merged in dependency order after fresh GitHub merge-state,
review and thread inspection. No blocking GitHub reviews or threads were present, and no bypass was used.

| PR | Merge commit | Exact-head full check |
| --- | --- | --- |
| #88 | [e9d03a7](https://github.com/whiteredhouse/exacly/commit/e9d03a721b497907aedc29cb1566d87921cec456) | [run 41252040463](https://github.com/whiteredhouse/exacly/actions/runs/41252040463), 3,429 tests |
| #89 | [4fa40c9](https://github.com/whiteredhouse/exacly/commit/4fa40c95ec8567ed592a8c084036b002d368c40d) | [run 41252157970](https://github.com/whiteredhouse/exacly/actions/runs/41252157970) |
| #90 | [748487f](https://github.com/whiteredhouse/exacly/commit/748487f5d9c1d4078091cfa6384763acf2df742a) | [run 41252320226](https://github.com/whiteredhouse/exacly/actions/runs/41252320226) |
| #91 | [54536a9](https://github.com/whiteredhouse/exacly/commit/54536a90408ca2d1fa78b5cd3e9291bfb7012d8f) | [run 41252408011](https://github.com/whiteredhouse/exacly/actions/runs/41252408011) |
| #92 | [ac76acf](https://github.com/whiteredhouse/exacly/commit/ac76acfdbfe78e4dfed587b74a314b1da9f20fab) | [run 41252506469](https://github.com/whiteredhouse/exacly/actions/runs/41252506469), 3,531 tests |

The merged controlled integration connects the actual fixed worker recipe and native
executor/handoff/association/completion to controlled remote and producer responses. It passed in
the supported Ubuntu runtime in #92, with wrong-revision rejection, a distinct fresh controlled run,
exact native replays, preserved unrelated state and exhausted original-objective budgets after
binding supersession. This is actual isolated execution and controlled integration, not REQ-039
live remote/producer evidence. The local development subset separately passed 2,990 tests.

Independent reviews checked each substantial slice. Findings about remote source drift, sticky
conflicts and directory durability were corrected before merge. Association and remote journals now
synchronize before effects and before successful receipt/replay exposure; fault tests cover failed
barriers. Reviews of the accounting and snapshot subsets found no blockers within their declared scope.

## Observed-state publication is still pending

At the last read after #92 merged, main was `ac76acfdbfe78e4dfed587b74a314b1da9f20fab`.
Its [Spec run 41253411322](https://github.com/whiteredhouse/exacly/actions/runs/41253411322)
and [evidence run 41253411329](https://github.com/whiteredhouse/exacly/actions/runs/41253411329)
had started. The committed `spec/exacly/state/main.json` still had serial 66 and config revision
`5ba9e66a713a526e913cad84ab9ef2d45d201856` (written `2026-10-31T19:00:24Z`).
This is a direct remote read, not a successful publication claim. Earlier main runs may be superseded;
the workflow queues state jobs and only a publication for current main establishes fresh observations.
Check current main and its state job before treating any observation as refreshed. A later checkpoint
documentation merge also starts its own main run. Never edit state to close this gap.

REQ-036/APP-022 now describe the delivered fixed-command resource integration; actual supported-runtime CI must verify it. REQ-034–035, REQ-037–039 and CAP-004/PROC-004 remain proposed. Complete future selectors remain required:
accounting tests cannot establish standing scope, recipe execution cannot establish resource
containment, and controlled transport cannot establish live publication. REQ-040–043 are unchanged.

## Validation and external blockers

The cloud host cannot enforce the required local isolation boundary. The actual preflight
`python3 ci/check_isolation_runtime.py` fails with `bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted`
and the namespace probe fails with `/proc/self/uid_map: Read-only file system`. Cgroup v2 is read-only;
this host also cannot provision the CPU, memory and process envelope required by REQ-036.
Do not relax the boundary. The existing disposable Ubuntu CI jobs provision and check the supported
Bubblewrap runtime; real worker acceptance must use a supported runtime with declared resource bounds.

Shell `git fetch origin` fails because the session HTTP proxy cannot be reached. The GitHub connector
can read remote main, open PRs and repository rules, and is the available repository API transport.
Use it for remote work while preserving exact SHA checks. Do not disable or bypass the proxy.

The live consumer is `whiteredhouse/exacly` on `pilot/req039-consumer`; coordination is bounded to
one dedicated `ubuntu-24.04` Actions runner. Exact initialized journals must remain on that runner
through process recovery. Independently approved delegation/producer policy and a configured
trusted publishing credential are still required before REQ-039. A new separate push-only,
single-job branch producer is authorized; the existing main producer stays unchanged. Policy
approval cannot be inferred from a copied template, filename, hash or this session's connector.

The accounting foundation intentionally has no hot-journal recovery or migration API. Preserve
database and sidecar after interruption and escalate; replacing either the store or trusted pin
cannot be used to reset objective capacity.

The remote protocol deliberately requires an independently established operation identity. Ordinary
GitHub REST ref updates lack atomic expected-old CAS, and plain refs do not prove operation correlation.
A concrete transport must solve both without merely echoing its inputs. Atomic leased Git publication
with a separately approved operation-marker ref is a possible next design; the current grant does not
authorize the extra ref. No remote source-publication implementation or live success is claimed yet.

Repository ruleset `26028163` enforces deletion and non-fast-forward protection with no bypass actors.
The classic branch-protection endpoint returns HTTP 403 for this integration; merge readiness must
also be checked from current PR metadata and checks. Never infer permission to bypass a missing view.

## Recovery

1. Read this checkpoint and inspect remote main and all open PRs again before resuming.
2. Inspect `git status` in each worktree; preserve uncommitted work. The five implementation worktrees are clean at the exact remote heads listed above. `/workspace/work/exacly-integration` contains old untracked import copies; the committed test on main is authoritative. Do not overwrite current source from those copies or reset the original checkout.
3. First verify current main's actual CI state publication, including state serial/config revision, current requirement lifecycle and matching coverage. The repository checkpoint update is documentation, not observed state.
4. Use the selected consumer/ref and dedicated same-runner coordinator; do not ask for a persistent host or repeat those decisions. Keep the approved verifier fixed and worker changes confined to exact fixture paths outside its closure. Prepare the separate branch producer and exact policy/delegation/credential packet for independent approval. Missing credentials block live execution, not implementation.
5. Next engineering slice: trusted standing-scope evaluation, expiry/revocation and native exact grants around the existing objective-accounting store. Then implement the controller's durable phase correlation/recovery, concrete leased/correlated remote adapter and exact approved run selection, and resource envelope. Use the delivered controlled integration as the native API seam; it does not substitute for these contracts.
6. Add the remaining fault and live acceptance coverage, independently approve the exact pilot configuration, and run the real nonproduction end-to-end and negative/recovery cases with retained evidence before activating complete contracts.
7. For each next PR, update model/bindings accurately, validate/impact/test, obtain independent technical review, require exact-head CI and GitHub reviews, merge without bypass, then verify actual CI publication and update this checkpoint.

The unattended verification target is incomplete. REQ-036 is delivered and active; REQ-034–035 and REQ-037–039 remain proposed until their complete coverage is delivered. No live acceptance, release or deployment is claimed.

Resource PR #94 first full CI head `b25941c0a9ec545868c899117b83f559868eaee3` executed all five real
resource/lifecycle cases successfully; total 3,584 tests passed and one development-subset contract
test failed because its explicit backend-module list had not added the two new mandatory runtime
modules. The expected list is corrected without excluding anything from full verification. Fresh
exact-head CI is required before merging; the failed run is not a successful model publication.


## Bounded installer recovery checkpoint (2026-11-01)

Open implementation PRs #95–98 have cleared independent source review but their
full checks are stalled in the existing isolation-runtime installation step.
The older main Spec run 41253365811 (association merge 748487f5d9c1d4078091cfa6384763acf2df742a)
has state job124693321007 stuck in that same step since00:20UTC. Later main
state jobs remain queued; committed state is still serial66/config5ba9e66.
No current publication or successful open-PR full check is claimed.

The installer now uses noninteractive sudo, explicit GNU timeout TERM/KILL bounds,
finite mirror retries/HTTP timeouts and bounded package-lock waits. Exact packages,
vendor-profile refusal, scoped AppArmor policy load and before/after enforcement
checks remain mandatory. Timeout or package/profile failure fails setup and the job;
a partly installed disposable host is not salvaged as a supported runtime.
The existing main-only verification-evidence.yml bytes are unchanged.

After exact-head CI passes and this focused fix merges, refresh #95–98 onto it
without changing their independently reviewed implementation bytes, require fresh
exact-head CI/reviews, and merge prerequisites in order. The bounded coordinator
and retained-proof checker are staged separately and still need final integrated
review/CI. Actual live execution additionally requires the independently approved
run-specific packet and a real scoped App publishing credential.


## Standing scope implementation checkpoint (2026-11-01)

Resource PR [#94](https://github.com/whiteredhouse/exacly/pull/94) merged as
`b2e429c195d4e73ef8f30e79e72f373b03919d54`. Final head
`faa60a32341fa78e19f84ac25ea030e8b3e658a5` passed the full supported Ubuntu
CI suite: 3,585 tests, including all real resource/lifecycle cases, plus lint,
types, model refresh/validation and verification gate. The earlier failed head is superseded.
Main observed-state publication remains to be checked against current main; merged code is not publication.

The standing authorizer now evaluates a protected operator-selected independent delegation,
current exact native facts and independently approved native component configuration.
Every effect and bounded read reserves from the original goal's single accounting store;
uncertainty retains reservations. A goal-wide fence spans policy files and all write adapters.
Expiry/revocation deny new effects; separately approved recovery and exact cancellation remain metered.
The native configuration digest is mandatory, preventing provider self-reports from approving
a different installed implementation. See `docs/standing-authorization.md`. Independent technical
review cleared the final implementation and an unchanged cross-policy adversarial repro
(177 repository tests plus that repro passed). Exact PR-head supported CI is still required.
REQ-035, APP-021 and INFO-012 remain proposed pending complete native controller acceptance;
the full completion selector remains present so accounting and scope tests alone cannot establish it.

The user selected `branch_protection: not_required` only for the dedicated
`pilot/req039-consumer` admission policy. Defaults and repository rules stay unchanged.
All other admission checks, fixed observer pins, fixture restrictions and bounded delegation
remain mandatory. Receipts must report that branch protection was not verified.
The finalized policy/delegation still require independent approval before any live effects.
Missing publishing credentials block the live run only.

Next: merge the independently reviewed concrete remote transport, fixed pilot producer,
whole-workflow controller and bounded coordinator in focused PRs, then finalize the exact
run-specific approval packet and verify the declared live recovery/negative cases.
Preserve every existing private journal; never recreate the original approved goal on another runner.


Runtime setup PR #99 merged as 0f7bf455c710797ea900f9a2daa72dd941d41bb4 after exact-head supported CI passed 3,593 tests and all other checks. This standing-scope head inherits that fix through an ordinary merge; reviewed implementation bytes are unchanged. Fresh exact-head CI remains required. Main state publication is still behind the older queued jobs; no fresh publication is claimed.


## Concrete remote bridge checkpoint (2026-11-01)

Standing-scope PR [#95](https://github.com/whiteredhouse/exacly/pull/95), head
`3440d6854a997bd119fde581e2fae1cd5c7fd08f`, is independently reviewed and
awaiting exact-head full CI/merge at this checkpoint.

The concrete remote bridge now uses explicit atomic Git receive-pack old/new commands,
not porcelain push's up-to-date shortcut. Real local receive-pack tests demonstrate
expected-old enforcement even when another actor already moved the branch to the desired
commit. Publication atomically creates a separately approved HMAC-signed annotated operation
marker; ambiguous response recovery independently reads and verifies it without resending.
An authenticated all-ref rejection remains terminal conflict. The same captured App token's
installation-repositories response must contain exactly immutable repository ID 1411700094.
Only the bounded literal GitHub transport is supported; candidate code cannot select it.

Producer selection pins actual workflow bytes and ID, exact branch/push/source revision,
run/attempt and single-job closure on each bounded poll. There is no latest-green or rerun
substitution. Publication journal lookup validates the actual pinned store before treating
a missing operation as absent. Protected HMAC key bytes never enter journals or exports.
The reviewed adapter has 129 passing controlled/unit cases and one opt-in live case skipped;
the latter is inconclusive, not live verification. REQ-037/APP-023 remain proposed.

Next: install the separate fixed fixture producer and finish controller/coordinator review,
then approve actual run-specific policy/delegation and credentials before live execution.
The selected bounded same-runner scope and dedicated-policy protection opt-out are unchanged.

This bridge head inherits merged bounded installer #99 through the refreshed #95 branch. Reviewed transport bytes are unchanged; fresh exact-head CI remains required.


## Fixed consumer and producer checkpoint (2026-11-01)

The new fixed consumer model is `fixtures/unattended-pilot/model.yaml`, namespace
`req039-consumer`, with one byte-inventory requirement. Its immutable observer is
`verification/req039_inventory_test.py`; worker authority is limited to fixture
input reads and the exact result path outside protected observer/model paths.
No generated result is seeded: the initial producer should authentically fail
for missing output. `ci/req039-producer.json` defines the dedicated push-only,
single-job `pilot/req039-consumer` producer. Existing main evidence workflow bytes
are unchanged. Nine producer/verifier contract tests and native workflow validation
pass; independent technical review clears this scope. Full exact-head CI remains required.

`docs/unattended-pilot-approval.md` records the concrete operational proposal, exact
refs, permissions, non-Actions GitHub App token configuration, pilot-only
`branch_protection: not_required`, same-runner lifetime and independent approval
handshake. It is not an approval or live receipt. Actual per-run journal identities,
policy/verifier/source pins, credential mint facts and delegation must be finalized
in the coordinator packet and independently approved before authority.
REQ-039 and APP-026 remain proposed. Missing credentials affect live execution only.

This fixed-producer head inherits bounded runtime setup #99 through the refreshed prerequisite stack. Producer and verifier implementation bytes are unchanged; fresh exact-head CI remains required.
