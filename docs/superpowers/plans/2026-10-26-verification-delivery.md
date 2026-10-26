# Verification and Delivery Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Prepare three tested, stacked PRs for independent verification, scoped queries, and policy gates.

**Architecture:** Keep observations independent, project them against explicit implementation targets, and evaluate policy separately from conformance. Share the applicability evaluator across projection and policy.

**Tech Stack:** Python 3.12, pytest, JSON Schema, existing CLI and YAML loading.

**Spec:** `docs/verification-delivery-design.md`

## Global Constraints

- Retain Python 3.12 and existing dependencies.
- Preserve original assessments, subjects, evidence, timestamps, and source replacement semantics.
- Never infer tested artifacts or environments from source tests.
- Keep policy and target files outside recursively loaded model folders.
- Run `mise run check` for each PR before pushing.

## Review Focus

- Cross-revision bindings must not jointly establish verification.
- Missing identity must produce unknown applicability, not an inferred match.
- Expired evidence and scoped production checks must not produce readiness.
- Existing extension assessments must remain readable.
- Policy invalidity and a blocked policy are different CLI outcomes.

### Task 1: Independent verification (PR 1)

**Files:** core.py, validator cases, refresh tests, spec 0.2 and Python README.
**Interfaces:** preserve `validate_documents` and `refresh` signatures.

- [x] Change the refresh pass-without-delivery test and rule 7.8 case to expect conformance. Run them and confirm assertion failure.
- [x] Remove only the cross-dimension prerequisite. Update expired-evidence expectations and exception fixture to use an unsupported verification claim.
- [x] Clarify normative spec and README; verify all checks and commit `fix: allow verification before delivery`.

### Task 2: Applicability-aware projections (PR 2)

**Files:** new applicability.py, query.py, cli.py, refresh.py, manual_qa.py, latest state schema, test_applicability.py, test_query.py and docs.
**Interfaces:** `applicability.Target` validates explicit targets; `query.assess(model: Model, rid: str, target: Target) -> dict[str, Any]` returns explained projections consumed by stage 3.

- [x] Add regression tests for mixed revisions, target scope/build/model mismatch, legacy identity limits, missing bindings, expired support, manual-QA matching and CLI text/JSON. Watch failures before implementation.
- [x] Implement Target and subject matching, evidence support and projection. Add schema iteration, observer subjects, default query guard and assess command.
- [x] Verify preservation and deterministic reports, run all required checks, commit and push separate branch based on PR 1.

### Task 3: Explicit policy gates (PR 3)

**Files:** new policy.py and test_policy.py; cli.py and documentation.
**Interfaces:** `policy.evaluate(model: query.Model, policy: dict[str, Any], target: Target) -> dict[str, Any]`.

- [x] Add tests for satisfied/negative/unknown, negative precedence, invalid/duplicate policy input, nonconformance, target mismatch and CLI exit behavior. Watch failures.
- [x] Implement validated policy documents, read-only evaluation using assess, deterministic text/JSON gate command and user docs.
- [x] Run required checks, obtain fresh code review, fix material findings, commit and push separate branch based on PR 2.

## Execution ledger

- Authorized: all three PRs; execute inline in this chat.
- Baseline: initial direct pytest run: 338 passed, 1 failed due to absent installed exacly metadata. Restore locked environment using `mise run sync`, then rerun.
- Stage 1: RED confirmed refresh without delivery fails on rule 7.8; GREEN `mise run check`: 339 tests, lint, typing, examples and self-model passed. Ruling: preserve the historical 0.1 prerequisite; only the 0.2 config/state specification is changing.
- Stage 2: RED confirmed mixed revisions incorrectly passed, missing delivery binding was hidden, and obsolete bindings poisoned projections. Added explicit subjects, conservative targets and exclusions, assess CLI, observer metadata, and schema iteration. Fresh review found obsolete-binding and abbreviated-hash ambiguities; fixed with regressions and pairwise identity checking. No policy implementation is included in this stage.
- Stage 2 final: exact branch checked independently of stage 3: `mise run check` passed 376 tests, lint, typing, examples and self-model. Delivery scope review refinement defaults delivery environment to selected context and requires explicit platform/release scope.
- Stage 3: RED confirmed absent gate command/evaluator and expired historical failure misreported as current failure. Implemented validated policies, three outcomes, deterministic evidence explanations, CLI exit codes and nonconformance blocking. Final review scope finding was fixed in stage 2, with policy-level regressions as well.
- Final verification: `mise run check` passed 402 tests, Ruff, mypy, examples and Exacly self-model refresh/validation. Fresh stage 3 reviewer rechecked all fixes and independently passed 58 applicability/policy tests with no remaining findings.
- Publication: user explicitly approved source/test/schema/documentation pushes and PR creation to whiteredhouse/exacly after automatic review initially blocked export. PRs are drafts and stacked; no merge is authorized or performed.
