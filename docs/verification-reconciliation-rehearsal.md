# Live verification and reconciliation rehearsal

On 2026-10-28, admission and the read-only reconciliation API were exercised against the
real [verification producer run 40711348508, attempt 1](https://github.com/whiteredhouse/exacly/actions/runs/40711348508/attempts/1).
The model and implementation were the merged revision
`07d0c7cd1854516e3d542acb0dcb8df4e2e1140e` from [PR #48](https://github.com/whiteredhouse/exacly/pull/48).

The rehearsal explicitly selected that revision's producer proposal, observer paths and six model
config files. This was a session-local rehearsal baseline; no persistent organization approval
policy was published. Generating the policy fingerprints did not itself approve the producer.

## Results

| Check | Observed result |
| --- | --- |
| Producer and artifact | Successful push run; artifact `12825049915`, named `exacly-evidence-40711348508-1` |
| Retained JUnit | 819 cases, zero failures and zero skips |
| Admission | All six checks passed; fresh state contained 19 passed verification assessments |
| Verification gates | Both REQ-019 and the merged version 3 policy's 19 requirements were satisfied |
| Reconciliation | A config-only plan proposed 19 actions; previewing REQ-019 returned `ready` |
| Preview validation | Public schema and report digest matched; inputs remained unchanged |

The preview used a new live source instance and made 26 production GitHub API reads, including
fresh exact-attempt, branch, workflow, job and artifact checks. It did not reuse metadata from the
earlier admission invocation. Approved model content was fetched at its immutable revision.
The resulting report included all 19 readiness checks separately from the selected action's result.
The existing merged model was used for this rehearsal; it predates the new reconciliation requirement.

Retained artifact identifiers:

- Repository ID: `1411700094`.
- Producer job ID: `122897164770`; workflow ID: `399538948`.
- ZIP size: 18,443 bytes.
- ZIP digest: `sha256:7e8c2dfc000422db5366c2acc83bc60ed748544c517e60c2dc88114e1cd7f299`.
- JUnit digest: `sha256:d6b22f5a5a9252f4be26e26fa1a8d57a0862090b02f9e465d00741879fe5acbf`.
- Preview report digest: `sha256:54972806df51ab99f84fba267304a80d037837b75982b0db1287982e07857684`.

## Transport coverage and limits

The production `GitHubClient` performed the authenticated metadata reads. The production admission
CLI was also exercised: it rejected the request when the managed environment's proxy blocked the
artifact storage connection with HTTP 403. That invocation produced only a rejected receipt.

The successful API rehearsals used the authenticated GitHub connector's artifact download for the
ZIP bytes. Each ZIP digest matched the production client's live artifact-detail response, and the
archive was downloaded again for reconciliation. The inherited proxy and TLS verification were
preserved. No offline-evidence input was added to either command.

These results establish a live rehearsal using the production metadata client and connector artifact
transport. Successful artifact transfer through the production client's storage redirect remains
unverified in this environment.

The preview remains advisory and unsigned. It does not establish test honesty, acceptance coverage,
executor-to-output linkage, delivery readiness or authority to close work. No tasks, observed state
files or approvals were changed. Local raw metadata, ZIPs, plans and reports were retained outside
the repository; no credentials or temporary download URLs are included here.
