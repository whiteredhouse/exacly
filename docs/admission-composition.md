# Compose evidence admission and readiness

**Unreleased functionality on main.**

A composition checks every member of an independently reviewed required set. Each
member keeps its own producer trust policy, approved model, explicit source target
and readiness policy. Exacly performs fresh native GitHub admission and then
evaluates that member's gate against only its freshly derived state. The aggregate
is satisfied only when all required admissions are accepted and all gates are
satisfied.

This is useful when several native platform producers must independently verify
one implementation. Each producer continues to use the existing push-only,
single-job [admission contract](../python/README.md#admit-github-actions-evidence).
The composition does not relax that contract or turn diagnostic matrix jobs into
approved producers.

## Filesystem CLI and input bounds

Both composition commands require a supported local POSIX filesystem with
no-follow and nonblocking file opens, private file modes and directory syncing.
The CLI checks for the required primitives before opening input content or
retrieving GitHub evidence. Input documents must be regular UTF-8 JSON or YAML
files with string mapping keys and finite JSON-compatible values. Duplicate
keys, YAML anchors and aliases, and excessive nesting are rejected.

Each document is limited to 4 MiB, nesting depth to 64 and decoded value nodes to
200,000; YAML parsing also has a token bound. Resolving a definition is limited
to 1,024 distinct files and a 64 MiB input-material budget, counting repeated use
of a document by different members. The selection document also has the per-file
limits. Output setup permits at most 128 missing parent directories.

The `exacly.composition` API accepts parsed inputs and leaves result-bundle
publication to its caller, so it remains independent of these filesystem CLI
requirements. Existing model validation and each producer's native admission and
readiness guarantees retain their own contracts.

## Review the required definition

Keep the composition definition and its referenced inputs under the same review
and storage controls as your trust policies. The definition is authoritative for
which members are required and which policy belongs to each member. Run selections
are a separate input: removing a required member from the selection is an error.
Changing the required definition itself requires independent review.

For example, a repository can keep these files:

```text
trusted/
  native-composition.yaml
  policies/linux-amd64.json
  policies/darwin-arm64.json
  gates/linux-amd64.yaml
  gates/darwin-arm64.yaml
spec/exacly/releases.yaml
```

`trusted/native-composition.yaml`:

```yaml
schema_version: "0.1.0"
name: native-release-verification
version: "1"
members:
  - id: linux-amd64
    trust_policy: policies/linux-amd64.json
    readiness_policy: gates/linux-amd64.yaml
    model_root: ..
  - id: darwin-arm64
    trust_policy: policies/darwin-arm64.json
    readiness_policy: gates/darwin-arm64.yaml
    model_root: ..
constraints:
  - kind: same_revision
    members: [linux-amd64, darwin-arm64]
  - kind: same_model
    members: [linux-amd64, darwin-arm64]
```

All definition fields shown above are required; use `constraints: []` when no
cross-member equality is needed. There must be between one and 64 uniquely named
members. Member IDs contain one to 64 ASCII letters, digits, underscores or
hyphens and start with a letter or digit; they are used in retained output paths.
There can be at most 128 consistency groups. Unknown fields and unknown or
duplicate member references are rejected. References are portable relative paths;
parent-directory references are permitted, while absolute paths, backslashes and
NUL characters are rejected.

Policy paths and `model_root` resolve relative to the definition file's directory.
The model root is explicit. Exacly loads only the canonical repository-relative
paths already selected by that member's trust policy under `model.files`; it does
not recursively discover model documents. In this example, a policy selecting
`spec/exacly/releases.yaml` reads that file beneath the repository root named by
`model_root: ..`.

Each supplied model must be conforming 0.2 config without observed state. Its
selected canonical file names and content digest must match the approved trust
policy. A gate cannot use an edited working-tree model merely because its
namespace or declared revision matches.

The readiness policies select each member's own required records. For example,
the Linux policy can require a Linux-specific Requirement while the Darwin policy
requires its corresponding Darwin Requirement. The consumer decides which native
platforms and checks are necessary and independently approves the producer
policies; Exacly does not infer those decisions from member names.

Validate the complete definition locally:

```sh
exacly composition validate --definition trusted/native-composition.yaml \
  --date 2026-10-30 --json
```

Validation resolves and checks all referenced policies and model inputs without
GitHub access. Its reported `definition_digest` binds the definition and the
resolved trust policies, readiness policies and approved named model documents.
It therefore changes when a member is assigned a different producer policy, a
readiness policy is weakened or model content changes. Inspect the inputs and
record the digest through your independent review process. The validation command
computes identity and conformance; it grants no approval.

## Select exact runs and targets

The selection document pins the reviewed definition digest and provides exactly
one selection for every required member. It cannot add, omit or duplicate members.

For example, `native-selection.json` has this shape. Replace the example digest,
run IDs and full revisions with the actual independently reviewed definition and
selected runs:

```json
{
  "schema_version": "0.1.0",
  "definition_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "members": [
    {
      "id": "linux-amd64",
      "run": 123456789,
      "attempt": 1,
      "target": {
        "context": "native-ci",
        "model_revision": "1111111111111111111111111111111111111111",
        "revision": "2222222222222222222222222222222222222222"
      }
    },
    {
      "id": "darwin-arm64",
      "run": 987654321,
      "attempt": 1,
      "target": {
        "context": "native-ci",
        "model_revision": "1111111111111111111111111111111111111111",
        "revision": "2222222222222222222222222222222222222222"
      }
    }
  ]
}
```

Run and attempt must be positive integers. Native source admission accepts only
`context`, `model_revision` and `revision` in the target, with full 40-character
commit SHAs. The model revision must match that member's approved trust policy.
Keep platform labels in member IDs and approved producer assignments; adding
`target.platform` does not extend the supported source-admission contract.

`same_revision` compares repository identity and the selected full implementation
revision within its named group. `same_model` compares repository identity,
namespace, approved model revision, selected model files and config digest. These
constraints apply only to their explicit groups. A composition involving different
repositories can leave them ungrouped or declare separate repository-specific
groups; participation alone does not require a shared Git SHA or namespace.

The whole definition, referenced inputs, exact member selections, consistency
constraints and output paths are checked before evidence retrieval. A bad later
member prevents earlier members from beginning admission. The invocation retains
fixed inputs rather than rereading changed policy files between members.

## Evaluate and retain the result

Use the same `GH_TOKEN`, falling back to `GITHUB_TOKEN`, as single-target admission.
The token needs read access to every selected repository and its evidence. Do not
place credentials in either document.

Select a new output directory outside loaded model directories and input paths.
The destination itself must not exist. Missing parent directories are created
privately; existing parent permissions are preserved. A temporary parent is
convenient for an individual invocation:

```sh
composition_output=$(mktemp -d)
exacly compose github-actions --definition trusted/native-composition.yaml \
  --selection native-selection.json --date 2026-10-30 \
  --out "$composition_output/invocation" --json
```

Exacly evaluates members sequentially through `admission.admit` and the native
readiness evaluator. A completed failing report can be admitted as an authentic
failure observation. Its gate still blocks readiness. Failed, skipped, incomplete,
missing, inapplicable, rejected or unavailable evidence cannot establish a
satisfied aggregate. Normal member rejection does not discard successful members
or stop the remaining independent members from producing diagnostics.

Member models and state remain separate even when they use the same context.
Exacly neither combines same-context state documents nor relabels another model's
namespace to satisfy the consumer's gate.

Retained output has this layout:

```text
invocation/
  report.json
  members/
    linux-amd64/
      receipt.json
      archive.zip
      junit.xml
      state.json
      gate.json
    darwin-arm64/
      ...
```

An admitted member retains its native receipt, archive, JUnit report, freshly
derived state and readiness result. A rejected admission retains only its receipt;
there is no gate result for unadmitted evidence. The aggregate identifies every
required member, exact selection, policy/model identity and individual outcome.
Directories use mode `0700` and files use `0600` independently of umask. Existing
destinations, input aliases and output placement within loaded model directories are
refused.

The retained aggregate starts with `complete: false` and a blocked result. A final
complete aggregate is published atomically after required member output has been
retained. A missing, incomplete or partially written aggregate is never an
all-members success. Reading one member's passing result is insufficient to claim
that the whole composition passed.

| Exit | Meaning |
| --- | --- |
| `0` | Final `complete: true` aggregate with `result: satisfied`; every required member was admitted and ready. |
| `1` | Final complete aggregate with `result: blocked`; inspect the member admission and gate outcomes. |
| `2` | Invalid inputs or an operational/output error; no aggregate success is established. |

If interrupted or unable to retain required output, preserve the directory for
diagnostics. A rerun uses a new destination and performs fresh admission for every
member. Prior successful receipts or member bundles never silently replace those
live checks. There is no cross-invocation resume or cache substitution.

Admission's HTTP operations retain their existing per-request timeouts. The
composition is sequential and has no separate whole-invocation deadline or
parallel scheduling contract.

## Python validation and evaluation

The supported `exacly.composition` API accepts parsed definition and selection
documents plus explicit member inputs. File resolution belongs to the CLI;
Python callers supply each model document under the canonical repository-relative
name selected by the trust policy:

```python
from exacly import composition

inputs = {
    "linux-amd64": composition.MemberInputs(
        trust_policy=linux_trust_policy,
        readiness_policy=linux_readiness_policy,
        documents={"spec/exacly/releases.yaml": approved_release_model},
    ),
    "darwin-arm64": composition.MemberInputs(
        trust_policy=darwin_trust_policy,
        readiness_policy=darwin_readiness_policy,
        documents={"spec/exacly/releases.yaml": approved_release_model},
    ),
}
prepared = composition.validate_definition(definition, inputs, date)
# Independently review prepared.as_dict() and preserve prepared.digest.
composition.validate_selection(prepared, selection, date)
result = composition.evaluate(source, prepared, selection, date)
report = result.report
```

`source` implements the existing admission source interface. Pure validation
performs no source calls. `evaluate` validates the complete selection again before
fresh admission and returns the aggregate plus per-member native admission
results. Callers that retain results must preserve the same completeness and
freshness rules as the CLI.

The supported `admission.validate_request`, `admission.validate_model` and
`policy.validate_policy` helpers expose the single-target preflight checks reused
by composition. Validation and ordinary single-target admission/gate behavior
share their implementation.

## Claims and consumer responsibilities

An aggregate preserves the claims and limitations of its member receipts. It is
unsigned and does not authenticate itself, verify test honesty, establish
acceptance coverage or prove that an executor produced a revision. Satisfied
readiness does not grant completion, release or deployment authorization.
Composition retains diagnostic state in its private bundle; it does not publish
authoritative model state or update execution ledgers and pending work.

Keep required platform lists, native package/service tests, exact external release
inventories, producer selection and policy approval in the consumer repository.
Equal observing checkout revisions do not prove that independently discovered
external release pairs were the same. Pin or compare those consumer inputs through
the consumer's own contract. The generic composition does not interpret
product-specific JUnit text or choose a latest run or release.
