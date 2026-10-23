# Exacly validator

Validates a product map against the [Exacly Specification](../spec/README.md) and prints the validation report that section 11 describes.

```sh
pip install -r validator/requirements.txt
python validator/exacly_validate.py docs/exacly/product-map.yaml
python validator/exacly_validate.py docs/exacly/product-map.yaml --json --date 2026-10-23
```

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
   - Rule 7.8 needs an implementer and implementation or deployment evidence for a released Requirement. A passed verification also needs test or decision evidence.
4. **Section 11 warnings.**
   - Active records never reviewed or reviewed longer ago than the declared interval (`11.review-overdue`).
   - Unknown baselines (`11.unknown-baseline`).
   - `depends_on` cycles (`11.dependency-cycle`).
   - Exceptions that are expired, expiring within 30 days, or match no finding.

An exception names the rule code from the report, such as `7.1` or `7.retired-target`, and the record. A valid exception waives that error, and the report then says "conforms with exceptions". An expired exception waives nothing.

## Not checked

- Mixing current and proposed state inside one document.
- Whether a `depends_on` that is optional or asynchronous describes its conditions.
- Whether external locators can be reached.
- Whether diagrams are older than the records they describe.

These need information the map does not carry, or a person's judgement.

## Tests

`tests/cases.yaml` holds the cases as data. Each is a JSON Patch to the spec's worked example, with the exact findings and result the report must give.

```sh
python -m unittest discover -s validator/tests
```
