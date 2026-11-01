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
identity attestation may rotate. The read grant permits discovery with zero
writes, including after withdrawal; it cannot authorize apply, refill capacity,
change code/policy/key/caps or create another intent. The original delivery
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
- Original objective/goal, canonical journal path/UUID, key identity, operation
  key, marker ref, finite aggregate/native budgets and actual expiry.
- Exact request, payload, selected approval document and separately supplied
  reviewer/content pin; credentials and private key bytes remain private.
- Negative/concurrent-change checks, retained remote and local proof, no-resend
  recovery, read-only token rotation, offline replay and acceptance-verifier
  commands. Confirm the selected provider's guarantees rather than inferring
  them from a controlled fake provider.

No destination or runtime grant is currently approved. This packet becomes an
operational approval request only after those concrete identities are selected.
A merge receipt records a repository effect. The resulting commit needs new
applicable observations before readiness, and separate grants for publication
and deployment. It establishes no runtime health.
