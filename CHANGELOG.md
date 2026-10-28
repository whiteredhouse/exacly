# Package release notes

## Unreleased

- `exacly refresh --model-revision MODEL --revision IMPLEMENTATION` records verification against an explicitly identified model and implementation. The enclosing state's config revision follows the model; assessment context and evidence applicability follow the implementation. Omitting `--model-revision` preserves the existing single-revision behavior and retained observations keep their original identities.
- Distribution refresh rejects distinct model and implementation revisions. The new identity option labels caller-supplied model files and results; it does not load Git revisions or authenticate evidence. GitHub's automatic source link follows the selected implementation revision, while an explicit `--source-url` remains unchanged.
- `exacly plan --policy FILE --target FILE --date YYYY-MM-DD` proposes read-only work for Requirement verification prerequisites. It distinguishes investigation of a known failure, collection of missing evidence and missing observation configuration, while satisfied prerequisites generate no action.
- Text and JSON views share a versioned plan with input fingerprints, stable action IDs and observable completion conditions. `--out` saves JSON outside model inputs. Planning does not dispatch work or establish evidence authenticity; readiness exit behavior remains on `gate`.

## 0.5.0

- Case-bound verification now requires every selected case to pass: mixed passing/skipped outcomes give `inconclusive`, retaining evidence and naming skipped cases alongside any missing patterns. Failures still give `failed`; all-skipped selections remain `not_checked`. This supersedes the permissive pass-plus-skip behavior accepted in #24. Verification readiness gates return `unknown` for these incomplete results and exit 1.
- Reobserve affected bindings after upgrading. Existing serialized `passed` assessments keep their original meaning until refreshed; do not rewrite historical state by hand. Broad case patterns that include platform-specific skips or pytest expected failures may now prevent a readiness gate from passing. Job-only bindings and duplicate-case-ID merging are unchanged.

## 0.4.0

- Verification and delivery are independent in 0.2 config/state validation. Passing tests can conform before publication or deployment, while each positive claim still needs its own applicable evidence and identified implementer. Historical 0.1 product-map semantics are unchanged.
- `exacly assess RECORD --target FILE` projects observations onto an explicit model revision, implementation revision or build composition, and selected scope. Reports explain exclusions and missing bindings.
- `exacly gate --policy FILE --target FILE` evaluates versioned prerequisites separately from model conformance. It exits 0 for satisfied, 1 for a known unmet or unknown prerequisite, and 2 for invalid input.
- Refresh and manual-QA observations retain structured subjects. Mixed revisions, obsolete bindings, ambiguous Git abbreviations and unsupported evidence cannot establish a passed scoped assessment.
- Exacly's own CI requires current verification for every active Requirement before publication. Wheel smoke checks run outside the checkout; a separate post-publication job installs the exact package version from PyPI and exercises the CLI.

### Upgrading

Existing 0.2 documents remain readable. Newly written state may include the optional `subject` field from the 2026-10-26 state-schema iteration; upgrade observers and validators together, since older validators may reject that field. Reobserve legacy state to establish implementation-specific applicability rather than adding identities by hand.

Keep target and policy files outside model folders. Start with a verification-only policy before release. Require delivery only when evaluating evidence of publication or deployment; source-test success does not establish production-environment verification. An omitted delivery environment selects the target's named context, while verification without environment makes a source-level claim.
