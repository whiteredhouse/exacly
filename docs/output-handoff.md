# Reviewed executor output handoff

The local handoff implements [REQ-032](../spec/exacly/automation.yaml). It connects a retained successful [executor attempt](bounded-executor.md) to an exact Git implementation revision. This is unreleased functionality on main.

The executor identifies collected artifact bytes. The handoff separately reviews how selected artifacts become source files, constructs the resulting Git tree and commit in trusted operator code, and conditionally publishes one selected local branch ref. The retained provenance identifies this byte-to-Git transformation; it does not authenticate tests, establish acceptance coverage or authorize evidence admission or completion.

The first adapter is `local-git-v1`. Its destination is an explicitly initialized private bare SHA-1 Git repository. It does not check out candidate source or use a working tree. Remote push, binding replacement, producer execution and completion are separate operator actions.

## Initialize protected storage

Use canonical absolute paths in operator-owned `0700` directories outside candidate storage, Git working trees and model directories. Keep the handoff journal, executor journal, bare repository and artifact output resources distinct. Existing executor and binding/completion stores retain their formats.

```sh
EXACLY_HANDOFF_DIR="$HOME/.local/state/exacly/handoff"
mkdir -p "$EXACLY_HANDOFF_DIR"
chmod 0700 "$EXACLY_HANDOFF_DIR"
EXACLY_HANDOFF_REPOSITORY="$EXACLY_HANDOFF_DIR/source.git"
EXACLY_HANDOFF_JOURNAL="$EXACLY_HANDOFF_DIR/handoff.sqlite3"

exacly handoffs init-repository --repository "$EXACLY_HANDOFF_REPOSITORY" --json

exacly handoffs init --journal "$EXACLY_HANDOFF_JOURNAL" \
  --repository "$EXACLY_HANDOFF_REPOSITORY" --json
```

Repository initialization refuses an existing destination and returns its initialized identity. The bare repository uses sealed settings and contains no candidate-controlled checkout configuration. Journal initialization is also explicit and returns a separate identity pinned to the repository. Ordinary preview, apply and inspection never initialize, replace or silently migrate storage.

The base commit and its complete reachable tree/blob objects must already exist in the destination. Importing a reviewed base from an explicitly trusted source is a separate operator operation. For example:

```sh
git --git-dir "$EXACLY_HANDOFF_REPOSITORY" fetch --no-tags --no-write-fetch-head \
  "$EXACLY_TRUSTED_SOURCE_REPOSITORY" "$EXACLY_BASE_REVISION:refs/heads/base"
```

Independently review the source, full base revision and import command. Handoff commands do not fetch missing objects or select an implicit remote. Import must preserve the initialized repository identity and sealed settings. Incomplete base objects block preview or recovery rather than permit publication from a different base.

## Review a policy and request

A policy selects the initialized handoff journal, canonical executor journal and private bare destination. Pin the identities returned by initialization and the executor journal's initialized identity. The policy's `allowed_paths` contains literal destination paths or directory prefixes ending in `/`; it supplies no repository-specific workflow or test defaults.

This example uses placeholders. Replace them with exact canonical paths and initialized identities before review:

```json
{
  "schema_version": "0.1.0",
  "name": "product-reviewed-source-handoff",
  "version": "1",
  "adapter": "local-git-v1",
  "storage": {
    "journal": "/absolute/operator/handoff/handoff.sqlite3",
    "journal_id": "<initialized handoff journal identity>",
    "executor_journal": "/absolute/operator/executor/executor.sqlite3",
    "executor_journal_id": "<initialized executor journal identity>",
    "repository": "/absolute/operator/handoff/source.git",
    "repository_id": "<initialized repository identity>"
  },
  "allowed_paths": ["src/", "tests/"]
}
```

Keep policy, request and authorization files outside recursively loaded model directories and candidate-accessible storage. Review the permitted source paths against the approved work contract. A path allowance is repository-mutation authority for those paths; it does not approve changed criteria, observation bindings, trusted producers or completion policy.

The request selects one successful retained executor attempt and maps each selected artifact path to one source path. The base revision must equal the attempt's retained input target revision. The source journal, receipt and build are read and checked directly. The handoff retains the complete original executor contract and requires its digest to match `receipt.authorization.contract_digest`, binding its approved model, context and input target to that exact receipt. Importing copied receipt or target metadata grants no authority.

```json
{
  "schema_version": "0.1.0",
  "policy": {
    "schema_version": "0.1.0",
    "name": "product-reviewed-source-handoff",
    "version": "1",
    "adapter": "local-git-v1",
    "storage": {
      "journal": "/absolute/operator/handoff/handoff.sqlite3",
      "journal_id": "<initialized handoff journal identity>",
      "executor_journal": "/absolute/operator/executor/executor.sqlite3",
      "executor_journal_id": "<initialized executor journal identity>",
      "repository": "/absolute/operator/handoff/source.git",
      "repository_id": "<initialized repository identity>"
    },
    "allowed_paths": ["src/", "tests/"]
  },
  "execution_id": "sha256:<successful execution identity>",
  "attempt": 1,
  "base_revision": "<full 40-character approved input commit>",
  "ref": "refs/heads/reviewed-change",
  "expected_revision": null,
  "mappings": {
    "artifacts/module.py": "src/module.py",
    "artifacts/test_module.py": "tests/test_module.py"
  },
  "message": "Implement the reviewed product change",
  "author": {
    "name": "Reviewed operator",
    "email": "operator@example.invalid",
    "time": 1793367000
  }
}
```

`expected_revision: null` authorizes an absent new branch destination. To advance an existing branch, its expected value must be the exact approved base revision. Ref names, mappings, message and author metadata are explicit request inputs, not values supplied by an executor claim. The commit identity is deterministic from its approved tree, single parent and retained metadata; preview displays that exact resulting revision.

Only ordinary files can be added or replaced. Existing ordinary-file modes are preserved; new files use `100644`. Mappings cannot delete files, select executable modes, traverse symbolic links or submodules, collide, escape their source or destination scope or overwrite unrelated tree entries. Unselected artifacts remain outside the resulting change. The original executor build continues to identify the complete collected bundle, while the handoff records the selected roster and mapping explicitly.

## Preview and separately authorize publication

```sh
exacly handoff local-git --request trusted/handoff-request.json --dry-run --json
```

Dry-run is the default. Preview verifies current journal and repository identities, retained successful history, bounded artifact bytes, base objects and expected ref. It returns the exact path changes, proposed tree and commit and an `authorization_template`. It writes no Git objects, refs, handoff history, authoritative state or bindings. A preview or copied template is not approval.

Independently review the selected receipt/build, artifact identities and destination changes, exact repository, base, ref, expected value, policy and commit metadata. Save the exact authorization object in protected operator storage, then explicitly apply it:

```sh
exacly handoff local-git --request trusted/handoff-request.json \
  --apply --authorization trusted/handoff-authorization.json --json
```

Apply rechecks those exact facts. Changed artifact bytes, storage identity, policy, expected ref or other authorized input blocks new publication and requires a fresh reviewed proposal. Fixed Git plumbing uses a sanitized environment and sealed settings; it does not invoke candidate hooks, filters, credential helpers or network transports to construct the result.

Before writing any result object or changing a ref, the handoff durably prepares the complete approved operation, exact Git object bytes and expected identities in its separate journal. It then writes and verifies the objects and updates only the selected local ref with compare-and-swap. A concurrent ref change conflicts without overwriting it. Successful publication retains an immutable provenance receipt. Exact replay returns the retained result without changing the executor's consumed attempt count or producing another commit.

## Inspect and recover

```sh
exacly handoffs show --journal "$EXACLY_HANDOFF_JOURNAL" \
  --handoff "$EXACLY_HANDOFF_ID" --json

exacly handoffs recover --journal "$EXACLY_HANDOFF_JOURNAL" \
  --handoff "$EXACLY_HANDOFF_ID" --json
```

Inspection and recovery use retained handoff history and do not need the original model, request file or current candidate output. Recovery verifies the pinned initialized repository and journal and retained object bytes before making a local publication decision:

| Current destination ref | Recovery behavior |
| --- | --- |
| Exact authorized before value, including authorized absence | Restore the prepared result objects and conditionally publish that exact result. |
| Exact prepared result commit | Synchronize and finalize the retained publication once. |
| Any third value | Report conflict and preserve the divergent destination. |

Required base objects must remain available. A prepared handoff is not permission to invent another base, reread changed output as a replacement or overwrite a newer branch. A historical published receipt identifies its completed operation; it does not assert that the destination ref still has that value today.

Ordinary commands refuse a journal with an interrupted SQLite rollback sidecar. Explicit storage repair preserves a protected exact backup before asking SQLite to recover it:

```sh
exacly handoffs recover-journal --journal "$EXACLY_HANDOFF_JOURNAL" \
  --authorize-recovery --json
```

Journal-only repair does not publish Git objects or refs. Inspect and recover the retained handoff separately afterward. The finite nonnegative `--lock-timeout` value, defaulting to 30 seconds, supplies one monotonic budget shared by storage coordination and Git operations. A timeout preserves retained history and requires an explicit retry; each wait or Git operation does not receive a fresh allowance. Neither storage repair nor handoff recovery starts candidate code, resets execution limits, changes state or admits evidence.

## Use the resulting target independently

The successful result identifies the approved model revision and context with the resulting Git implementation revision, together with provenance linking the selected executor receipt/build and artifact paths to that commit. Keep the original planning input, generated implementation and evidence subjects distinct.

Continue through separately reviewed binding supersession and any explicitly authorized remote publication or trusted producer run. Admit fresh evidence for the resulting revision through the existing producer policy and complete the registered verification goal through its existing completion protocol. A wrong revision, stale contract, failed or missing required observation keeps work pending. The handoff does not change existing admission or completion receipt claims automatically.

The end-to-end acceptance pilot uses real isolated execution and local Git publication with controlled trusted-evidence responses to test the composed flow and failure cases. That fixture establishes neither a live GitHub push nor a production producer run. The broader CAP-004/PROC-004 automation loop remains proposed until its supported composed behavior and evidence justify a lifecycle change.

The optional `mise run check-core` development subset excludes the handoff contract, process-fault and end-to-end modules. Some controlled cases still require local POSIX process identity, while the end-to-end pilot requires real Bubblewrap. The remaining command, Git and storage cases do not establish the complete REQ-032 contract. Only the full required Linux checks refresh the model and enter the verification gate.

## Python API

`exacly.handoff.Request` contains the reviewed policy, `execution_id`, `attempt`, `base_revision`, `ref`, `expected_revision`, artifact-to-source `mappings`, commit `message` and `author` metadata. `request_from_document(value)` parses the versioned JSON request. `preview(request)` computes its read-only proposal and `apply(request, authorization=...)` performs the separately authorized publication. `show(journal, handoff_id)`, `recover(journal, handoff_id)` and `recover_journal(journal, authorize=True)` operate from retained history. Policies and candidate documents cannot load another provider. Protocol `0.1.0` supports the declared local Git adapter only.
