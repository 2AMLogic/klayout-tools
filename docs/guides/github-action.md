# `klt verify` — the reusable GitHub Action

`action.yml` at the repo root wraps `klt` for downstream block repos — a
composite GitHub Action that installs `klt`, runs a caller-configured list
of verbs against a caller-provided layout, and publishes the results the
way a human reviewer wants them: JSON artifacts, `klt render` PNG
artifacts, and a rendered [`klt report`](../cli/report.md) appended to
`$GITHUB_STEP_SUMMARY`. It is the CI distribution channel for `klt`:
any sky130/gf180mcu block repo can add a few lines of workflow YAML and get
DRC/metrics on every push, without hand-rolling an install + JSON-to-markdown
step (see issue #250's Tiny Tapeout `tt-gds-action` survey for the prior
art this mirrors).

## Why one `action.yml`, not a separate repo

The action lives at this repo's root (`action.yml`) rather than in a
separate `klayout-tools/klt-action` repository. Pinning a downstream
workflow to a tag of *this* repo (`uses: 2AMLogic/klayout-tools@v0.1.0`)
version-locks the action to the exact `klt` build it drives — there is no
second repository to keep in sync on every release, and the action's own
"install klt" step defaults to building from the exact source checkout the
action itself is pinned to (see "How klt gets installed" below). A separate
repo would only pay off if the action needed a release cadence independent
of the CLI's — it doesn't; they are the same artifact release-for-release.

## What it does

1. **Installs `klt`.** By default, from this action's own pinned source
   checkout (`github.action_path`) — so the action always runs the exact
   `klt` build it is version-locked to. Set `klt-version` to install a
   specific released version from PyPI instead.
2. **Optionally fetches a pinned PDK** (`fetch-pdk: true`) via this action's
   own `scripts/fetch-pdks.sh`, reusing that script's pinned-version +
   checksum-verified fetch as-is (no reimplementation) — see
   [`../../pdks/README.md`](../../pdks/README.md). Off by default: neither
   `drc` nor `precheck`'s built-in rule decks need it, and it's a
   multi-hundred-MB download (the action caches it via `actions/cache`
   keyed on the pinned lambdapdk version, so repeat runs are fast).
3. **Runs each requested verb** (`verbs`, comma-separated) against `layout`
   — `klt drc <layout> --deck <deck> --format json`, etc. — exactly the
   invocation a local user would run. This action never reimplements verb
   logic; it only orchestrates the real CLI, so local and CI results can't
   diverge (the same design principle as Tiny Tapeout's `tt-gds-action`).
4. **Renders a step summary.** The JSON outputs from any `drc`/`lvs`/
   `layout-metrics`-shaped verb are piped through
   `klt report --format github-summary` (issue #267) and appended to
   `$GITHUB_STEP_SUMMARY`, so every PR gets a reviewable violations/metrics
   table with zero local tooling.
5. **Uploads artifacts.** Every verb's raw JSON output (plus the rendered
   summary markdown) under one artifact (`json-artifact-name`, default
   `klt-json-outputs`); any `klt render` PNGs under another
   (`render-artifact-name`, default `klt-render`).
6. **Fails clearly on findings.** The action's `status` output (and, unless
   `fail-on-findings: false`, the step's own exit code) reflects whether
   every requested verb ran clean — a DRC violation or a hard error is
   surfaced as a red step, not swallowed into a green one.

## Inputs

| Input | Default | Description |
| ----- | ------- | ----------- |
| `layout` | *(required)* | Path to the GDSII/OASIS layout file to check. |
| `block` | dirname of `layout` | Block directory the `layout-metrics` verb operates on. |
| `verbs` | `drc,layout-metrics` | Comma-separated `klt` verbs to run: `drc`, `precheck`, `layers`, `cells`, `stats`, `render`, `layout-metrics`. |
| `deck` | `sky130` | Rule deck passed to `drc`/`precheck`/`layout-metrics` (`sky130` or `gf180mcu`). |
| `fetch-pdk` | `false` | Fetch the pinned lambdapdk data set before running verbs. |
| `klt-version` | *(empty)* | Install a released PyPI version instead of this action's pinned source. |
| `python-version` | `3.12` | Python version to set up. |
| `output-dir` | `klt-action-output` | Where JSON outputs / renders are written, relative to the caller's workspace. |
| `json-artifact-name` | `klt-json-outputs` | Uploaded-artifact name for JSON outputs + the rendered summary. |
| `render-artifact-name` | `klt-render` | Uploaded-artifact name for `klt render` PNGs. |
| `fail-on-findings` | `true` | Fail the step when any verb reports findings or an error. |

## Outputs

| Output | Description |
| ------ | ----------- |
| `status` | `"success"` if every requested verb ran clean, `"failure"` otherwise (regardless of `fail-on-findings`). |
| `summary-markdown-path` | Path to the rendered `klt report --format github-summary` markdown file. |
| `lambdapdk-dir` | Absolute path to the fetched lambdapdk tree when `fetch-pdk: true` (empty otherwise). |

## Worked example — a downstream block repo

```yaml
# .github/workflows/klt.yml
name: klt checks
on: [push, pull_request]

jobs:
  klt:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: 2AMLogic/klayout-tools@v0.1.0
        with:
          layout: layout/my_block.gds
          verbs: drc,layout-metrics,render
          deck: sky130
```

That's the whole workflow. Every push and PR now gets:

- A step summary with a DRC violation table (or "No violations found.") and
  a key-metrics table from `layout-metrics`.
- A `klt-json-outputs` artifact with the raw JSON for each verb (useful for
  a downstream tool, or for diffing metrics across commits).
- A `klt-render` artifact with per-layer PNGs from `klt render`.
- A red step (and therefore a red job/PR check) whenever a DRC violation is
  found or a verb errors — gate branch protection on this job the same way
  you would gate on a local `klt drc` invocation failing.

## Dogfooding

This repo's own `.github/workflows/ci.yml` runs an `action-smoke-test` job
that exercises `action.yml` (via `uses: ./`) against two fixtures on every
push and PR: a DRC-clean corpus cell (`tests/corpus/sky130/`, expecting
`status: success`) and a layout with seeded violations
(`examples/drc/example.gds`, expecting `status: failure` with the violation
surfaced in the step summary) — so a regression in the action breaks this
repo's own CI before it reaches a downstream consumer.
