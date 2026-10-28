# Invoice demonstration inputs

`sample-junit.xml` is an explicitly fictional three-case passing report, not the
output of tests run in this repository. `DEMO.md` accompanies the published
browser and downloadable model so the illustration cannot be mistaken for live
project evidence.

The build calls `ci.invoice_demo.prepare_demo` to derive a staged model from the
existing canonical invoice example. Only the demo environment and the reminder
requirement's lifecycle/review date change. State comes from the real refresh
observer with the fixture as input; no delivery assessment is created.

To reproduce a staged demo from the repository root:

```sh
demo_dir="$(mktemp -d)"
uv run --locked python -m ci.invoice_demo --out "$demo_dir"
uvx exacly show REQ-001 -m "$demo_dir"
```

The timestamp and revision are stable illustrative identities. A fresh state's
lineage is assigned by refresh. Canonical YAML and committed project state are
never modified.
