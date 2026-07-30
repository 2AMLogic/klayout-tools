# klayout-tools — agent instructions

Tools for AI agents to work with IC layout (GDSII/OASIS via KLayout's
Python API). Public repo, MIT, built in the open by 2AM Logic.

- **The target is the closed loop.** An agent can take a spec →
  schematic/generator → sized circuit → layout → DRC/LVS clean →
  extracted netlist → simulation-verified, on an open PDK, unaided —
  with every step headless and JSON-contracted. `docs/ARCHITECTURE.md`
  defines the scope, the layers, the contract-first rule, and when
  engines get wrapped vs. rewritten — read it before proposing new
  capabilities.
- **Issues track work; docs track direction.** Day-to-day work lives in
  GitHub issues (Loom labels). `ROADMAP.md` and `docs/ARCHITECTURE.md`
  are alignment docs — update them when direction changes, not per task,
  and keep the vision statement identical everywhere it appears.
- **Mirror kicad-tools deliberately.** When adding a capability, check how
  `~/GitHub/kicad-tools` shaped the equivalent (CLI verb naming, `--format
  json` everywhere, MCP tool granularity, worked-example structure) and
  diverge only with a reason.
- **Headless always.** Nothing may require the KLayout GUI; `pya` in batch
  mode only. Every command must be runnable in CI.
- **JSON is the contract.** Human-readable output is a courtesy; the JSON
  schema is the API. Breaking a JSON field is a breaking change. Every `klt`
  verb emits through the shared envelope defined in
  `docs/json-contract.md` (`schema_version`, error shape, exit codes).
- **Open PDKs only** (sky130 first). Never vendor proprietary PDK data or
  reference NDA'd design rules.
- Python 3.10+, `pyproject.toml`/uv, pytest. CLI entry point is `klt`.
- Site lives in `site/` and deploys to Cloudflare Pages (project
  `klayout-tools`, personal account, custom domain klayout-tools.org) via
  `scripts/deploy-site.sh`.

<!-- BEGIN REPO-SKILLS -->
This repository has [Repo Skills](https://github.com/rjwalters/repo) v0.6.1 installed —
general repository hygiene and environment commands invoked as `/repo:<command>`. Run
`/repo:help` for the command list, or see `.claude/skills/repo/SKILL.md` for the full
guide. Hygiene commands apply safe, reversible fixes by default and report each
change; run with `--ask` to review first, and `--prune` to allow irreversible
removals. Managed by `install.sh` — edit outside the markers only.
<!-- END REPO-SKILLS -->

<!-- BEGIN LOOM ORCHESTRATION -->
This repository uses [Loom](https://github.com/rjwalters/loom) for AI-powered development orchestration — see the Loom repository for the full guide (roles, labels, worktrees, configuration). When installed, Loom also writes a locally-substituted copy of that guide to `.loom/CLAUDE.md`.
<!-- END LOOM ORCHESTRATION -->