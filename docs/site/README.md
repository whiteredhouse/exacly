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

The visitor quick start downloads `example.yaml` and runs a requirement trace
with the published package through `uvx`. It needs no repository clone or
persistent Exacly installation. Keep the download URL aligned with the Pages
address. The contributor workflow remains `mise run check` from the repository
root.

## Maintain the worked example

The `example/` directory is authentic static output from the fictional invoice
reminders model. The downloadable `example.yaml` merges its boundary and records
into one config document. Its records are proposed; it does not represent a deployed
customer product or a passing test run. Regenerate it when that model changes:

```sh
uv run --locked exacly validate spec/examples/0.2/invoice-reminders
uv run --locked exacly view site \
  -m spec/examples/0.2/invoice-reminders \
  --out docs/site/example
uv run --locked python - <<'PYTHON'
from pathlib import Path
import yaml
source = Path("spec/examples/0.2/invoice-reminders")
model = {**yaml.safe_load((source / "model.yaml").read_text()),
         **yaml.safe_load((source / "billing.yaml").read_text())}
header = "# Fictional invoice-reminders example from spec/examples/0.2/invoice-reminders/.\n# All records are proposed; this config has no observed state.\n"
Path("docs/site/example.yaml").write_text(
    header + yaml.safe_dump(model, sort_keys=False, allow_unicode=True)
)
PYTHON
uv run --locked exacly validate docs/site/example.yaml
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
