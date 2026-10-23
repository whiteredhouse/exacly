# Exacly

Keep a product model true to what ships, across the software development lifecycle.

Exacly describes a product from business purpose down to the systems that run it: outcomes, capabilities, processes, requirements, information, applications and infrastructure, and how they relate. Teams and agents author that description as **config**. Tooling observes the real system (builds, tests, deployments, metrics) and records what it finds as **state**, with the evidence behind it. Comparing the two shows what is planned, what has shipped, and where the model and the product have drifted apart.

The project has two parts:

- **The [Exacly Specification](spec/README.md)**, which defines the model, its traceability rules and its validation rules, with JSON Schemas for config and state.
- **Tools that follow the specification** through the lifecycle, so the model is checked and kept current as part of everyday development rather than written once and left to go stale.

## How it fits the lifecycle

Exacly borrows the plan and apply loop from infrastructure as code, and the spec/status split from Kubernetes.

| Step | What it does | Status |
| --- | --- | --- |
| **Describe** | Author config in YAML: records, relationships, exceptions and how each record can be observed. Start from a description (greenfield) or from an existing repo and documents (brownfield). | Spec 0.2 |
| **Validate** | Check config against the schema, the relationship rules and the traceability rules. | [`exacly-validate`](validator/README.md), 0.1 maps today, 0.2 next |
| **Refresh** | Observe the real system and write state: assessments per environment and the evidence behind them. | Planned |
| **Plan** | Compare config with state and list the work needed to make them agree. | Planned |
| **Apply** | Turn the plan into tasks for people or agents. The next refresh closes them. | Planned |

## Repository layout

| Path | Contents |
| --- | --- |
| [`spec/`](spec/README.md) | Specification versions, JSON Schemas, worked examples and data-driven test cases |
| [`validator/`](validator/README.md) | `exacly-validate`, the Python validator |
| [`docs/`](docs/) | Research and pilot notes |

## Quick start

Validate a product map:

```sh
uv tool install "exacly-validate @ git+https://github.com/whiteredhouse/exacly"
exacly-validate spec/examples/0.1/invoice-reminders.yaml
```

The [invoice reminders example](spec/examples/0.2/invoice-reminders/) shows a small slice of a product written as 0.2 config.

## Development

The repository uses [mise](https://mise.jdx.dev/) for Python, uv and tasks. Run what CI runs (tests, Ruff, mypy and every example) with:

```sh
mise run check
```

## Status

Exacly is at 0.x and unstable: anything may change until 1.0, which comes with the first outside adopter. See [Versioning](spec/README.md#versioning).
