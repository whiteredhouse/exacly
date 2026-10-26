# Verification before delivery: research and solution options

Research date: 2026-10-26. Related issue: [#32](https://github.com/whiteredhouse/exacly/issues/32).

Status: research and proposed direction for discussion, not an approved implementation design. Repository evidence was inspected at commit `57e3317`; no product code was changed.

## Recommendation

Allow verification independently of delivery. Keep model conformance, evidence-backed assessment, and release/deployment policy as separate questions. Follow the small correction with applicability-aware queries before claiming that Exacly establishes whether the delivered implementation is verified.

The important distinction is between these claims:

1. The model and its supplied claims conform to the specification.
2. The configured checks passed for identified source code, artifacts, or a deployed system, within stated coverage.
3. An identified implementation was published or deployed to a particular target.
4. A named policy's prerequisites are satisfied for a particular action or target.

None of these is a universal lifecycle stage. A conforming model can describe failed tests. Passing source tests can precede publication. A deployment can exist while acceptance checks fail. Deployment policy can block promotion without making the recorded test results invalid.

This recommendation is a synthesis of the sources below, rather than a claim that one external standard supplies Exacly's complete model.

## What other systems do

### GitHub: checks are observations; deployment has its own identity and gates

A check run is created for a commit and carries its own status and conclusion. Deployments separately identify a ref and target environment, with subsequent deployment statuses. The deployment API supports required commit-status contexts, and Actions environments support protection rules before a job proceeds. This places prerequisites at the deployment boundary; it does not require deployment before recording a successful check. A deployment request alone also does not establish successful deployment.

Sources: [check runs](https://docs.github.com/en/rest/checks/runs), [deployments](https://docs.github.com/en/rest/deployments/deployments), [environment protection rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

Lesson for Exacly: preserve results independently and evaluate readiness for an explicitly selected target. Do not treat the existence of a deployment record as proof of success.

### GitLab: requirement satisfaction can come from CI

GitLab accepts requirements reports from CI jobs. Individual requirement IDs can be reported as passed or failed; omitting an ID records no result for it. The documented workflow does not require a release or deployment to record requirement satisfaction.

Source: [requirements management](https://docs.gitlab.com/user/project/requirements/).

Lesson for Exacly: this is a direct requirements-management precedent for #32. The documented workflow is not, by itself, a complete solution to Exacly's revision and environment applicability problem; copying a single global “satisfied” badge would leave that problem unresolved.

### in-toto: test results name a subject and configuration

The Test Result predicate describes an invocation of a test suite, including its result, configuration, optional run URL, and individual test results. Its example identifies tested source by Git commit. Policy can require the appropriate configuration and subset of tests, including different device targets. The Statement layer binds claims to immutable subjects identified by digest.

Sources: [Test Result v0.1](https://github.com/in-toto/attestation/blob/main/spec/predicates/test-result.md), [Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md).

Lesson for Exacly: distinguish the requirement being assessed from the concrete implementation being tested. Test execution configuration and criterion coverage matter alongside revision identity. Borrow the identity model without requiring users to adopt signed attestations.

### SLSA: connect source and artifacts through provenance

Build provenance identifies output artifacts and records inputs, including resolved source revisions. A Verification Summary Attestation records a verifier's policy evaluation for particular artifacts; consumers check that the subject matches the artifact they intend to use. SLSA verification concerns supply-chain properties, not functional acceptance tests.

Sources: [build provenance v1.2](https://slsa.dev/spec/v1.2/build-provenance), [Verification Summary Attestation v1.2](https://slsa.dev/spec/v1.2/verification_summary).

Lesson for Exacly: a commit and an artifact digest are different identities. A provenance link can establish where an artifact came from; it does not transform source-test results into evidence that the built artifact or deployed environment was directly tested.

### CDEvents: testing, publication, and deployment are different event families

CDEvents defines test-suite execution and test-output events separately from deployment events. A service-deployed event identifies its environment and artifact. Its vocabulary also distinguishes deployment, upgrade, rollback, removal, and service publication.

Sources: [testing events](https://github.com/cdevents/spec/blob/main/testing-events.md), [continuous deployment events](https://github.com/cdevents/spec/blob/main/continuous-deployment.md).

Lesson for Exacly: observations can arrive independently and in different orders. Correlate them through explicit identities rather than infer causality or applicability from timestamps or shared requirement IDs. A full event bus is unnecessary for this principle.

### Eiffel: confidence is explicitly derived for a subject

Eiffel's Confidence Level Modified event supports success, failure, and inconclusive confidence statements. A subject identifies what the statement applies to, and confidence-basis links can identify supporting test or confidence events. Confidence names can express different abstractions, such as smoke-test success or releasability for a customer segment.

Source: [Confidence Level Modified event](https://github.com/eiffel-community/eiffel/blob/master/eiffel-vocabulary/EiffelConfidenceLevelModifiedEvent.md).

Lesson for Exacly: “ready for release” is a derived, scoped judgment, not a synonym for one test result. Preserve the supporting observations and make the derivation explainable. The linked file is evolving repository documentation, not a recommendation to adopt every current event field.

### Cloud Deploy and Skaffold: verification can mean a post-deployment check

Cloud Deploy runs configured verification tasks after deployment. A verification failure can fail the rollout, and verification can be retried. Skaffold likewise describes its verify stage as post-deployment testing. These tools deliberately use a narrower meaning of verification than generic CI testing.

Sources: [Cloud Deploy verification](https://docs.cloud.google.com/deploy/docs/verify-deployment), [Skaffold verification](https://skaffold.dev/docs/verify/).

Lesson for Exacly: separate “tests passed for revision A” from “the deployed system in production passed its acceptance checks.” Both fit the verification dimension, but their subjects and scope differ. This counterexample means the right conclusion is independence of generic verification, not that deployment can never be a prerequisite of a particular check.

### in-toto SVR: policy results are their own scoped statements

Simple Verification Result records a verifier, evaluation time, policy references, and properties established for subject artifacts. It supports multiple verification statements for the same subject. Its properties summarize successful policy evaluation rather than replace detailed test observations.

Source: [Simple Verification Result v0.2](https://github.com/in-toto/attestation/blob/main/spec/predicates/svr.md).

Lesson for Exacly: if policy decisions are eventually persisted, identify their target, inputs, policy version, and time. For #32, an ordinary derived report is sufficient; a new stored decision format is optional future work.

## What is happening in Exacly

### The immediate issue is deliberate coupling

`python/src/exacly/core.py:530` implements rule 7.8. It independently checks supporting delivery and verification evidence, then also requires a supported released assessment whenever any passed verification exists for a Requirement. That final requirement is grouped only by requirement ID, not by matching tested implementation or environment.

The specification's section 7.8 uses “additionally,” supporting this interpretation. However, section 8 describes contextual dimensions separately, and the worked example at `spec/versions/0.2.0.md:307` records implementation/test verification before deployment delivery. This is a semantic inconsistency to resolve explicitly, not just an observer defect.

An in-memory reproduction using the existing refresh test fixture generated a passed verification assessment for one bound test at revision `0b6ad3b`. Validation produced only the reported 7.8 delivery-prerequisite error. No files were changed for that experiment.

### Two kinds of context already coexist

The state document's `context` is an environment or business context. Assessment `context`, by schema, can instead be a revision, release, environment, or population. Refresh writes the tested revision into verification assessment context and the distribution context into delivery assessment context.

Consequently, requiring equal assessment-context strings would be an incorrect fix. The model needs to distinguish the observation's container context from the identity and applicability of what was assessed.

### Query aggregation can combine different revisions

`query.Model.results()` groups by state-document context and dimension, then emits result summaries without assessment context or evidence applicability. `status()` combines those summaries. `refresh.carry()` preserves other observers' and bindings' assessments, even when a new state document names a later config revision.

A second in-memory experiment used two bindings:

1. Unit tests passed at revision A.
2. Integration tests passed at revision B, retaining the unit result.
3. Distribution delivery was observed at B using the existing fixture's implementer reference.

The retained verification assessments correctly named A and B individually. Nevertheless, validation returned `conforms`, the query returned `main: {verification: passed, delivery: released}`, and unverified coverage was empty. This demonstrates a separate aggregation problem; it is not evidence that every normal refresh has this outcome.

Preserving an old result is useful. Reinterpreting it as coverage of a new revision is not. Merely removing the delivery prerequisite will not fix this aggregation.

### Existing work provides a starting point

The manual-QA extension already uses a target manifest with model revision, release, builds, and due criteria. It compares observations against exact release/build inputs and checks retained output digests. This is relevant internal experience for an eventual shared applicability contract, not a reason to move all extension fields into core unchanged.

The measurement-coverage criteria already separate conformance from coverage and treat mismatched applicability as insufficient support for the selected question. Verification should follow the same reporting principle.

## A cleaner conceptual model

Keep these identities separate, even if some share the same Git commit in a simple project:

| Concept | Question answered |
| --- | --- |
| Model/config revision | Which requirement definitions, criteria, and bindings were used? |
| Implementation subject | Which repository revision, artifact digest, or component composition was assessed? |
| Assessment scope | Which environment, platform, configuration, criteria, or period limits the claim? |
| Observation identity | Which source/binding/run produced the result, and when? |
| Delivery target | Where was an identified implementation published or deployed? |
| Policy and selection | What decision is requested, for which target, under which prerequisites? |

“Test runner executes in CI” and “system under test is staging” can both be true. Execution location must not silently determine the assessed environment. Similarly, `main` is a useful label but cannot replace immutable implementation identity.

For a selected target, aggregation should first select applicable observations, then resolve supersession within a source/binding and subject, then evaluate required checks. Only then should it combine results. Old failures must not poison a different subject; old passes must not satisfy that subject. Current failures must remain visible. Missing, ambiguous, or expired support leaves the corresponding conclusion unestablished.

Source tests, artifact tests, and deployed-system checks support different claims. A policy may accept source-test evidence plus an explicit source-to-artifact provenance link as one release prerequisite. That does not justify labeling the artifact directly tested or production behavior verified.

## Solution options

| Option | Shape | Benefit | Cost or limitation |
| --- | --- | --- | --- |
| A. Minimal semantic correction | Make verification and delivery independent under 7.8; retain existing state format | Small change; resolves #32 directly | Existing summaries can still mix revision applicability; no reliable combined claim |
| B. Independent assessments plus applicability-aware projections | A, followed by explicit target selection, shared applicability rules, and separate policy/coverage reporting | Correct observations and trustworthy summaries; builds on existing model | Query contracts and likely state evolution require design and migration |
| C. Evidence/event graph with attestations | Immutable observation identities, explicit subjects, provenance links, materialized projections and policy decisions | Strong historical traceability and external integration | Much larger storage, migration, and operational scope than #32 needs |

Recommendation: B as a staged direction. Implement A as its independently reviewable first step; do not present A as solving all combined-status questions. Defer C unless real consumers need historical queries or signed supply-chain interoperability. A snapshot state with retained external evidence can support the proposed direction without an event store.

## Proposed staged solution

### Stage 1: correct the semantics of rule 7.8

Proposed normative wording:

> A Requirement claimed delivered MUST have an identified implementer and applicable delivery evidence. A Requirement claimed verified MUST have an identified implementer and applicable verification evidence. Verification does not require delivery, and delivery does not imply verification. A combined claim MUST establish applicability of both assessments to the implementation and scope claimed.

Keep existing test-result semantics, supporting evidence checks, expiry handling, and implementer requirements. Missing delivery is a coverage gap, not fabricated delivery, failed delivery, or an invalid test observation. “Delivery not observed” is a derived display condition; it need not become a new stored delivery enum.

Update the specification, worked examples, validator cases, and refresh-to-validation cases together. Review the existing cross-context test: after removing the prerequisite it may conform for a different reason, so its description and assertions must no longer imply that unrelated delivery validates verification.

Stage 1 should make no new combined readiness claim. The query applicability problem above should be a separately tracked follow-up, with current summaries described as observations in a context rather than proof for a selected release.

### Stage 2: make queries answer a selected question

Specify a target selection contract before changing schemas. At minimum it must distinguish model revision, tested subject, and assessed scope; support component/build compositions where needed; and explain how evidence maps to each identity.

Then define a shared applicability evaluator used by status, coverage, views, and any future policy gate. Preserve original observation identifiers and timestamps in explanations. Do not silently match free-text claims or mutable branch labels. Missing linkage produces an explicit unknown applicability result.

For legacy state, infer only what an observer's documented format guarantees. For example, refresh's verification assessment context identifies its tested revision. Generic assessment-context strings have no universal parseable meaning. Do not invent artifact digests, build provenance, or deployment identity. Legacy evidence may remain valid for its original claim while being insufficient for a new combined question.

Keep publication and environment deployment distinguishable in reports. The existing delivery dimension can remain, with explicit target selection, until concrete requirements justify a schema split.

### Stage 3: optional policy gates

Once applicability is reliable, projects can define release or deployment prerequisites separately from conformance. Initially this can be a report or a narrowly scoped command; no policy engine is required for #32.

A gate needs at least satisfied, not satisfied, and unknown outcomes, with reasons and input evidence. Automation can block on either of the latter two without confusing “no evidence” with “tests failed.” A result should name the policy version, selected implementation/target, and evaluation time.

## Proposed user-facing presentation

For revision A before delivery:

```text
Requirement: Invoice total
Model: M1                 Implementation: repository revision A
Configured verification: passed — all applicable bound checks passed
Delivery: not observed
Model conformance: conforms
```

For revision B with old unit evidence from A:

```text
Requirement: Invoice total
Model: M1                 Implementation: repository revision B
Unit checks: no applicable result — retained result is for A
Integration checks: passed for B
Configured verification: not established
Distribution: observed for B
```

For production running an artifact derived from tested source:

```text
Production: artifact sha256:D observed deployed
Source checks: passed for A; build provenance links A to D
Production acceptance checks: not observed
Production behavior verified: not established
```

These are proposed report labels, not existing commands or new persisted enum values. An eligible-for-deployment policy could pass in the last example while production acceptance remains unknown, depending on the declared prerequisites.

## Acceptance scenarios to add

| Scenario | Required behavior |
| --- | --- |
| Bound tests pass for A; no delivery | Conforms; verification records A; delivery remains unobserved |
| Verification claims pass without applicable supporting evidence | Rejected under the evidence rule |
| Delivery claims released without supporting evidence | Rejected under the evidence rule |
| Bound tests fail, are skipped, or missing | Existing observer semantics retained; do not invent delivery |
| Unit result A plus integration result B | Preserve both; no complete verification for B |
| A is tested; unrelated B is delivered | No combined claim for B |
| Staging acceptance passes; same artifact is in production | Staging result retained; no inference of production acceptance |
| Source A tested; artifact D built from A | May satisfy a declared source-test prerequisite with linkage; do not claim direct artifact testing |
| Delivery later observed without reobserving the test source | Preserve the earlier result and its original subject/scope/time |
| The test source is reobserved with no selected tests | Existing not-checked semantics apply; distinguish this from not observing the source |
| Requirement criteria or bindings changed | Do not reuse an old pass as proof of the new requirement without an explicit compatibility contract |
| Result expired or applicability cannot be determined | Historical result visible; current support unestablished |
| Rollback to previously tested subject | Re-evaluate applicable evidence and freshness for that subject; do not copy the newest revision's status |
| Only distribution publication is observed | No inferred installation, deployment, or runtime health |
| One required applicable binding fails | Failure remains visible even if other applicable bindings pass |

## Scope and limits

This research compares documented data models and behavior; it is not a benchmark or deployment trial of the external products. GitHub and GitLab docs are living product documentation; several linked specifications are evolving repository documents. Versioned SLSA pages and version-labelled in-toto predicates are identified where available. Their use of “verification” differs, so terminology alone is not evidence of identical semantics.

The repository experiments were in-memory reproductions using existing fixtures. They establish the particular validator and query behaviors described, not full regression coverage. The proposed long-term field names, query API, migration rules, and policy syntax remain to be designed after selecting an approach. No GitHub comments, issue edits, or product implementation changes were made.
