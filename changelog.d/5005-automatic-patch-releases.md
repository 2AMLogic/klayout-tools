- **Releases are cut automatically on Loom's cadence model** (issue #5005, no
  deck hash changes): `release.yml` replaces the weekly release-PR flow with
  a cadence-gated patch bump (`scripts/version-bump-gate.sh`, at most one per
  `RELEASE_MIN_INTERVAL`, default 24 h) POSTed to the 2AM fleet publisher —
  which commits exactly `pyproject.toml`'s `base-version` and a heading-only
  `CHANGELOG.md` entry, never a direct push — and a `tag` job that tags
  `vX.Y.Z` only on a green `CI` run of the commit carrying it, then dispatches
  `publish.yml`. The published wheel and sdist now carry a deck-history table
  generated from the real tag outside the tree
  (`generate_deck_history.py --output` + `hatch_build.py`'s
  `KLT_DECK_HISTORY_PATH` force-include; `--as-release` is gone), the checked-in
  `_history.json` may lag `git tag` until a normal PR refreshes it, and
  `site/src/data/cli.generated.json` is generated at site-deploy time instead
  of being committed. GitHub Release notes include the `changelog.d/` fragments
  added since the previous tag (`release_notes.py`), which are no longer folded
  into `CHANGELOG.md`. Before the first live run an admin must add the
  publisher App to ruleset 24397320's bypass list (see RELEASING.md).
