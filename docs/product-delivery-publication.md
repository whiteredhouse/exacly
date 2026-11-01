# Exact-byte local owning publisher

The first release-publication profile is `local-owning-publisher-v1`: one
owner-managed, private POSIX filesystem registry in one coordination domain.
It executes a fixed installed publisher workflow in a separate Python process
with Python's `-I` import isolation. This is not an operating-system sandbox.
It does not dispatch GitHub Actions, publish to PyPI or claim public registry
support. The existing tag-triggered release workflow keeps its ownership.

The operator independently selects a private owning-workflow configuration.
It pins the exact objective, operation, source, artifact inventory, immutable
version, destination UUID, workflow revision and installed contract bytes,
delivery reviewer identity, journal UUID, original limits, scope expiry and
separate publisher and artifact-verifier purpose keys. Dispatch JSON cannot
select another configuration, executable, producer policy, destination or key.
The delivery approval additionally pins the actual installed package, schemas,
runtime, dependencies, owner configuration and policy. These content pins are
trusted bootstrap decisions, not proof that a human review was independent.

The independently selected private owner configuration is a separate
predelegation for this one immutable version. Matching caller issuer, approval
ID, journal and limit fields is structural; the owner entry point does not
verify an external `ApprovalPin`, durable engine reservation or revocation.
The trusted local adapter caller enforces those boundaries before dispatch.
This profile requires candidate code to be unable to invoke the host command or
read the private owner config and keys. A remote or untrusted owner would need
a separate authenticated handoff contract; this local workflow does not provide
that protocol.

The input artifact store contains already built bytes named by SHA-256. Before
reserving a version, the owner retrieves the exact signed, independently
produced verification receipt and checks its producer, source, artifact inventory,
contract and current applicability. It reads and hashes every selected artifact.
The publisher has no source checkout, build command or substitution path.

One workflow invocation is one bounded native dispatch. Each inspection or
discovery phase makes exactly one owner inspection or discovery invocation;
`read_calls: 1` therefore suffices. Publication makes one owning-workflow
invocation (`write_calls: 1`), with a conservatively charged read slot as well.
No phase starts an additional owner invocation. These units count workflow
calls rather than filesystem reads, writes or syscalls. Its internal effect is
limited separately to one immutable version, at most 64 artifacts and 64 MiB,
plus fixed claim and receipt metadata. Repeated calls consume the original
goal's cumulative engine budget. The delivery engine must persist intent and
reserve its original goal capacity before releasing the workflow under its real watchdog
and invocation fence. The workflow also enforces its whole remaining deadline;
its own process never writes the delivery SQLite journal.

An exclusive version directory reserves the canonical registry/package/version
resource. Resource paths hash the structured package and version pair, avoiding
label concatenation aliases. The owner syncs the reservation and claim, exact
artifact bytes, retained verification and signed publisher receipt. It publishes
an empty terminal `published` directory only after those durability barriers.
No code path replaces an existing version or moves a tag.

Reconciliation reads the existing terminal marker, authenticates the actual
owner receipt and retrieves every retained artifact again. A missing marker,
partial reservation or absent version remains uncertain. Native facts distinguish
unobserved state after a lost reply, observed absence and an actual reservation;
partial claim identities and artifact hashes remain separate from terminal proof.
Discovery never republishes, fills missing files or interprets absence as
permission to retry.
The native invocation also binds each authenticated owner receipt to the exact
retained intent. A valid receipt for another intent with identical source and
artifact inputs remains diagnostic evidence of an uncertain invocation; it
cannot be rebound to the caller's authority or declared applied.
An exact existing operation returns its original receipt; another operation
cannot acquire that version. The public read-only consumer helper also checks
the selected source, owning workflow, registry UUID, version and every actual
artifact before returning bytes to an independently authorized later stage.
Both reconciliation and the consumer reject future publication timestamps and
require the timestamp to fall within the signed original owner window and the
retained verification window. Current grant or verification expiry does not
invalidate an applicable historical publication.

The consumer authenticates retained verification bytes through their digest in
the trusted publisher's signed receipt. Without the separate verifier key, it
does not independently reauthenticate that verifier. Optional independently
selected verification-contract and producer expectations bind downstream
artifact acceptance meaning. They do not admit a new provider or turn the
native readiness gate into runtime health evidence.

Publisher and verifier HMAC keys use different file formats and signature
domains. These keys authenticate the selected local roles. The same-user host,
its installed code, private directories and local durable filesystem are trusted;
this profile is not a security boundary against a dishonest operator or host.
Do not place it on a filesystem without the required local POSIX locking,
exclusive creation and durability semantics.

All publication claims about verified implementation, deployment, operational
health and later authority remain false. A retained verification receipt is a
prerequisite for publishing its selected bytes; publication does not create a
fresh observation or authorize deployment. Controlled subprocess, concurrency
and crash tests demonstrate this reference profile. Authentic operational
acceptance still needs an independently approved nonproduction owner, storage,
verifier, current runtime pins and delivery grant. REQ-042 remains proposed until
that acceptance exists.
