# Static deployment and operational observation

REQ-043 has a deliberately narrow reference implementation: one independently configured,
nonproduction, loopback HTTP service on one trusted Linux host with local POSIX storage. The owning workflow installs and
serves exact opaque artifact bytes. It never imports an artifact, executes application code, rebuilds
a package or chooses a deployment command from JSON. This profile supports static byte readiness;
it does not establish the health of a general application.

Source implementation and controlled local verification do not constitute an approved staging or
production deployment. A real selected environment still needs its independently approved owning
workflow, artifact/source, deployment grant, observer configuration and key, service endpoint,
environment generation, budgets and recovery instructions. Existing verification-pilot grants,
publication authority and native gate results grant none of that authority.

## Components and trust

`deployment_automation.LocalStaticDeploymentAdapter` implements the delivery engine's installed
adapter interface. Its constructor takes trusted configuration and policy plus the externally
selected `delivery_automation.ApprovalPin`. Candidate request/payload JSON cannot select Python
modules, shell commands, storage paths, keys, HTTP endpoints or observers. The default artifact
reader is the fixed installed `owning_publisher.read_published_artifact` implementation. It checks
the publisher's authenticated terminal receipt, registry identity, exact release workflow/source,
version and complete actual immutable inventory before returning the selected bytes. Deployment
policy separately requires `publication_verification_contract` and
`publication_verification_producer`: the retained publication verification must match both. These
artifact-acceptance pins are independent of the deployment health/common contract; a trusted
publisher receipt for different acceptance meaning or producer cannot authorize dispatch. Historical
verification expiry after an applicable publication does not invalidate its immutable artifact.

The adapter rechecks current independent authority, exact request and artifact, protected contract,
actual installed configuration, and current environment generation before invoking the owner.
`LocalStaticOwner` repeats the separately selected approval pin and actual UTC lifetime check under
its exclusive lock before persisting intent and immediately before switching the active generation.
The delivery engine supplies the separate durable objective journal and reserves operation, time,
read and rollback budgets before invocation. Calling a Python validator alone reserves no budget.
The owner journal provides an additional durable environment fence across different callers.
Native `read_calls` counts bounded logical owner/registry operations and HTTP sampling requests,
not filesystem syscalls. Immutable registry inventory, owner ancestry/file sizes and sample/body
volume have separate fixed bounds; all run inside the same actual whole-call deadline. The engine
reserves the configured phase ceiling before releasing the native call.
Every phase conservatively reserves at least `samples + 5` logical reads. Rollback invocation can
use a registry read, current snapshot, explicit prior lookup, owning-workflow transition, the HTTP
samples and a final generation-discovery read. The last read is charged inside the native phase;
presentation does not add a provider read afterward.

The owner root, publisher registry, deployment owner key and operational observer key have distinct
identities. Keys use separate format magic and HMAC domains. Key files and owner records require
owned, single-link, mode-0600 regular files inside protected directories. Owner storage must be
outside a source working tree and have a mode-0700 directory. Independent selection of an observer
key/configuration remains an operator responsibility: possession of an HMAC key authenticates the
selected local observer, not host honesty or independent administration. All host processes with
the operator's account are within this reference profile's trusted boundary.
The observer key must be outside the owning workflow root, including nested paths and existing
directory aliases. Canonical namespace checks and directory device/inode comparisons keep its
shared key lock separate from the owner's exclusive environment lock. Preview reads directory
identity metadata only; live preconditions require supported private directories and a private
regular observer key before retaining intent. Rollback repeats these checks under the owner lock.

The configuration digest uses the shared `delivery_profile.implementation` inventory: actual whole
package enforcement source, schemas, parser dependencies, Python standard library, loaded ELF
runtime, TLS roots and a digest of proxy configuration. Closed deployment and observer schemas are
also retained. This is an actual installed inventory, with no reduced validation-only fallback.
Observer configuration has its own separately approved digest. A
configuration or observer change requires reviewed new pins; candidate timestamps cannot renew a
grant. These checks do not attest the integrity of a compromised host or external operator.

## Owning workflow and recovery

Bootstrap explicitly creates a new empty owner with `local_static_deployment.initialize` and a new
purpose-specific owner key with `initialize_key`. It refuses to reset an existing root. The owner
identity pins the application, environment and owning workflow. A `StaticDeploymentServer` instance
serves `/artifact` on `127.0.0.1`; other paths fail, and nonloopback binds are rejected. Its active
response is `application/octet-stream`, has a fixed content length and carries rollout, generation,
effect-nonce and source-revision headers. Artifact bytes are never executed.

Each mutation performs the following bounded protocol under an owner-directory flock:

1. Recheck the independently approved scope, current generation and retained unresolved intents.
2. Create and fsync an immutable exact intent, native payload and authority record.
3. Create or verify the immutable content-addressed artifact slot.
4. Create and fsync the authenticated rollout record, including its exact parent generation.
5. Repeat authority and rollback preconditions, then conditionally replace and fsync the active
   generation pointer while still holding the owner lock.

This lock serializes the configured owner protocol; it does not fence arbitrary administrators or
writers that bypass it. The active pointer is replaced atomically on the supported local filesystem.
The owner retains a bounded authenticated parent chain. A signed staged rollout file alone is not
proof that its generation became active. After reading the actual active pointer or authenticated
active ancestry, the owner mints a distinct-purpose activation attestation bound to the exact record,
generation, current active generation and observation time. Native result validation requires that
attestation. Discovery signs it in memory without writing the environment; the engine retains it.

`reconcile` discovers an exact intent only from the actual active generation or its authenticated
active ancestry. It never stages bytes, switches a generation, resends a deployment, clears a fence,
or infers that an absent result means nothing happened. Read-only operational sampling may accompany
discovery. A retained intent outside the active chain fences further mutations. A truncated file,
unknown outcome, exhausted history bound or unconfirmed filesystem result stops with an actionable
uncertain outcome. Operator recovery requires inspecting retained files and separately approving a
specific recovery procedure; there is no automatic journal reset or corruption repair.

Before returning an applied result, invocation and discovery additionally bind the authenticated
owner record and activation to the supplied exact intent ID and authority digest. Matching request
and payload alone cannot complete a different approval or journal's intent. A genuine foreign
intent's signed proof stays retained as uncertain evidence, with no current-intent effect or new
health claim. No mutable last-validation cache participates in this correlation.

## Independent operational contract

`operational_observation.observe` samples the configured loopback HTTP endpoint using a direct,
bounded socket protocol with no proxy, redirect, command or credential-helper selection. It hashes
the actual response bytes, records status, completion, size, latency and all rollout headers, and
signs the complete report with the separate observer key. It checks actual whole-call deadlines,
sample/body bounds and the declared interval between samples.

Every report binds application, environment, exact artifact digest/size, complete source identity,
rollout ID, effect nonce, generation, required sample count, evaluation start/end and expiry, observer
configuration and a fresh report identity. Verification rejects malformed, substituted, future,
stale, cross-environment, incomplete-window or already consumed reports. The adapter additionally
requires the window to begin after the actual owner rollout was created. A measurement with wrong
served bytes/status/latency/rollout is retained as unhealthy; unavailable complete measurements are
unknown. The native verification/delivery gate has no operational-health role.

The native result retains the authenticated deployment effect separately from the signed observation
and derived operational evaluation. A definite applied effect remains applied when the service is
unhealthy or observation is unavailable. Operational readiness remains failed or unestablished in
those cases. The common delivery receipt's `operational_health_established` claim stays false;
callers must inspect the separate operational evaluation and must not equate `applied` with healthy.
The engine durably retains the result and observations. Report verification alone does not consume
an observation or grant any later stage authority.

Retained `facts.evaluation` authenticates the historical measurements and explicitly grants no
current-readiness claim. `operational_observation.authenticate` checks signature, exact scope and
measurement consistency even after expiry; future or malformed observations still fail closed.
An expired health report cannot invalidate an authenticated activated deployment effect.
After sampling, authentication and full profile work, the bounded native phase performs one final
owner discovery and retains its fresh signed activation as `facts.current_activation`. It then
records `facts.operational_status` with actual UTC, the observed generation/time and scope
`as-of-recorded-environment-observation`. A newer active generation makes the selected rollout
`unknown`/`superseded`, despite a fresh healthy report. An unavailable final snapshot makes current
readiness `unknown`/`unavailable`. The already authenticated applied effect and original report stay
retained. No claim extends beyond the signed environment-observation point.

`retained_operational_status(request, payload, provenance)` presents that engine-authenticated native
point using only in-memory schema/digest/applicability checks. It performs no key, profile or provider
reads, never advances the original `evaluated_at`, and only downgrades expired measurements to
`unknown`/`stale`. This presenter does not authenticate arbitrary untrusted input. Inside the bounded
native boundary, `validate_provenance` authenticates signatures/profile first; `validate_result`
then checks the already authenticated outcome and recorded point without IO. CLI presentation uses
the retained API; terminal history remains offline and does not invent new health observations.
The authenticating boundary compares recorded evaluation/snapshot times with actual UTC after all
fallible checks and rejects future points. Deterministic as-of recomputation cannot authorize a
future native observation.

The separate actual-live API `operational_status(..., deadline=..., read_scope=...)` requires an
`OperationalReadScope` matching the owner/application/environment and a caller-owned authoritative
`reserve_read` hook. It charges one owner read before discovery, respects the supplied deadline and
returns the new signed generation point. It does not collect new HTTP samples or create a budget.
The strict current observer `verify` still rejects expired reports for rollback. The observer's
measurement-window helper captures actual UTC after authentication/profile work by default;
explicit `now` requests a deterministic as-of query. Measurement freshness alone cannot establish
that the selected generation is currently active.

## Explicit bounded rollback

This first profile supports a separate, explicitly pinned rollback grant. It does not infer rollback
authority from a failed observation and does not implement a general automatic recovery policy.
The exact rollback request binds the selected current generation, observed current artifact, exact
allowed prior artifact, environment, workflow, strategy and condition digest. The policy/payload
also pin the current rollout ID, prior active generation, exact fresh signed failure report, expiry
and a maximum of one rollback. Its separate delivery budget consumes one rollback operation.

Before writing, the owner checks that the current rollout really became active, the trusted fresh
report evaluates that exact rollout as unhealthy, the report postdates rollout creation, and the
desired artifact/source belonged to the explicitly selected prior active generation. The owner
repeats these checks while holding the CAS lock immediately before switching. Failure report IDs
are retained and consumed in immutable owner intents; reuse is rejected. A healthy or unknown
observation, stale generation, missing prior record, expired conditions or changed artifact blocks.

A successful rollback creates a new generation and rollout identity and obtains new operational
observations. It never rewinds the journal, reuses the old generation, silently retries, or labels
the old healthy observation as evidence for the rollback. An uncertain deployment must first be
reconciled; it cannot be blindly rolled back.

## Verification and remaining acceptance

Controlled tests use a real transient HTTP service, actual opaque bytes, separate real HMAC keys,
private owner storage, real concurrent generation contenders and process kills before/after active
CAS. They cover effect/health separation, wrong identity, fresh unhealthy/healthy observations,
explicit rollback, partial writes and discover-only recovery. Operational-observer tests independently
cover byte substitution, HTTP/protocol/body/deadline faults, purpose-separated keys, stale/future
reports, complete windows and replay rejection.

Focused adapter tests use an explicit trusted Python byte-reader fixture. The integrated controlled
test uses the real fixed owning-publisher subprocess, signed artifact verification, immutable
registry, production default deployment reader, actual HTTP observer and separately approved bounded
rollback to the prior published artifact. Wrong artifact-acceptance contracts or producers must
reject before any deployment effect and preserve owner/registry files. A selected nonproduction
acceptance must repeat the full path with independently approved real environment/observer pins.
No controlled result or merged code may be presented as that authentic acceptance, and no selected
staging or production authority is bundled here.

The [acceptance plan and packet inputs](product-delivery-acceptance.md) give the
bounded operational sequence and the missing infrastructure/reviewer selections.
