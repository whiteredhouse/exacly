# Measurement coverage acceptance criteria

Status: Proposed tooling design, not implemented

Date: 2026-10-24

Specification: [Exacly 0.2 draft](../spec/versions/0.2.0.md)

Measurement coverage answers: **For each active Outcome, what is defined, what collection is demonstrated, and what can be evaluated in this context?** It supplements structural validation without changing conformance, exceptions, or the validator's exit status.

## Scope and alternatives

The first delivery is a coverage report derived from config, applicable state, and explicitly supplied evaluation timing. It reports uncertainty when the inputs cannot establish readiness or a due date. It does not execute bindings itself.

Making every linked Measure active would check adoption without establishing collection. Requiring measurement evidence for every active Outcome would incorrectly reject outcomes whose evaluation period has not completed. Separate coverage findings preserve both distinctions and are the selected approach.

Observers, release gates, view-recipe syntax, structured process steps, new state dimensions, and schema changes are outside this delivery. A project may later use these findings in its own adoption policy; that policy is separate from specification conformance.

## Inputs and evidence boundaries

The report takes a structurally valid config model, its exact revision, a selected environment or business context, an as-of date, and any supplied state. State is optional. Invalid config is reported through validation before coverage is calculated.

Only state applicable to the selected config revision and context can establish current coverage. Mismatched state remains visible as a diagnostic and does not supply current evidence. A coverage tool cannot establish lineage continuity without a known prior lineage; it reports that limitation when continuity matters instead of assuming it checked history.

An evidence reference counts only for the claim it supports, its declared revision or release, population, period, and validity. An expired reference remains visible historically but does not establish current coverage. Different population or period labels are not silently treated as equivalent. Evidence that cannot be checked is reported as unavailable, rather than accepted as proof or treated as proof of failure.

Collection readiness means evidence demonstrates that the declared collection method can obtain the required data in the selected context. A source description, binding, adopted Measure, passing unrelated test, or package release alone does not establish it. Evidence of collection can exist without a final outcome evaluation, including evidence of a working collection run before the evaluation window closes. Readiness does not establish the validity of an evaluation method or causal attribution.

A missing binding means automated observation is unconfigured. An unsupported observer, an unreachable source, an observed collection failure, and no supplied observation are distinct reasons. The report uses a specific reason only when the inputs establish it; otherwise readiness is unconfirmed. It does not infer that data does not exist from an absent binding or absent state.

An overdue finding requires an explicit evaluation due date, including any reporting delay, supplied by the project or an observer with a documented timing contract. The report identifies that input. It does not guess dates from free text such as "Quarterly" or "First complete quarter after rollout". Without an unambiguous schedule, evaluation timing is unconfirmed. A result is overdue only after its due date, not on that date. Dates are evaluated in the project's declared evaluation timezone when timestamps require conversion; absent a timezone, an ambiguous timestamp cannot establish that a deadline has passed.

The input contract above describes information the future tool needs, not new fields on 0.2 records or state. Existing evidence references and referenced artifacts may supply the detail. An implementation must document how it reads timing and evidence applicability before claiming to automate those decisions.

## Report behavior

The report covers every active Outcome, lists its linked Measures and their lifecycle, and distinguishes definition adoption, collection readiness, and evaluation evidence. Proposed Outcomes may be inspected explicitly; retired Outcomes are excluded from current coverage. Proposed and retired Measures remain visible, but do not count as adopted current measurement definitions.

Each finding identifies the Outcome, relevant Measure IDs, config revision, selected context, as-of date, severity, reason, evidence or missing input, accountable owner, and next action. The Outcome owner is accountable for the finding; a Measure's owner is shown as the collection contact where relevant. Next actions describe the needed observation or decision without automatically creating tasks or editing config or state.

Coverage severities are separate from validator severities:

- **Warning:** an adoption or evidence gap, a demonstrated collection blocker, or a missed evaluation deadline.
- **Information:** an expected wait, unknown timing, or a limitation that prevents a stronger conclusion.

No coverage finding changes the validator's conformance result or is automatically waived by an exception to a structural rule. Existing validation findings, such as an unknown baseline, are linked rather than duplicated without explanation.

With multiple Measures, the report preserves each Measure's status. One ready Measure does not conceal another's missing evidence. If the Outcome's success criterion requires several measurements, one successful measurement does not establish complete evaluation. If the required combination is not explicit, the report shows the individual evidence and leaves overall evaluability unconfirmed rather than inventing an AND/OR rule.

## Acceptance scenarios

Unless specified otherwise, scenarios use an otherwise conforming 0.2 model with active `OUT-001`, a linked `MEAS-001`, the selected config revision and context, and an as-of date of 2026-10-24. Timing is explicitly supplied where a scenario relies on a deadline. These are behavioral acceptance criteria for future tooling, not executable validator cases.

| ID | Given | Expected coverage behavior |
| --- | --- | --- |
| MC-01 | `OUT-001` has no linked Measure | Show the existing rule 7.1 error as missing measurement definition. Do not substitute a readiness finding or change the validation result. |
| MC-02 | Its only linked Measure is proposed | Warn that no current measurement definition is adopted. List the proposed Measure. Do not assert that data collection or evaluation is impossible. Validation continues to conform. |
| MC-03 | The Measure is active but there is no state | Warn that collection readiness is unconfirmed because no observation was supplied. Do not mark collection failed, operation unavailable, or the outcome target missed. |
| MC-04 | The Measure has no binding and no collection evidence | Warn that collection readiness is unconfirmed; identify automated observation as unconfigured. Do not assert that the underlying data is absent. |
| MC-05 | A binding exists, but no observer result is supplied | Warn that collection readiness is unconfirmed because no observer result was supplied. Do not infer success or that the observer is unsupported merely from the binding. |
| MC-06 | An observer reports an unsupported source kind, an unreachable source, or a failed collection run | Warn with the specific reported reason and evidence locator. Keep the three reasons distinct. Do not classify any of them as the outcome target being missed. |
| MC-07 | The baseline is `unknown`, with its required owner and collection action | Reference the existing unknown-baseline warning. Explain its effect only where the declared success criterion needs a baseline; do not claim every absolute target is unevaluable without one. |
| MC-08 | Collection is demonstrated and the evaluation due date is 2026-11-15 | Show collection readiness as demonstrated and evaluation as awaiting its due date, with information severity. Do not warn solely because the final result is not yet available. |
| MC-09 | Collection failed before the same future due date | Warn about the collection blocker even though final evaluation is not due. An open window does not hide a known readiness problem. |
| MC-10 | No evaluation result is available and the due date is 2026-10-24 | Show evaluation as due today, with information severity. It is not yet overdue. |
| MC-11 | No evaluation result is available and the due date is 2026-10-23 | Warn that evaluation is overdue and identify the missing result and timing input. Do not infer `target_not_met`. |
| MC-12 | The only result expired on 2026-10-23, and evaluation is due | Reference the existing `11.evidence-expired` warning and explain that the expired result leaves current evaluation unsupported, rather than issue a duplicate expiry finding. Keep its historical result visible without using it for current coverage. Evidence expiring on 2026-10-24 remains valid that day. |
| MC-13 | The only result belongs to another config revision, context, population, or evaluation period | Warn about a confirmed applicability mismatch and leave evaluation unsupported for the selected inputs. Reference any existing `11.state-revision` warning rather than duplicate it. If applicability cannot be determined, report that limitation with information severity. Do not silently reuse the result. |
| MC-14 | Applicable, unexpired measurement evidence supports a current outcome assessment | Show evaluation as supported and display its actual assessment, including `target_not_met` or `inconclusive`. A supported negative result is not missing measurement coverage. |
| MC-15 | One linked Measure is ready and another is unconfirmed | Show both statuses and warn about the Measure whose collection readiness is unconfirmed. Do not claim complete evaluation unless the evidence covers the declared success criterion. If the required combination of measurements is unspecified, leave overall evaluability unconfirmed and report the missing combination rule with information severity. |
| MC-16 | The only linked Measure is retired | Reference the rule 7.1 validation error as missing current measurement definition, rather than issue only a coverage warning. Retain the historical link and evidence without counting retirement as current adoption. A valid rule 7.1 exception remains visible as conforming with exceptions; coverage does not hide the missing definition. |
| MC-17 | Cadence and evaluation period are free text with no resolvable due date | Report timing as unconfirmed, with information severity and a next action to establish the schedule. Do not guess an overdue date. Collection gaps remain independently visible. |
| MC-18 | All records in the invoice-reminders example remain proposed | Do not label its Outcome as an active-outcome coverage gap. An explicitly requested proposed-outcome view can show its planned measurement separately. |

For MC-02 through MC-18, except MC-16's existing rule 7.1 error, these coverage findings alone do not change conformance or the validator's exit status. MC-01 and MC-16 display structural validation results; coverage does not replace or escalate those results. All scenarios preserve input documents. Repeating a report with identical inputs produces the same findings and deterministic ordering by Outcome ID, then Measure ID and finding kind.

## Review and implementation boundary

The draft clarifications and these criteria can be reviewed independently of a report implementation. Before implementation, select a concrete source for collection evidence and evaluation timing, and turn the scenarios into coverage tests. Existing schema and validator suites remain regression checks; coverage expectations belong in a separate suite rather than changing existing conforming cases into failures.
