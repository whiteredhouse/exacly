# Exacly Specification

| Version | Status | Date |
| --- | --- | --- |
| [0.1.0](versions/0.1.0.md) | Draft | 2026-10-18 |

## Schema

[`schema/0.1/2026-10-23.json`](schema/0.1/2026-10-23.json) is a JSON Schema for a product map that follows any 0.1.x version, in YAML or JSON. It checks one document's shape:
- the model boundary;
- the common record fields, plus the type-specific fields each active record needs;
- evidence, relationships, exceptions and assessments.

Rules that need the whole graph are left to a validator, as section 11 describes: unresolved references, legal relationship endpoints, duplicate edges, cycles and the section 7 traceability rules. Fields starting with `x-` are allowed anywhere.

[`examples/0.1/invoice-reminders.yaml`](examples/0.1/invoice-reminders.yaml) is the section 12 worked example as a product map that validates against the schema.

## Versioning

Versions follow the [OpenAPI](https://spec.openapis.org/oas/) pattern.

- **Spec versions** are major.minor.patch, and each one has its own file in `versions/`.
  - Patch releases only clarify wording.
  - Minor releases add optional record types, fields or relationships.
  - Major releases may rename, remove or change meaning.
- **0.x is unstable.** Anything may change until 1.0. 1.0 comes with the first outside adopter, and compatibility is promised from then on.
- **Released versions are never edited.** A version may change while its status is Draft. Once it is released, a change becomes a new version file. Adopting projects pin a version (section 13), so an edited file would change the rules under them.
- **Schemas, once published, are dated iterations within a minor version.** For example, `schema/0.1/2026-10-23.json` covers every 0.1.x release. The latest date is the correct one, and a new date fixes the schema without changing the spec.
