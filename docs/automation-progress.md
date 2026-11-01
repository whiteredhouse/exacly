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

The unattended verification target is incomplete. No requirement activation, live acceptance,
unattended completion, release or deployment is claimed by this checkpoint.
