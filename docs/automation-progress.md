# Unattended verification implementation checkpoint

This checkpoint covers REQ-034–039 from roadmap PR [#87](https://github.com/whiteredhouse/exacly/pull/87).
It does not authorize or implement REQ-040–043. Incomplete contracts remain proposed.

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
| REQ-035 / APP-021 / INFO-012 / INF-008 | [PR #88](https://github.com/whiteredhouse/exacly/pull/88), final head `03798bc85b11375367e9c35385eb0d9934868991`; 53 focused tests, repository Ruff/format and strict mypy pass; independent technical review found no blockers | Full CI in progress; merge only after final-head checks/reviews pass, then verify observed-state publication. Scope evaluator, revocation and native grants still outstanding |
| REQ-037 / APP-023 / INFO-011 | Exact remote CAS publication foundation in progress on `codex/unattended-remote` | Review retained native handoff provenance and ambiguous outcome recovery |
| REQ-036 / APP-022 | Immutable snapshot and deterministic recipe foundation implemented on `codex/unattended-worker`; 37 tests, Ruff and strict mypy pass; independent technical review found no blockers | Publish and verify focused PR; resource envelope and supported-runtime lifecycle acceptance remain outstanding |
| REQ-038 / APP-020 | Current-state output association foundation implemented on `codex/unattended-association`; 25 tests, Ruff and strict mypy pass; independent review passed after directory durability correction | Publish focused PR and verify final-head CI; composition/output mapping and fresh native workflow completion remain outstanding |
| REQ-034 | Not implemented | Integrate native phases, approved scope, accounting and recovery after the primitives above |
| REQ-039 | Blocked pending implementation and approved pilot configuration | Select and independently approve the exact nonproduction consumer/runtime/policies before any live run |

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

No exact live consumer, canonical coordination host, independently approved delegation, producer
policy or permitted remote destination has yet been declared. These are required before REQ-039.
The existing Exacly producer accepts pushes to main and pins its observer closure; changing it or
claiming branch/PR evidence is not a substitute for independent policy approval.

The accounting foundation intentionally has no hot-journal recovery or migration API. Preserve
database and sidecar after interruption and escalate; replacing either the store or trusted pin
cannot be used to reset objective capacity.

Repository ruleset `26028163` enforces deletion and non-fast-forward protection with no bypass actors.
The classic branch-protection endpoint returns HTTP 403 for this integration; merge readiness must
also be checked from current PR metadata and checks. Never infer permission to bypass a missing view.

## Recovery

1. Read this checkpoint and inspect remote main and all open PRs again before resuming.
2. Inspect `git status` in each worktree; preserve uncommitted agent work. Do not reset the original checkout.
3. Resume the named scoped branch, inspect exact final head, run its focused tests/lint/type checks, and obtain independent technical review.
4. Publish focused PRs with model impact and evidence; merge only after final-head CI and required GitHub reviews pass and blocking findings are resolved.
5. Verify main's actual CI state publication after each merge. Never edit `spec/exacly/state/` manually.
6. Update this file with PR, head, merge and workflow links and exact check results. Continue the target, keeping unit, controlled integration and live evidence distinct.

No requirement activation, live acceptance, unattended completion, release or deployment is claimed by this checkpoint.
