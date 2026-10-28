# Exacly landing page

This directory contains the static landing-page mockup and generated model
browser. [The GitHub Pages workflow](../../.github/workflows/pages.yml) stages
these files together with the original invoice-reminder YAML files from
`spec/examples/0.2/invoice-reminders/`. It needs no package installation, API key,
or application server. Keep links relative so it works at both a repository
subpath and a custom domain.

## Preview

From the repository root:

```sh
site_preview_dir="$(mktemp -d)"
cp -R docs/site/. "$site_preview_dir/"
cp spec/examples/0.2/invoice-reminders/*.yaml "$site_preview_dir/example/"
python3 -m http.server 8000 --directory "$site_preview_dir"
```

Open `http://localhost:8000`. The main presentation files are `index.html`,
`style.css`, and `site.js`. All browser interactions run locally.

The visitor quick start downloads the original `model.yaml` and `billing.yaml`
files from the published `example/` directory and runs a requirement trace with
the PyPI package through `uvx`. It needs no repository clone or persistent Exacly
installation. The source YAML files are copied into the published artifact; do
not maintain another copy under `docs/site/`. Source-example changes also trigger
the Pages workflow. Keep the download URL aligned with the Pages address. The
contributor workflow remains `mise run check` from the repository root.

## Maintain the worked example

The `example/` directory is authentic static output from the fictional invoice
reminders model. Its records are proposed; it does not represent a deployed
customer product or a passing test run. Regenerate the browser when that model
changes:

```sh
uv run --locked exacly validate spec/examples/0.2/invoice-reminders
uv run --locked exacly view site \
  -m spec/examples/0.2/invoice-reminders \
  --out docs/site/example

```

Commit the generated HTML with the landing-page changes. Keep assertions about
available commands aligned with `python/README.md`; target-aware verification
planning is available on main ahead of the next package release. Task dispatch
and automated apply remain proposed. A requirement's verification, delivery,
operation, and outcome evaluation are separate claims.

## Publish through GitHub Pages

1. In the repository's **Settings → Pages**, select **GitHub Actions** as the
   source.
2. Merge the static files and `.github/workflows/pages.yml` into `main`.
3. The **Landing page** workflow stages the site with the source YAML example,
   checks its local HTML references, and publishes that validated artifact to
   the `github-pages` environment. Its deployment summary provides
   the resulting URL.

Pull requests run the reference check without deployment. Changes under
`docs/site/` or the source example on `main` deploy automatically; **Run workflow** also allows a
manual deployment from `main`. The `.nojekyll` marker preserves static delivery.

The mockup has been prepared for deployment; creating these files alone does
not enable Pages or publish the repository.
