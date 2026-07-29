# klayout-tools — agent instructions

Tools for AI agents to work with IC layout (GDSII/OASIS via KLayout's
Python API). Public repo, MIT, built in the open by 2AM Logic.

- **Mirror kicad-tools deliberately.** When adding a capability, check how
  `~/GitHub/kicad-tools` shaped the equivalent (CLI verb naming, `--format
  json` everywhere, MCP tool granularity, worked-example structure) and
  diverge only with a reason.
- **Headless always.** Nothing may require the KLayout GUI; `pya` in batch
  mode only. Every command must be runnable in CI.
- **JSON is the contract.** Human-readable output is a courtesy; the JSON
  schema is the API. Breaking a JSON field is a breaking change.
- **Open PDKs only** (sky130 first). Never vendor proprietary PDK data or
  reference NDA'd design rules.
- Python 3.10+, `pyproject.toml`/uv, pytest. CLI entry point is `klt`.
- Site lives in `site/` and deploys to Cloudflare Pages (project
  `klayout-tools`, personal account, custom domain klayout-tools.org) via
  `scripts/deploy-site.sh`.

<!-- BEGIN LOOM ORCHESTRATION -->
This repository uses [Loom](https://github.com/rjwalters/loom) for AI-powered development orchestration — see the Loom repository for the full guide (roles, labels, worktrees, configuration). When installed, Loom also writes a locally-substituted copy of that guide to `.loom/CLAUDE.md`.
<!-- END LOOM ORCHESTRATION -->