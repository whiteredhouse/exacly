---
name: exacly
description: Looks up and keeps true a repository's Exacly product model (YAML config in spec/exacly, state written by CI). Use when a task changes behaviour, code, tests or infrastructure that the model describes, when it changes spec/exacly itself, or when the user mentions Exacly, requirements, traceability or the product model.
---

# Working with an Exacly product model

The repository describes its product as an Exacly model: **config** that people and agents author (YAML in `spec/exacly/`), and **state** that CI writes from observations (`spec/exacly/state/`). Config says what the product is meant to be and why; state says what has been verified and released, with evidence. Keep config true to every change you make.

Ask the model with the `exacly_*` MCP tools when they are available, or the `exacly` command otherwise (add `--json` for data). Both read `spec/exacly` and give the same answers.

If neither is available, install the command with `uv tool install "exacly-validate @ git+https://github.com/whiteredhouse/exacly"`; in the Exacly repository itself, `uv run exacly` works.

| Question | MCP tool | Command |
| --- | --- | --- |
| What is this record, what relates to it, what is its state? | `exacly_show` | `exacly show REQ-001` |
| Why does it exist, and what realizes it? | `exacly_trace` | `exacly trace APP-001` |
| What does a change affect, and what to test again? | `exacly_impact` | `exacly impact APP-001` or `exacly impact --base main` |
| What is missing or failing? | `exacly_gaps` | `exacly gaps` |

## Before changing anything

1. Find the records the task touches. Search config for the code path (`implementation_ref`, `operational_ref`), the test path (`observe.tests`), or the feature's name, and `exacly show` each one.
2. `exacly trace` each one to see which Requirements and Outcomes depend on it. Treat a Requirement's statement and acceptance criteria as the contract: if the task contradicts them, say so to the person before writing code.
3. Check `exacly gaps` so you know which findings already existed.

## While changing

- When behaviour changes, change the Requirement's statement or acceptance criteria in the same change, and the tests its `observe.tests` binding selects.
- New behaviour gets a Requirement that `supports` a Process, Capability or Outcome, and an implementer that `implements` it. New code that runs on its own gets an Application with an `implementation_ref`. A new data concept gets an Information record, read or written by an Application and stored by Infrastructure.
- Give each new Requirement a `tests` binding whose `cases` patterns select the tests that verify it, so refresh can verify it.
- Keep IDs stable. Retire a record (`lifecycle: retired`) or `supersede` it instead of deleting or reusing it.
- Update `last_reviewed_at` on records whose meaning you changed.
- Never edit state: CI writes it from observations. If an observation is wrong, record an exception or a decision in config.

## After changing

Copy this checklist into your response and tick it off:

```
- [ ] exacly validate spec/exacly conforms
- [ ] exacly impact --base main reviewed
- [ ] Tests of every Requirement under "Check again" pass
- [ ] Pull request names the changed records and affected Outcomes
```

1. `exacly validate spec/exacly` must conform. Fix errors and run it again until it does; read warnings.
2. `exacly impact --base main` lists what the branch changed and what that affects. Run the tests of every Requirement under "Check again", and mention the changed records and affected Outcomes in the pull request.
3. `mise run check` (or the repository's own check) refreshes state locally into `reports/` without committing it, so you can see verification for your change before CI does.
