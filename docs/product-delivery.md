# Product planning and delivery

Product delivery has its own approved scope, separate from the unattended
verification pilot. The [implementation checkpoint](product-delivery-progress.md)
records source, verification and operational acceptance separately.

The first delivered interface is advisory implementation planning, REQ-040.
Merge, publication, deployment and rollback require separately reviewed concrete
adapters and operational grants. Passing a proposal check does not grant any of
those effects. CAP-005/PROC-005 remain proposed while their whole process is
incomplete.

## Approve the planning inputs

Keep reviewed intent and planning policy outside generated candidate content.
The trusted caller selects their exact digests and the issuer/approval identity.
The planning policy additionally pins the approved model namespace and content
digest, repository identity, exact source/model target, eligible Requirements,
literal output paths, validity dates and `genesis_contract_digest`. The genesis
pin covers the initial intent, policy (excluding that genesis field), model,
criteria, bindings, target and applicable observation identities. A claimed Git revision alone cannot
approve edited working-tree criteria.

Intent selects canonical Requirement IDs and zero-based criterion indices. Each
goal supplies allowed output paths, reviewed Requirement dependencies, unresolved
design decisions and findings. Finding kinds distinguish:

- `observation_gap`: evidence collection or investigation is needed; this alone
  produces no implementation task.
- `approved_behavior_gap` or `investigated_finding`: an independently reviewed
  finding can produce a task for each mapped criterion.

Finding evidence retains explicit immutable content identities and review
references. Native observations remain a separate axis: unknown evidence neither
establishes missing code nor erases a separately approved behavior finding.
Failed verification alone calls for investigation rather than automatically
authorizing a repair.

Semantic validation errors in supplied observations remain explicit blockers.
Their positive assessments are diagnostic and cannot satisfy task prerequisites.
Independently approved implementation tasks remain visible, but the proposal
stays blocked until observation validity and dependencies are reviewed.

The pure API exposes closed `INTENT_SCHEMA` and `POLICY_SCHEMA` documents. Input
pins authenticate selected content only through the trusted caller's approval
process; these files do not prove their own authenticity or independent review.
Dates use ISO calendar dates with an exclusive `expires_at` for planning scope.
Operational effect adapters must check actual current authority and preconditions
at invocation time; the advisory planning date is not an operational grant.

## Generate and retain a proposal

Use independently reviewed input files and pins:

```sh
exacly implementation propose -m spec/exacly \
  --intent reviewed/product-intent.json \
  --policy reviewed/planning-policy.json \
  --target reviewed/source-target.json --date 2026-11-01 \
  --approved-intent-digest "$APPROVED_INTENT_DIGEST" \
  --approved-policy-digest "$APPROVED_POLICY_DIGEST" \
  --issuer "$APPROVED_ISSUER" --approval-id "$APPROVAL_ID" \
  --out reports/product-proposal-1 --json
```

The planner generates criterion-level tasks and expands reviewed intent
dependencies into their task graph. Runtime Application dependencies do not
silently become implementation ordering. Each task preserves its Requirement,
exact criterion text/fingerprint, immutable input identities, literal output
scope, dependencies and observable completion conditions. Missing references,
cycles and unresolved choices block a usable decomposition.

Generated criteria, tests, bindings and policy edits remain explicitly unapproved
contract-change proposals. They require independent review; neither the planner
nor a later executor can approve weaker acceptance for its own work.

The output directory must be new and outside selected inputs/model folders.
It contains the proposal, an input manifest and exact raw input snapshots:

```text
product-proposal-1/
  proposal.json
  manifest.json
  inputs/
    ...
  .proposal-prepared
```

Directories use `0700` and files `0600`. The CLI reserves the destination with a
create-only directory operation and publishes `proposal.json` last with a
create-only link. A prior bundle, including an existing empty directory, is
never replaced. Missing `proposal.json` means incomplete retained diagnostics.
The private prepared link is retained so cleanup cannot damage a committed
proposal. This is protected history between cooperating processes, not protection
against its owning OS user editing files.

Inputs are bounded regular JSON/YAML documents with finite JSON values and unique
mapping keys. Unsupported aliases, excessive nesting/size and unsafe output
aliases are rejected. No network call, candidate execution, observation write or
delivery effect occurs.

| Exit | Meaning |
| --- | --- |
| `0` | A valid unblocked advisory proposal or currentness check; no execution authority is established. |
| `1` | The proposal/check contains explicit blockers requiring review. |
| `2` | Invalid/stale inputs, approval mismatch, incomplete/corrupt bundle or output error. |

## Review currentness and explicit revisions

Check a complete bundle against fresh separately approved inputs:

```sh
exacly implementation check -m spec/exacly \
  --bundle reports/product-proposal-1 \
  --intent reviewed/product-intent.json \
  --policy reviewed/planning-policy.json \
  --target reviewed/source-target.json --date 2026-11-01 \
  --approved-intent-digest "$APPROVED_INTENT_DIGEST" \
  --approved-policy-digest "$APPROVED_POLICY_DIGEST" \
  --issuer "$APPROVED_ISSUER" --approval-id "$APPROVAL_ID" --json
```

The reader verifies retained bytes/manifest and reproduces the proposal from
its retained inputs before checking current model, criteria, bindings, policy,
intent and applicable observation fingerprints. Expiry or changed inputs require
review; changing the checking date alone is not an approval renewal.

Revision 1 has no predecessor and must match the independently pinned initial
contract. `implementation_planning.genesis_digest(...)` computes a fingerprint
for review; computing it grants no approval. Changing a target, model, policy or
observation cannot silently restart the same series at revision 1. An explicitly
approved new genesis has a different logical proposal identity, even if its human
intent label is reused.

A later reviewed intent explicitly names the same
logical intent, increments the revision and pins `supersedes` to the exact prior
proposal digest. Supply `--previous PRIOR_BUNDLE/proposal.json` and a new output
directory. Currentness checking a later revision also needs that predecessor.
Earlier revisions remain retained; a new policy, replan or renamed operation
does not silently supersede them or authorize an executor. Advisory branch
proposals are not a globally selected execution head. A future execution journal
must independently select an exact approved proposal and retain its original
objective and consumed budgets across any replanning.

In Python, use `implementation_planning.create(...)` and
`implementation_planning.validate_current(...)` with explicit named model
documents, intent, policy, target, date and externally selected approval pins.
Both are pure. Proposal output contains no verified implementation claim.
People or separately bounded external agents perform implementation after its
own review. The fixed snapshot-inventory worker is not a coding-agent integration.

## Delivery document boundary

`exacly.delivery_automation` defines closed versioned requests, approvals, intents
and receipts under a distinct `product-delivery-v1` profile. Stages are separate:
planning, merge, publication, deployment and rollback do not authorize each other.
Typed stage identities retain exact source/head/base, artifact, publisher or
environment coordinates, content digests, original objective, installed adapter
configuration, journal identity and declared limits.

`ApprovalPin` selects the issuer, approval ID and content digest outside the
proposed document. `validate_approval` requires an explicit aware current time;
`validate_intent` and `validate_receipt` can inspect historical records without
pretending that their approval is current. Stable resource keys identify declared
conflict domains; concrete adapters must resolve actual provider identities and
aliases before relying on those keys.

These validators perform no provider invocation, budget reservation, signature
authentication or live precondition check. A concrete adapter must validate its
native payload, installed configuration, current authority/revocation and actual
provider guarantees; a durable journal must reserve immutable budgets and retain
intent before effects. Local serialization is not a remote atomicity guarantee.

An applied receipt records only its selected stage effect. Uncertain receipts can
retain unexpected actual effects for reconciliation without declaring success or
granting compensation. Historical replay is not a fresh observation. No receipt
establishes operational health or later-stage authority; REQ-043 requires its own
trusted environment-specific runtime observation/evaluation contract.

The current package's tag-triggered release workflow retains its existing
ownership. It rebuilds from source, so it is not an adapter for promotion of
separately approved existing bytes. No publication or deployment invocation is
introduced by these document interfaces.

The separately implemented [merge profile](product-delivery-merge.md) adds the
private journal, current merge authority, real bounded native invocation and
exact GitHub effect adapter. Its controlled verification does not establish live
provider acceptance. Publication and deployment stay separately configured and
authorized; they cannot inherit a merge grant.

The [local owning-publisher profile](product-delivery-publication.md) promotes
exact independently accepted prebuilt bytes through its separately configured
owner. Its private immutable registry, original-goal journal, authenticated
receipt and actual-byte discovery implement a bounded reference workflow without
changing the existing tag-triggered package release. Selected operational
acceptance and deployment remain separate.

The [static deployment reference](product-delivery-deployment.md) consumes exact
authenticated published bytes under its own application/environment grant. A
distinct operational observer measures the actual HTTP result and preserves
current freshness separately from the retained applied effect. Rollback has its
own exact prior artifact, fresh-failure conditions and original-goal budget.
These are bounded local profiles, with selected operational acceptance still
pending; neither they nor the native gate establish general application health.
