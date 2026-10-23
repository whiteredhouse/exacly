# Exacly validator

Validates a product map against the [Exacly Specification](../spec/README.md) and prints the validation report that section 11 describes.

```sh
uv tool install "exacly-validate @ git+https://github.com/whiteredhouse/exacly"
exacly-validate docs/exacly/product-map.yaml
exacly-validate docs/exacly/product-map.yaml --json --date 2026-10-23
```

`pip install` works the same way. The package carries the spec's schemas, so it runs anywhere. In Python, `exacly_validate.validate(doc, date)` returns the report without changing `doc`.

It exits 0 when the map conforms, with or without exceptions, and 1 when it does not.

## What it checks

1. **Schema.** The document must match the JSON Schema for its spec version, which is the latest dated iteration in `spec/schema/<major.minor>/`. If it doesn't, the report lists the schema errors and skips the other checks.
2. **Section 11 errors.**
   - Duplicate IDs (`11.duplicate-id`).
   - Unresolved references in relationships, exceptions and assessments (`11.unresolved`). A reference to another model is `namespace:ID`; a reference to this model may be qualified with its own namespace.
   - Relationship endpoints section 6 does not allow, and `contains` or `supersedes` between different types (`11.endpoint`).
   - Duplicate edges (`11.duplicate-edge`).
   - Containment or replacement cycles (`11.cycle`).
3. **Section 7 traceability.** Rules 7.1 to 7.8, plus active relationships to retired records (`7.retired-target`).
   - A gap in an active record is an error. A gap in a proposed record is a warning, because proposed records may be incomplete.
   - Rule 7.4's business path runs through `supports`, `uses_capability` and `contributes_to`.
   - Rule 7.8 needs an implementer for a released or verified Requirement. Each released delivery assessment needs implementation or deployment evidence, and each passed verification needs test or decision evidence. The evidence must apply to that assessment's own context, because assessments do not carry forward to another release or environment. A verified Requirement also needs at least one supported release.
4. **Section 11 warnings.**
   - Active records never reviewed or reviewed longer ago than the declared interval (`11.review-overdue`).
   - Unknown baselines (`11.unknown-baseline`).
   - `depends_on` cycles, including a record that depends on itself (`11.dependency-cycle`).
   - Exceptions that are expired, expiring within 30 days, or match no finding.

An exception names the rule code from the report, such as `7.1` or `7.retired-target`, and the record, with or without the model's own namespace. A valid exception waives that error, and the report then says "conforms with exceptions". An expired exception waives nothing.

## Not checked

- Mixing current and proposed state inside one document.
- Whether a `depends_on` that is optional or asynchronous describes its conditions.
- Whether external locators can be reached.
- Whether diagrams are older than the records they describe.

These need information the map does not carry, or a person's judgement.

## Tests

`tests/cases.yaml` holds the cases as data. Each is a JSON Patch to the spec's worked example, with the exact findings and result the report must give.

## Development

The repository uses [mise](https://mise.jdx.dev/) for its tools (Python and uv) and tasks. `mise run check` is what CI runs.

| Task | What it does |
|---|---|
| `mise run sync` | Installs the validator and development tools into `.venv` from `uv.lock` |
| `mise run test` | Runs the schema and validator test cases |
| `mise run lint` | Checks style and formatting with Ruff |
| `mise run typecheck` | Type-checks with mypy in strict mode |
| `mise run examples` | Validates every example in `spec/examples/` |
| `mise run check` | All of the above |

To try a map against your working copy, run `uv run exacly-validate path/to/map.yaml`.
