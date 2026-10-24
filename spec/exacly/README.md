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
- [`observation.yaml`](observation.yaml) covers observing a model's reality. The `exacly-refresh` command exists, so its requirement and application are active. Running it on main, and the measure it feeds, are still proposed.

## State

`state/` will hold one state document per context, written only by `exacly-refresh` and committed only from main. It stays empty until CI runs refresh on main. To see the state a run would write:

```sh
uv run pytest spec/tests validator/tests --junitxml=junit.xml
uv run exacly-refresh spec/exacly --context main --state /tmp/main.json --junit junit.xml --distribution \
  --revision "$(git rev-parse HEAD)" --workflow .github/workflows/spec.yml --job check --job-status success
uv run exacly-validate spec/exacly /tmp/main.json --revision "$(git rev-parse HEAD)"
```

`--distribution` says that main is how Exacly is distributed, since people install it from the repository. So an implementer present at the revision counts as released there.

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

A case ID is `<cases file>::<description>` for a data-driven case, and `<test file>::<test name>` for any other test. Patterns are case-sensitive. Without `cases`, the job's own result is the evidence. Each test reports its case ID in the JUnit report, as the `exacly-case` property.

## Validating

```sh
mise run model
```

This runs `exacly-validate spec/exacly --revision <HEAD>`, and CI runs it on every change as part of `mise run check`.
