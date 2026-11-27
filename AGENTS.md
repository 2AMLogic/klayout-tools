<!-- BEGIN LOOM ORCHESTRATION (AGENTS) -->
This repository uses [Loom](https://github.com/rjwalters/loom) for AI-powered development orchestration (dual-runtime: Claude Code reads `CLAUDE.md`; OpenAI Codex CLI and other AGENTS.md-aware runtimes read this file). See the Loom repository for the full guide (roles, labels, worktrees, configuration). When installed, Loom also writes a locally-substituted copy of the runtime-neutral guide to `.loom/AGENTS.md`.
<!-- END LOOM ORCHESTRATION (AGENTS) -->

Before running `.loom/scripts/resync-installed.sh` without `--dry-run`, read
[Resync on a shared worker](docs/loom-pin-drift.md#resync-on-a-shared-worker):
a plain apply can install user-level Loom files on a shared worker unless it
opts out.
