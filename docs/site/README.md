# Exacly landing page

The build generates static HTML, CSS and JavaScript using the repository's locked
Python dependencies. The published site needs no application server or API key.
Keep presentation links relative for GitHub project Pages.

## Preview

From the repository root, with uv available:

```sh
site_preview_dir="$(mktemp -d)"
uv run --locked python -m ci.build_site --out "$site_preview_dir"
python3 -m http.server 8000 --directory "$site_preview_dir"
```

Open `http://localhost:8000`. Serve the built directory: model browsers,
downloads and the demonstration-state panel are generated during the build.

## Invoice demonstration

The main example is derived from the existing
`spec/examples/0.2/invoice-reminders/` model. `ci/invoice_demo.py` stages a copy,
sets its environment to `demo`, and makes `REQ-001` active with an illustrative
review date. Canonical source documents remain unchanged, including their
proposed lifecycle values.

Exacly's refresh observer reads the explicitly fictional report in
`ci/fixtures/invoice-reminders/sample-junit.xml` and generates `state/demo.json`.
The sample verification passes; delivery has no observation. Demo labels appear
on the landing page, every invoice-browser page, the staged YAML and the bundled
`DEMO.md`. The sample report does not establish real product verification or
complete acceptance coverage.

`ci/build_site.py` publishes the prepared model, report and state beside the
generated browser in `example/`, and packages the same files as
`invoice-model.tar.gz` under `invoice-model/`. The visitor runs two commands:

```sh
curl -fsSL https://whiteredhouse.github.io/exacly/invoice-model.tar.gz | tar -xz
uvx exacly show REQ-001 -m invoice-model
```

Keep the `<!-- DEMO_EVIDENCE -->` marker in `index.html`: the builder replaces it
with the requirement's demonstration state. Do not maintain duplicate config or
generated-browser snapshots in this directory.

## Real project state

The build also generates `project/` from `spec/exacly/` and its committed state.
The repository README links to that full project view; the landing page stays
focused on the invoice demonstration. Real state is read without refreshing or
modifying it. Each assessment retains its own context, date and revision, so an
observation does not establish verification of newer commits.

## GitHub Pages

Select **Settings → Pages → GitHub Actions**. The Landing page workflow builds
the site and checks local references. Pull requests validate without publishing.
Automatic publication follows a successful **Spec** push run on this repository's
`main` branch, checking out main after Spec's `[skip ci]` state commit. Failed,
pull-request and other-repository runs cannot publish. Production builds and
deployment are serialized.

**Run workflow** permits manual publication from `main`. The build has read-only
repository permissions; Pages deployment permissions are confined to its deploy
job. Run `mise run check` before pushing.
