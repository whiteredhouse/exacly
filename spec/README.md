# Exacly Specification

| Version | Status | Date |
| --- | --- | --- |
| [0.2.0](versions/0.2.0.md) | Draft | 2026-10-23 |
| [0.1.0](versions/0.1.0.md) | Draft | 2026-10-18 |

## Schema

### 0.2

Version 0.2 splits the product map into **config**, which people and agents author, and **state**, which only tooling writes (section 9). Each has its own schema:

- [`schema/0.2/config/2026-10-25.json`](schema/0.2/config/2026-10-25.json) checks one config document: the model boundary, records with their bindings, relationships and exceptions. Config may span several documents, and exactly one declares the model boundary. It allows a list of bindings for a source, which the [2026-10-23](schema/0.2/config/2026-10-23.json) iteration did not.
- [`schema/0.2/state/2026-10-25.json`](schema/0.2/state/2026-10-25.json) checks one state document: for one context, the assessments and observed evidence per record, observed evidence per relationship, and pending work. It adds an optional `source`, `binding` and `x-` fields on assessments to the [2026-10-23](schema/0.2/state/2026-10-23.json) iteration.

[`examples/0.2/invoice-reminders/`](examples/0.2/invoice-reminders/) is the section 12 worked example as two config documents. It has no state, because every record is still proposed. Section 14 of the spec lists the changes from 0.1 and how to migrate.

The draft distinguishes structural conformance from measurement readiness and observed results. [Measurement coverage acceptance criteria](../docs/measurement-coverage-acceptance-criteria.md) describe proposed coverage tooling; they do not add schema fields or validator rules to 0.2.

### 0.1

[`schema/0.1/2026-10-23.json`](schema/0.1/2026-10-23.json) is a JSON Schema for a product map that follows any 0.1.x version, in YAML or JSON. It checks one document's shape:
- the model boundary;
- the common record fields, plus the type-specific fields each active record needs;
- evidence, relationships, exceptions and assessments.

Rules that need the whole graph are left to a validator, as section 11 describes: unresolved references, legal relationship endpoints, duplicate edges, cycles and the section 7 traceability rules. Fields starting with `x-` are allowed on the document, the model boundary, records, relationships and evidence.

[`examples/0.1/invoice-reminders.yaml`](examples/0.1/invoice-reminders.yaml) is the section 12 worked example as a product map that validates against the schema.

## Tests

`tests/` checks that each schema is valid and that every example validates. It also runs every case in [`tests/0.1/cases.yaml`](tests/0.1/cases.yaml) and [`tests/0.2/cases.yaml`](tests/0.2/cases.yaml). Each case is a JSON Patch to the worked example, with the result the schema must give and, for an invalid document, the errors it must report by location and keyword. The cases are plain data, so any validator can run them. The tests run in CI on every change under `spec/`. To run them locally with [mise](https://mise.jdx.dev/), which also runs the [`exacly` package](../python/README.md)'s tests:

```sh
mise run test
```

## Versioning

Versions follow the [OpenAPI](https://spec.openapis.org/oas/) pattern.

- **Spec versions** are major.minor.patch, and each one has its own file in `versions/`.
  - Patch releases only clarify wording.
  - Minor releases add optional record types, fields or relationships.
  - Major releases may rename, remove or change meaning.
- **0.x is unstable.** Anything may change until 1.0. 1.0 comes with the first outside adopter, and compatibility is promised from then on.
- **Released versions are never edited.** A version may change while its status is Draft. Once it is released, a change becomes a new version file. Adopting projects pin a version (section 13), so an edited file would change the rules under them.
- **Schemas, once published, are dated iterations within a minor version.** For example, `schema/0.1/2026-10-23.json` covers every 0.1.x release. From 0.2, each document kind has its own folder, such as `schema/0.2/config/` and `schema/0.2/state/`. The latest date is the correct one, and a new date fixes the schema without changing the spec.
