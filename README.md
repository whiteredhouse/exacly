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
| **Validate** | Check config against the schema, the relationship rules and the traceability rules. | [`exacly-validate`](validator/README.md), 0.1 and 0.2 |
| **Refresh** | Observe the real system and write state: assessments per environment and the evidence behind them. | [`exacly-refresh`](validator/README.md#refresh), from test results |
| **Ask** | Show a record, trace why it exists and what realizes it, list what a change affects and what the model is missing. | [`exacly`](validator/README.md#questions) show, trace, impact and gaps; [views](validator/README.md#views) as a pull request plan and a site; an [MCP server and skill](validator/README.md#for-coding-agents) for agents |
| **Plan** | Compare config with state and list the work needed to make them agree. | Planned |
| **Apply** | Turn the plan into tasks for people or agents. The next refresh closes them. | Planned |

## Where it comes from

Exacly started from a gap in AI-assisted development. Agents now write code faster than anyone can keep a description of the product true to it. Spec-driven tools treat the spec as a prompt that goes stale once the code is written, and architecture tools draw what exists without saying what was intended or proven. [Our research](docs/uml-spec-driven-development-research.md) found tools for each step of the loop from model to code and back, but none that checks code against the intended model, so that return path is where Exacly starts.

Its ideas come from established practice:

- **[ArchiMate](https://archimate-community.pages.opengroup.org/workgroups/archimate-101/)** informs the record types and relationships, from outcomes and capabilities down to infrastructure. Exacly keeps its own vocabulary and does not claim ArchiMate conformance.
- **[UML](https://www.omg.org/uml/what-is-uml.htm)** fits the detailed views of structure and behaviour, such as interactions, workflows and state transitions.
- **Terraform** gives the loop: authored config, recorded state, and plan and apply to bring them together.
- **Kubernetes** gives the split between what was asked for and what was observed, as spec and status do, with status written only by tooling.
- **Supply chain attestations** such as in-toto and OpenVEX shape evidence: state cites a test run or deployment record rather than copying it, and a negative assessment must give its reason.
- **[OpenAPI](https://spec.openapis.org/oas/)** gives the versioning: semantic spec versions with dated schema iterations.

## Repository layout

| Path | Contents |
| --- | --- |
| [`spec/`](spec/README.md) | Specification versions, JSON Schemas, worked examples and data-driven test cases |
| [`validator/`](validator/README.md) | `exacly-validate`, the Python validator |
| [`skills/`](skills/exacly/SKILL.md) | The skill that tells coding agents how to work with a model |
| [`action.yml`](action.yml) | The GitHub Action that checks a model and comments the plan in other repositories |
| [`docs/`](docs/) | Research and pilot notes |

## Quick start

Validate the [invoice reminders example](spec/examples/0.2/invoice-reminders/), a small slice of a product written as 0.2 config:

```sh
uv tool install "exacly-validate @ git+https://github.com/whiteredhouse/exacly"
exacly-validate spec/examples/0.2/invoice-reminders
```

In your own project, keep config in `spec/exacly/` and pass the commit it was validated at with `--revision "$(git rev-parse HEAD)"`.

## In CI

Other repositories run Exacly with the [action](action.yml) at this repository's root. `check` validates the model and fails the job when it doesn't conform. `plan` comments the change plan on each pull request, replacing its earlier comment. Both write their report to the job summary. The action installs Exacly from its own checkout with a pinned uv version and `uv.lock`; it does not require Exacly to be published on PyPI. uv and Python dependencies are downloaded as needed.

Replace `<RELEASE_COMMIT_SHA>` in this workflow template with the full commit SHA of an Exacly release, or a commit on `main` that contains the action. Version tags will be published with the first release.

```yaml
# .github/workflows/exacly.yml
name: Exacly
on: pull_request
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: whiteredhouse/exacly@<RELEASE_COMMIT_SHA>
        with:
          model: spec/exacly
  plan:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    concurrency:
      group: exacly-plan-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: whiteredhouse/exacly@<RELEASE_COMMIT_SHA>
        with:
          mode: plan
          model: spec/exacly
```

`model` takes files, folders or patterns such as `spec/exacly/*.yaml`, one per line or separated by spaces. `plan` compares with the pull request's base branch, or with `base` when given. A pull request from a fork gets a read-only token, so its plan stays in the job summary. CI exercises `check` on GitHub-hosted Linux and macOS runners, and `plan` on Linux. It sets up uv itself; self-hosted runners also need Git, Bash and, for commenting, the GitHub CLI (`gh`).

In private repositories, `plan` needs Git authentication to fetch the base branch: keep checkout's default `persist-credentials: true`, or configure Git authentication separately. The action's `token` input authenticates comment API calls only.

## Development

The repository uses [mise](https://mise.jdx.dev/) for Python, uv and tasks. Run what CI runs (tests, Ruff, mypy and every example) with:

```sh
mise run check
```

## Status

Exacly is at 0.x and unstable: anything may change until 1.0, which comes with the first outside adopter. See [Versioning](spec/README.md#versioning).

Each part of Exacly has its own version, tagged as `<part>-vX.Y.Z`, as release-please and Changesets tag the parts of one repository:

- **The specification**, `spec-vX.Y.Z`: the file in [`spec/versions/`](spec/versions/) and the `exacly:` line of every model document.
- **The validator**, `validator-vX.Y.Z`: the `exacly-validate` package with its `exacly-validate`, `exacly-refresh` and `exacly` commands and the MCP server.
- **The GitHub Action** lives in [whiteredhouse/exacly-action](https://github.com/whiteredhouse/exacly-action), tagged `vX.Y.Z` with a moving `v0`, as workflows expect. Each action release runs one validator release.

| Validator | Spec versions it reads |
| --- | --- |
| 0.1.0 | 0.1, 0.2 |
