# Separately authorized pull-request merging

`github-atomic-merge-v1` is a narrow delivery profile for one repository and one
local coordination domain. It uses an independently approved merge grant, a
distinct private delivery journal and a distinct marker key. A planning proposal,
verification grant, handoff receipt or successful check grants no merge authority.

The implementation and controlled tests do not establish authentic GitHub
acceptance. REQ-041 remains proposed until the approved nonproduction acceptance
establishes the provider guarantees this profile requires. Ordinary source PRs
merged while implementing Exacly are not that acceptance.

## Exact provider effect

The subject selects a same-repository pull request, exact head `H`, exact base
`B`, base branch and merge method. The native payload additionally selects the
GitHub-generated merge commit `M`, its tree, the head ref, authenticated marker
ref and reviewed before/after Git-blob inventories for model, criteria, bindings
and trusted observers. `M` must have exactly the approved parents and tree.

The adapter checks the actual repository and actor scope, current required
checks and reviews, branch protection and protected contract files. Required
checks include their producing application identity. Unsupported policies,
unknown bypass semantics, forks, squash/rebase, linear-history requirements and
required merge queues block this profile. The adapter cannot alter protections,
dismiss a review or fall back to a head-only merge request.

### Aggregate state and required-condition evidence

GitHub's aggregate merge state is separate from the results selected by the
branch policy. The existing `CLEAN` preflight and its retained fact shape remain
supported. A fresh `UNSTABLE` preflight is eligible only with additional direct
evidence of the supported requirements; the aggregate value or a merge-box
message grants no authority. Other aggregate states remain blocked.

For that `UNSTABLE` allowance, the adapter requires all of the following in
addition to its existing identity, actor, policy, review and reference checks:

- The independently selected App grant uses schema `0.2.0` and explicitly grants
  Commit statuses read alongside the five existing merge permissions. Legacy
  actors and the five-permission grant remain `CLEAN`-only. The adapter checks
  this capability before making the additional requests; it cannot fall back
  to anonymous reads or silently expand a token's permissions.
- Every actual server-required CheckRun and every approved-policy CheckRun has
  exactly one matching application/name result, completed with literal `success`
  on exact `M`. Checking only the approved subset cannot establish additional
  requirements imposed by the server.
- Complete bounded legacy commit-status inventories are read for both `H` and
  `M`, with their returned revisions and counts checked. A legacy status whose
  context matches any required CheckRun name is unsupported, even when that
  status says `success`: GitHub can require both a check and a commit status
  sharing a name. A CheckRun cannot establish the other result's meaning.
- A separate fixed GraphQL observation binds the repository, protected base ref
  and current PR tuple. Its branch rule must be known, have no required
  deployments or restricted-push policy, and agree with the pinned
  conversation-resolution setting.
  Where conversation resolution is required, a complete bounded thread
  inventory must show every thread resolved. An approving review alone does
  not establish resolved conversations.
- An exact `B...H` comparison establishes that the selected base is an ancestor
  of the head and that the head is not behind it. The generated merge commit's
  parents alone do not substitute for that explicit up-to-date observation.

Missing, truncated, duplicate, contradictory or unsupported observations block
the allowance. Each additional status/thread inventory and ancestry comparison
must be complete with fewer than 100 entries; larger scopes are unsupported.
An empty legacy-status inventory is absence of a legacy context;
its combined `pending` value does not itself establish a failed status. Neither
optional results nor copied success metadata can replace an actual required
result. All application identities, current reviews, exact `B/H/M` and tree,
protected source, no-bypass constraints and atomic reference leases still apply.

The additional observations are retained as `legacy_statuses` and
`required_conditions` for the fresh `UNSTABLE` path. Old `CLEAN` records are not
rewritten or reinterpreted. The conditional protocol uses 18 reads for inspection
and 40 for invocation; the existing `CLEAN` protocol retains its 16-read configured
inspection minimum and 32-read invocation. An invocation whose initial preflight
uses the new path must have enough independently approved capacity before marker
preparation. A later condition change after preparation can still leave an
uncertain result. No cap is increased automatically, and an exhausted or
uncertain invocation cannot obtain another effect allowance by changing its
state or output path.

Changed implementation bytes change the installed runtime and operation
fingerprints. New effects therefore need newly selected approvals; this
compatibility allowance does not migrate an old runtime or authorize replay as
a fresh effect. Existing terminal history remains historical.

### Readiness failure diagnostics

A blocked native inspection retains a fixed diagnostic in its existing `reason`
field. For example:

```text
GitHub merge readiness blocked: stage=protection; code=http_error; http_status=403.
```

The stage identifies the local key or credential check, repository/installation
scope, protection/signatures/rules, PR/review, legacy head/merge status,
branch conditions/ancestry, checks, merge commit, protected contract trees,
references, or final `ready_facts` validation. The additional aggregate stages
`unstable_capability`, `legacy_status_facts` and `required_condition_facts`
identify local validation of those groups; `unknown` is a defensive fallback
for invalid internal diagnostic context. Codes distinguish `http_error`,
`transport_error`, `invalid_response`, `deadline_or_storage`, `local_io_error`,
`malformed_facts` and `validation_failed`. HTTP status is included only for that
failed request and only as an integer from 100 through 599. Expected absence of
an optional operation-marker ref remains a successful read.

These diagnostics contain no exception text, provider response body, headers,
URL, token or private path. A stage describes where the check stopped; it does
not identify an individual rejected predicate within final fact validation or
establish that earlier observations still hold. The native result and blocked
proof shapes remain unchanged, and older generic blocked reasons remain valid
history. A not-ready inspection leaves a merge request using `--apply` `prepared`
before the native merge invocation. Diagnostics grant no retry, merge or
read-recovery authority, and changed runtime bytes require fresh approvals.

The conditional effect uses one GraphQL `updateRefs` transaction for the exact
`B → M` base update, an `H → H` head lease and creation of the signed operation
marker from an absent ref. Preparing the immutable tag object is a separately
bounded write within the stage. A lost reply can leave that preparation or the
transaction uncertain. Neither a missing marker nor a generic HTTP/GraphQL error
authorizes sending another write. Reconciliation only discovers actual provider
facts. Applied results require the exact resulting commit, authenticated marker
and actual merged pull-request identity; successful transport is insufficient.

The transaction leases references, not check conclusions or a policy version.
GitHub's enforced passing-check semantics include success, neutral and skipped;
the local preflight conservatively requires literal success. That stronger
preflight is an observed fact, not an atomic server guarantee. A trusted check
producer can change its conclusion between inspection and the transaction.
The independently pinned policy must explicitly select the actual provider
semantics and trust policy administrators to preserve the approved policy during
the call. The non-administrative merge actor cannot change or bypass it. A setup
that requires an atomic literal-success predicate or protection against
adversarial policy administrators is unsupported by this profile. Neither a
merged commit nor a retained successful preflight establishes fresh verification
after the effect.

Authentic acceptance must confirm the server's atomic head lease and protection
enforcement, including concurrent changes. If the selected GitHub configuration
cannot establish those guarantees, it is unsupported. The implementation does
not modify repository rules to make the profile usable.

The opt-in acceptance observer uses the fixed GitHub transport and independently
pinned original journal/key/configuration and current read authority. A separately
pinned outer packet selects one private create-only observation bundle and its
finite native limits. Reservation precedes its real watchdog-bounded read; loss
or a partial bundle does not authorize reuse or a new output path under that pin.
This observer has its own explicitly approved one-call budget. It does not mutate
the original delivery journal, charge its effect budget or turn offline terminal
replay into a fresh observation. An unconfigured observer case remains skipped
and therefore cannot produce a passing whole-REQ-041 observation.

## Durable authority and recovery

Initialize one private journal for the original objective and original goal.
Its canonical path, UUID, aggregate limits and per-call native limits are pinned
by the independently reviewed grant. Moving to a new path, creating a new
journal or changing policy is not a budget reset for the same approved scope.

The engine retains exact canonical request and native payload, approval identity
and resource reservation before native activity. It records that an invocation
may have started before releasing any write, including marker preparation.
Uncertain operations keep their resource conflict and spent capacity. All pull
requests targeting the same repository ID and base branch share that conflict
domain, including a different request prepared after the base moves. Subsequent
apply calls return retained history rather than sending a replacement effect.

Each call conservatively charges its full approved wall-time and read ceiling
before launch. One common `operations` unit means a logical delivery stage; it
does not mean one HTTP request. Native limits bound the finite preparation and
mutation writes. The real Linux watchdog bounds the whole native call, including
preparation, validation and nested requests. The invocation fence survives
parent death while the owned call is active. Withdrawal uses that same fence;
it does not undo an effect or promise to cancel an in-flight remote request.

New effects check actual UTC, the selected approval content pin, current local
withdrawal, selected policy and actual installed configuration. The fingerprint
includes installed Exacly Python source and the validation runtime/dependency
profile, the actual standard library/native runtime, TLS trust roots and a digest
of effective proxy settings, plus actual stage configuration. Proxy credentials
are never included in the profile. Changed code, key, policy, caps or
protected meaning requires a newly reviewed decision. Candidate JSON cannot
select a provider loader, endpoint or operational clock.

Exact terminal replay and `delivery show` work from retained history before
current credentials or configuration files are loaded. They are historical
results, not fresh observations.

An expired installation token cannot be replaced silently. A separate
`delivery-read-recovery` approval pins the original intent, request, payload,
journal, goal and capacity, the old and new configuration, and the unchanged
operation profile. Only the token descriptor and its independently reviewed
identity attestation may rotate. For the App-grant actor described below, the
local validity window and mint-provenance digest may rotate with that token;
credential kind, App/installation identity, grant schema, repository selection
and exact permissions stay in the operation profile. Switching from the legacy
actor to the App-grant actor requires a new operation decision. The read grant
permits discovery with zero writes, including after withdrawal; it cannot
authorize apply, refill capacity, change code/policy/key/caps or create another
intent. The original delivery
receipt retains its original authority and configuration.

The first profile does not implement runtime migration. Recover with the exact
retained installed runtime when its code profile is required. A code upgrade
needs a separately reviewed migration contract rather than weakening a retained
pin.

## Operator-selected setup and commands

The only merge credential selector is `EXACLY_DELIVERY_MERGE_TOKEN`. Do not put a
token in request, policy, payload or approval JSON. The installed actor descriptor
pins the actual token digest, application and installation identity. An operator's
review of that minting descriptor is a trusted bootstrap attestation; the
adapter does not claim it independently proves the token's issuer through an
unavailable identity API. The marker key is a separate delivery key; an existing
verification/publication key is not accepted as one.

### GitHub App capability evidence

The legacy actor remains the exact four-field descriptor: `app_id`,
`installation_id`, `token_digest` and `identity_approval_digest`. It keeps its
existing repository-role checks. The explicit App-grant actor adds
`credential_kind: github-app-installation-grant-v1` and the closed `grant`
object below. Both actor shapes reject unknown fields. Grant schema `0.1.0`
retains exactly the original five permissions and the existing `CLEAN` path.
Grant schema `0.2.0` adds only `statuses: read` and permits the separately
evidenced `UNSTABLE` path. These are capability-descriptor schema versions;
the credential kind is unchanged.

GitHub App grants and repository role flags are separate evidence. In the
[nonproduction diagnostic](https://github.com/whiteredhouse/exacly-delivery-acceptance/actions/runs/41422406506),
the pinned token action successfully requested Contents write for the selected
installation, while the repository response reported `push: false`. The legacy
check rejected that flag before reading branch protection. The App-grant actor
retains the actual role observations, including `false` or unavailable flags;
it does not manufacture `push: true`. A response that positively reports an
administrator or maintainer role remains unsupported.

Illustrative actor shape; replace the identities, digests and local validity
window with the actual reviewed values:

```json
{
  "credential_kind": "github-app-installation-grant-v1",
  "app_id": 5572362,
  "installation_id": 177653055,
  "token_digest": "sha256:<selected-token-digest>",
  "identity_approval_digest": "sha256:<selected-identity-attestation-digest>",
  "grant": {
    "schema_version": "0.1.0",
    "repository_ids": [1442278205],
    "permissions": {
      "contents": "write",
      "administration": "read",
      "checks": "read",
      "metadata": "read",
      "pull_requests": "read"
    },
    "not_before": "2026-11-02T10:00:00Z",
    "not_after": "2026-11-02T10:10:00Z",
    "mint_provenance_digest": "sha256:<reviewed-mint-evidence-digest>"
  }
}
```

The repository list must contain only the configured repository. For grant
schema `0.1.0`, the permission map must equal the five entries shown. Schema
`0.2.0` requires those same entries plus exactly `"statuses": "read"`;
missing, additional or broader grants are refused for either schema. The
independently selected configuration and identity attestation
bind the whole grant to the exact token, App, installation and repository.
A supplied permission object or matching provenance digest does not establish
that the issuer evidence was authenticated or independently reviewed.

The App owner must explicitly enable Commit statuses read and the trusted mint
must request it before selecting the six-permission grant. A successful earlier
five-permission preflight does not establish that access. Grant schema and
permissions remain in the stable operation fingerprint: moving from five to six
permissions needs a new operation decision, not a read-only credential rotation.
No App setting or existing token is changed automatically.

Retain the actual mint response or an explicitly trusted mint-run attestation
for that review. A trusted run attestation identifies its run and attempt,
pinned workflow, source and token-action implementation, exact requested
permissions, actual selected installation output and token digest. The
[pinned token action](https://github.com/actions/create-github-app-token/blob/64ab729a6294000f5aa4a4bc0b73d1f32bc45510/README.md#create-a-token-with-specific-permissions)
refuses requested permissions absent from the installation. Its ordinary
outputs do not expose the provider's granted-permission response or token
expiry; a following step must not label requested inputs as an independently
observed response or derive a purported provider expiry from the current time.

`not_before` and `not_after` are an independently approved **local use window**,
with UTC timestamps and a positive duration of at most one hour. They do not
claim GitHub's token issue time or expiry. The adapter checks actual UTC before
creating a fresh session and before every provider request, including reads and
reconciliation. GitHub may refuse an earlier-expired or revoked token. A local
window or successful mint does not establish that a protected mutation will
succeed, and neither permits another write after an uncertain outcome.

Static configuration validation and retained-fact validation check the local
window's shape and ordering without consulting the current clock. Historical
terminal replay therefore does not expire with a credential. The live adapter
still checks actual single-repository installation scope, protection, required
checks and independent reviews under the selected policy. The App descriptor
does not grant a protection bypass or relax any of those checks.

`exacly.automatic_merge.validate_installation_grant_candidate(repository,
actor, live_facts)` is a pure advisory validator for collecting review inputs.
Its closed candidate actor deliberately omits `identity_approval_digest`; the
installed configuration schema rejects that candidate. It checks supplied
facts without performing provider I/O or authenticating their provenance.
The caller separately uses `validate_installation_grant_time` with actual UTC
before fresh provider activity. A workflow result such as
`candidate_checks_passed` with `independent_mint_approval: false` establishes
neither an approved installation nor native merge readiness. Independent mint
review and the complete merge approval remain separate steps.

Create local storage before reviewing the resulting journal/key identities:

```bash
exacly delivery key-init --key /private/delivery/merge.key --json
exacly delivery init --journal /private/delivery/merge.sqlite \
  --objective objective.json --limits limits.json \
  --native-limits native-limits.json --json
```

These create private local material and grant no delivery authority. They never
replace an existing key or journal. Use the actual returned journal UUID below.
The operator supplies and reviews the exact configuration, policy, request and
native payload; the local preview performs no provider I/O:

```bash
exacly delivery merge --journal /private/delivery/merge.sqlite \
  --journal-id JOURNAL_UUID --config merge-config.json --policy merge-policy.json \
  --request merge-request.json --payload merge-payload.json --json
```

Only after independent approval, supply its content pin, issuer and identity
alongside `--approval approval.json --apply`. The content pin must be selected
outside that JSON; matching it does not prove that a human review occurred.
`delivery reconcile` selects the retained operation with `--operation-key`,
uses the separately current read authority and cannot resend the merge.
`delivery revoke --reason ...` withdraws new effects. `delivery show --json`
reads retained history without provider access.

## Nonproduction acceptance packet

Before a live effect, retain a reviewable packet containing:

- Existing nonproduction repository numeric ID/name, pull request and exact
  `B`, `H`, `M`, tree and protected branch/ref identities.
- Current protection configuration, required check/application identities,
  independent reviews, actor minting attestation, installed configuration and
  protected model/criteria/bindings/observer Git-blob inventory.
- For an App-grant actor, the exact token-bound grant, its reviewed mint
  evidence and local validity window; retain requested and observed mint facts
  with their actual provenance rather than replacing repository role flags.
- Original objective/goal, canonical journal path/UUID, key identity, operation
  key, marker ref, finite aggregate/native budgets and actual expiry.
- Exact request, payload, selected approval document and separately supplied
  reviewer/content pin; credentials and private key bytes remain private.
- Negative/concurrent-change checks, retained remote and local proof, no-resend
  recovery, read-only token rotation, offline replay and acceptance-verifier
  commands. Confirm the selected provider's guarantees rather than inferring
  them from a controlled fake provider.

Selecting a repository or running credential diagnostics grants no runtime
merge authority. This packet becomes an operational approval request only after
those concrete identities and limits are selected for independent review.
A merge receipt records a repository effect. The resulting commit needs new
applicable observations before readiness, and separate grants for publication
and deployment. It establishes no runtime health.
