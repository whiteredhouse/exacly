# Model-level diff developer test kit

This kit is for a small pilot of the claim in the [UML spec-driven development research](uml-spec-driven-development-research.md): developers can understand an agent-made code change faster or more accurately when they see a model-level diff.

## Readiness

The Exacly repository contains the research report but no Python source code, pull requests, or agent-authored code changes. There is no real change here from which to make honest, matched stimuli. The session plan, moderator script, stimulus templates, scoring rubric, and result sheet below are ready. Before recruiting participants, select two comparable agent-made changes from one Python codebase and complete the paired stimulus sheets.

Do not present a fabricated example as evidence about real agent work. If you want to rehearse the script before finding real changes, label that session a usability rehearsal and exclude its results.

## Study question

When developers inspect an agent-made Python change, does a model-level diff help them explain what changed and its consequences more quickly or accurately than a normal code diff?

This is a directional pilot with 3–5 participants. Treat results as evidence for whether to investigate further, not as a statistically conclusive product decision.

## Design

Use two real changes, A and B, from the same codebase. Both must be agent-made and large enough that reading the diff alone is real work: each should touch at least three components or change a call path between them. Small changes are a poor test, because a code diff already handles them well and a tie would say nothing. Keep the user-visible change and complexity reasonably similar. Use the same task prompt and the same agent summary for both views of a change, so the only difference between the views is the diff itself.

Each participant sees both views, but on different changes: one code diff and one model-level diff. This avoids asking them to explain the same change twice. Counterbalance which change and view each person sees:

| Participant | First task | Second task |
| --- | --- | --- |
| 1 | A — code diff | B — model diff |
| 2 | A — model diff | B — code diff |
| 3 | B — code diff | A — model diff |
| 4 | B — model diff | A — code diff |
| 5, if available | Repeat the least-used assignment | Repeat the least-used assignment |

For three participants, use assignments 1–3. For five, choose the fifth assignment after checking which change/view pairing has the fewest observations. Randomize the order of assignment rows across participants. Do not tell participants which presentation is expected to perform better.

## Selecting the changes

For each candidate, record:

- Repository, commit or pull request, and changed files.
- Evidence that an AI coding agent produced the change (for example, task log or author disclosure). If this cannot be established, do not call it agent-made.
- One-sentence user goal, stated without revealing implementation details.
- Whether the change has tests or observable behavior that can confirm its effects.
- A brief explanation of why its size and difficulty are comparable to the other candidate.

Prefer changes that touch at least three components, or that add, remove or reroute a call between components, and that have at least one meaningful consequence or edge case. Avoid pure formatting, dependency bumps, mechanical renames across the codebase, and changes small enough to read in a couple of minutes. Record the agent's own summary of the change (its PR description or final message) for each candidate; a change without one can still be used, but note it.

## Preparing paired stimuli

Make one stimulus sheet per change, then produce two views from it.

**Shared header for both views**

- Change ID: A or B
- User goal: [one sentence]
- Starting context: [two or three sentences about the system, identical in both views]
- Agent summary: [the agent's own PR description or final message, verbatim, identical in both views; write "none" if the agent left none]
- Task prompt: “In your own words, explain what changed, how the behavior or structure now works, and one consequence, edge case, or risk you would check.”

**Code-diff view**

- Include the relevant unified diff and only the minimum surrounding code needed to read it.
- Show the agent summary from the shared header, because reviewers normally read a diff alongside it. Hide human review comments, the agent transcript, and any other generated summary.
- Do not add annotations or explanations unavailable in the source diff.

**Model-level-diff view**

- Show the agent summary from the shared header, the same as in the code-diff view.
- Build the view by the fixed recipe below, not by hand-picking what matters. The test is meant to measure what an automatic tool could show, not a summary written by someone who already understands the change.
- Do not include source code excerpts, rationale, or warnings.

**Fixed recipe for the model-level diff**

Apply every step to both changes in the same way, and record any judgement call in the facilitator copy.

1. **Components.** Treat each top-level package or module under the source root as a component. Do not merge, rename or regroup components to make the picture clearer.
2. **Structure diff.** Draw a component diagram of the components the change touches plus their direct neighbours, built from import statements before and after the change. Mark added components and dependencies in one style, removed ones in another, and leave unchanged ones plain.
3. **Interface diff.** For each touched component, list public classes and functions whose signature was added, removed or changed, as before/after signatures. Leave out private names (leading underscore) and bodies.
4. **Behaviour diff.** Run the tests that exercise the change, before and after, and record the calls between components. Draw one sequence diagram for the main path, showing only calls that cross a component boundary, with added and removed calls marked. If the tests do not reach the changed code, write "no traced behaviour" rather than drawing one from reading the code.
5. **Layout.** Use the same order every time: structure, then interfaces, then behaviour. Keep it to one page and use the same legend for both changes.
- Link each changed model element to the relevant file or symbol in the facilitator copy, but hide those links from participants during timed explanation.

**Facilitator answer key**

For each change, record the facts needed for an accurate summary before looking at participant responses:

1. Main behavior or structure change.
2. Components, functions, or interfaces affected.
3. Important call/data/state transition.
4. One real consequence, edge case, or risk supported by the code or tests.
5. Common plausible misunderstanding to watch for.

Have a second engineer check that the model view is faithful to the diff and does not disclose extra information. If no second reviewer is available, note this limitation on the result sheet.

### Stimulus sheet template

Copy this section for A and B. Keep the answer key separate from the participant-facing view.

```text
CHANGE ID:
Repository and commit/PR:
Evidence this was agent-made:
Changed files:
User goal (one sentence):
Shared context (2–3 sentences):
Why this change is pilot-sized:

CODE-DIFF VIEW
[Paste relevant unified diff; remove PR/commit descriptions and commentary.]

MODEL-LEVEL-DIFF VIEW
Components/interfaces changed:
Dependency changes:
Behavior or state flow changed:
[Attach or sketch one-page before/after model view.]

FACILITATOR ANSWER KEY — DO NOT SHOW PARTICIPANTS
1. Main change:
2. Affected components/functions/interfaces:
3. Important flow:
4. Supported consequence, edge case, or risk:
5. Plausible misunderstanding:
Faithfulness reviewer and date:
```

## Moderator script

### Before the session

1. Ask permission to take notes and time the exercise. Do not record audio or video unless the participant separately agrees.
2. Open the assigned stimulus, timer, blank response form, and answer key. Keep the answer key hidden.
3. Tell the participant they are evaluating two ways of reading changes, not being evaluated themselves.

### Opening (read aloud)

“I’m comparing two ways to inspect changes made with coding agents. I’m interested in what you understand from each view. This is a test of the presentations, not of you. You’ll see two different changes, one in each format. For each one, explain what changed, how it works, and one consequence or risk you would check. Please think aloud if that feels natural. I’ll stay quiet while you work. You can stop at any time.”

### Each task

1. Show only the assigned participant-facing stimulus and read the task prompt verbatim.
2. Start the timer when the stimulus is visible. Stop it when the participant says they are ready to submit a summary, or at ten minutes.
3. Capture their explanation as close to verbatim as practical. Do not correct, teach, or ask leading questions.
4. If they stop early, ask only: “Anything else you would want to check before approving this change?” Stop the timer after the answer.
5. After both tasks, ask the neutral comparison questions below.

### Closing questions

- “Which view, if either, helped you understand the change more clearly? What in the view helped?”
- “What information was missing or misleading in either view?”
- “If you had to review a similar change tomorrow, which view would you choose first?”

Thank them. Do not reveal the answer key until both tasks are complete.

## Scoring rubric

Score each task from 0–4 using its answer key. Give one point for each correct item:

1. Identifies the main behavior or structure change.
2. Names the relevant component, function, or interface accurately.
3. Explains the important interaction, data movement, or state transition correctly.
4. Identifies a supported consequence, edge case, or risk (or proposes a relevant check).

Do not award a point for a guess contradicted by the code. Leave an item blank if the participant did not address it.

Separately, count **wrong claims**: each statement about the change that the code, tests or answer key contradict. Keep this count beside the 0–4 score rather than subtracting it, and note each wrong claim and whether it came from the view (for example, a diagram that suggested a call that does not happen). A view that raises confidence while adding wrong claims is a bad result, not a neutral one. If a response is ambiguous, mark it for a second scorer rather than interpreting it generously.

## Session score sheet

Copy one row per task and one summary row per participant. Keep participant names out of the analysis sheet; use P1–P5.

| Participant | Change | View | Order | Time (seconds) | Main change (0/1) | Elements (0/1) | Flow (0/1) | Consequence/check (0/1) | Total /4 | Wrong claims | Misunderstandings or notes |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| P1 | A | Code / Model | 1 / 2 |  |  |  |  |  |  |  |  |

| Participant | Code score /4 | Model score /4 | Code wrong claims | Model wrong claims | Code time | Model time | Preferred first view | Main comment |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| P1 |  |  |  |  |  |  |  |  |

After sessions, report the raw participant-level results, median score, wrong claims and time by view, and the number of participants who preferred each view. With this sample, emphasize consistency and concrete errors; do not present percentages as population estimates or claim statistical significance. Note any stimulus imbalance, presentation-order effects, or answer-key disagreement.

## Decision note

Before recruiting, write down what would count as a useful signal. Suggested pilot threshold: continue to a broader test if the model view improves the accuracy score for at least two of the first three participants, or produces a clear recurring reduction in a costly misunderstanding, without adding wrong claims or substantially increasing completion time. Treat preference alone as weak evidence. Adjust the threshold before seeing results if the team has a different decision in mind.

## Inputs still needed to run

- Two comparable, verified agent-made changes from a Python codebase, each touching at least three components or a call path, with tests that exercise the change.
- A facilitator to prepare and check the paired views and answer keys.
- Three to five developers who can spend about 30 minutes each.
- A decision threshold agreed before the first session.

No participant outreach has been sent.
