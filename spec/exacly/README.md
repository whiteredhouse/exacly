# Exacly's own model

Exacly models itself, following the layout in section 13 of the specification. It is the first model whose state will come from real observations.

| | |
| --- | --- |
| Namespace | `exacly` |
| Owner | `exacly-maintainers` |
| Specification | [0.2.0](exacly-0.2.0.md), an unmodified copy of [`../versions/0.2.0.md`](../versions/0.2.0.md) |
| Boundary | [`model.yaml`](model.yaml) |
| Extensions | None |

## Config

- [`validation.yaml`](validation.yaml) covers checking a model. The validator, CI and the repository exist today, so these records are active.
- [`observation.yaml`](observation.yaml) covers observing a model's reality: the `exacly refresh` command and the measure it feeds. These are not built yet, so the records are proposed.

## State

`state/` will hold one state document per context, written only by `exacly refresh` and committed only from main. It is empty until refresh exists.

## Bindings

A `tests` binding tells refresh which results verify a record:

```yaml
observe:
  tests:
    workflow: .github/workflows/spec.yml   # the workflow that runs the tests
    job: check                             # its job
    cases:                                 # optional: glob patterns over case IDs
      - "validator/tests/*/cases.yaml::7.*"
```

A case ID is `<cases file>::<description>` for a data-driven case, and `<test file>::<test name>` for any other test. Patterns are case-sensitive. Without `cases`, the job's own result is the evidence. This contract is a draft that `exacly refresh` will define.

## Validating

```sh
mise run model
```

This runs `exacly-validate spec/exacly --revision <HEAD>`, and CI runs it on every change as part of `mise run check`.
