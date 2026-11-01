# Exact remote publication and producer selection

`exacly.remote_publication` retains intent before the trusted transport can send
one atomic expected-state request. `exacly.git_remote_transport.GitHubGitTransport`
is the concrete GitHub smart-HTTP implementation. The candidate never receives
credentials, local operator storage or a publication API.

A request pins the native handoff and its stores, exact repository ID/name,
branch, source/base/result objects, expected old remote ref and separately
approved operation-marker ref. Initialize a private publication key with
`exacly.publication_key.initialize`, pin its returned key identity and preserve
its exact private storage alongside the separately scoped journals. Its secret
bytes never enter grants, logs or artifacts. A signed annotated marker binds the
operation, repository/ref, old/new commits, exact handoff and key identity.

The transport sends explicit atomic receive-pack commands for both old-to-new
branch movement and absent-to-exact marker creation. A porcelain push is
insufficient: an already-up-to-date branch can suppress its lease check. This
adapter tests that race against a real local receive-pack server. It imports no
unapproved base closure and cannot force-overwrite or delete a ref.

The same captured GitHub App token must prove that its authenticated
`/installation/repositories` scope contains exactly the approved numeric
repository identity. Literal GitHub endpoints, inherited proxy/CA trust,
redirect refusal, bounded data and deadlines remain enforced. Repository scope
does not attest the token issuer or every permission. Operators must separately
approve the configured App installation and mint permissions. For the first
pilot use a non-GitHub-Actions App token so the source push triggers the approved
producer; do not substitute the job token or this chat's connection.

After a lost acknowledgement the journal remains uncertain. Recovery discovers
remote objects, refs and the exact authenticated marker; it never resends an
uncertain publication. An independently observed divergent ref remains conflict.
An authenticated receive-pack rejection is terminal conflict even if another
actor later happens to create matching refs. Reinitializing journals or keys is
not recovery. `lookup` distinguishes validated absence in the exact pinned store
from unavailable, replaced or malformed history.

`exacly.producer_selection` validates the approved workflow ID and bytes, push
branch/event, exact source SHA, run/attempt and sole-job contract on every bounded
poll. It retains immutable selections; latest-green, arbitrary reruns, matrix or
PR producers are unsupported. A selection or published commit grants no evidence
admission or completion authority. Native admission and completion remain
separate, fresh checks.

The local receive-pack and controlled API tests verify adapter behavior. The
opt-in live publication test requires separately approved real configuration;
its skip is inconclusive. REQ-037 remains proposed until live acceptance. The
selected pilot's full setup and independent approval procedure is documented in
`unattended-pilot-approval.md` when that slice is installed.
