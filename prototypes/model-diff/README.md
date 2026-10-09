# model-diff prototype

A first, structure-only cut of Exacly's return path: from one git commit to a component-level model diff. It was written to build the model views for the [model-diff pilot](../../docs/model-diff-pilot-method.md).

## What it does

For a commit and its parent, read straight from git with no checkout:

1. Finds the changed Python modules under the package.
2. Parses each with `ast` and records:
   - its component, from a domain map;
   - imports of other package modules, added and removed;
   - public signatures (functions, classes, methods, class fields) added, removed or changed.
3. Lifts module imports to component edges and reports them as added, removed or unchanged.
4. Optionally prints a Mermaid component diagram. It shows the changed components plus any reached by a changed edge; added edges are bold.

Output is JSON by default.

## Usage

```sh
pip install pyyaml
python model_diff.py /path/to/repo <commit> \
  --source-root backend/src --package mypkg \
  --domains architecture/domains.yaml [--domains-rev <rev>] [--mermaid]
```

The domain map is YAML with a `rules` list of `{match: <glob>, domain: <name>}`. Globs are matched against module paths relative to the package root, and the first match wins. A module no rule matches becomes its own component, `unmapped:<path>`. Use `--domains-rev` when the commit predates the map.

## Limits

- Python only, and only absolute imports of the package itself.
- Imports re-exported through a package `__init__` show as edges to the `__init__`, not to the real module.
- Calls made by name (for example Temporal activities started as `"activity_name"`) have no import, so they are invisible here. The pilot used test traces for behaviour; that tracer is not part of this prototype yet.
- Importers are found by text search (`git grep`), so they can miss unusual import forms.
