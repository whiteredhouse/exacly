# Exacly landing page

This directory is the static landing-page mockup and the content published by
[the GitHub Pages workflow](../../.github/workflows/pages.yml). It needs no build
step, package installation, API key, or server. Keep links relative so it works
at both a repository subpath and a custom domain.

## Preview

From the repository root:

```sh
python3 -m http.server 8000 --directory docs/site
```

Open `http://localhost:8000`. The main presentation files are `index.html`,
`style.css`, and `site.js`. All browser interactions run locally.

The visitor quick start uses `uvx exacly --help` to run the published CLI
without a persistent installation or a repository clone. The worked model is
available in the browser example. The contributor workflow remains
`mise run check` from the repository root.

## Maintain the worked example

The `example/` directory is authentic static output from the fictional invoice
reminders model. Its records are proposed; it does not represent a deployed
customer product or a passing test run. Regenerate it when that model changes:

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
3. The **Landing page** workflow checks local HTML references and publishes
   `docs/site` to the `github-pages` environment. Its deployment summary provides
   the resulting URL.

Pull requests run the reference check without deployment. Changes under
`docs/site/` on `main` deploy automatically; **Run workflow** also allows a
manual deployment from `main`. The `.nojekyll` marker preserves static delivery.

The mockup has been prepared for deployment; creating these files alone does
not enable Pages or publish the repository.
