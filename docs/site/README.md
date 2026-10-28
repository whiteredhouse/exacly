# Exacly landing page

This directory contains the landing-page presentation. The build uses the
repository's locked Python dependencies to generate model browsers and render
recorded evidence. The published result is static HTML, CSS and JavaScript;
it needs no application server or API key.

Keep links relative so the site works at both a repository subpath and a custom
domain. The main presentation files are `index.html`, `style.css` and `site.js`.

## Preview

From the repository root, with uv available:

```sh
site_preview_dir="$(mktemp -d)"
uv run --locked python ci/build_site.py --out "$site_preview_dir"
python3 -m http.server 8000 --directory "$site_preview_dir"
```

Open `http://localhost:8000`. Preview the built directory rather than serving
`docs/site` directly: both model browsers, model downloads and project-evidence
cards are generated during the build.

The visitor quick start downloads `project-model.tar.gz`, extracts it, and opens
`REQ-013` with `uvx exacly show REQ-013 -m project-model`. It uses the PyPI package
without a repository clone or persistent Exacly installation. The invoice browser
and its original YAML downloads remain a smaller fictional learning example.
Keep the download URLs aligned with the Pages address. The contributor workflow
remains `mise run check` from the repository root.

## Source models and recorded evidence

`ci/build_site.py` builds from these authoritative inputs:

- `docs/site/` supplies the landing-page presentation and assets.
- `spec/examples/0.2/invoice-reminders/` supplies the fictional invoice model.
  Its original YAML is copied into the published `example/` directory, beside
  the generated invoice-model browser. These records are proposed and have no
  observed state.
- `spec/exacly/` supplies Exacly's real product model and committed observations.
  Its browser is generated into the published `project/` directory. The landing
  page's project-evidence cards are derived from the same records and state.
  The generated `project-model.tar.gz` contains its canonical YAML config and
  JSON state documents under `project-model/`, including `state/main.json`,
  preserving the source bytes. This archive is a build artifact, not another
  maintained source snapshot.

Do not maintain duplicate YAML or generated-browser snapshots under `docs/site`.
Rebuild to pick up source changes. The builder replaces the
`<!-- PROJECT_EVIDENCE -->` marker in the staged landing page; keep that marker
in the presentation source.

The build reads recorded state without refreshing or modifying it. Each
assessment keeps its own evidence, observation time and implementation identity;
an absent delivery observation remains absent. Verification, delivery, operation
and outcome evaluation are separate claims. Recorded observations identify the
revision they concern and do not establish verification of a newer commit.

Keep assertions about available commands aligned with `python/README.md` and
the package release status. Target-aware verification planning is advisory;
task dispatch and automated apply remain proposed.

## Publish through GitHub Pages

Select **Settings → Pages → GitHub Actions** as the repository's publishing
source. [The Landing page workflow](../../.github/workflows/pages.yml) then
builds, checks local HTML references and publishes the validated artifact to
the `github-pages` environment. The deployment summary provides the URL.

Pull requests validate the build without publishing. Automatic publication
follows a successful **Spec** workflow run from a push to this repository's
`main` branch. Spec commits fresh observations with `[skip ci]`, so the Pages
workflow listens for Spec completion and checks out `main` after that commit
instead of relying on another push event. Failed runs, pull-request runs and
runs from another repository cannot publish.

Production builds and deployment are serialized.

**Run workflow** also permits manual publication from `main`. The build job has
read-only repository permissions; Pages deployment permissions are confined to
the deployment job. The `.nojekyll` marker is preserved in the static artifact.
