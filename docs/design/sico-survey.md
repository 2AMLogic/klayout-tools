# Survey: SiCo (opencad-abu/SiCo)

**Status:** exploration finding, not a spike. Mined per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Mining the outside world" and
the intake convention in [`../library/README.md`](../library/README.md).
Adoptable findings are filed as unlabeled follow-up issues (§4); this
document does not itself authorize implementation.

**What was read vs. inferred:** the repository was shallow-cloned from
[opencad-abu/SiCo](https://github.com/opencad-abu/SiCo) `main` (head
`3d38bb5`, 2026-10-05). Its source tree was read directly (~183k lines of
Python/SKILL, surveyed area by area). Findings about the *unpublished*
components (§1) are inferred from module and symbol names in its compatibility
manifests only, and are labelled as such. The "already covered here" checks in
§3 come from targeted greps of this repo plus reads of the relevant
`docs/cli/*.md` sections. They are not a full audit.

## Licensing

SiCo is **GPL-3.0**, and this repo is MIT. **Nothing here may be ported from
its source.** Every finding below is an idea restated in this repo's own
words, to be implemented clean-room. Follow-up issues repeat this
constraint.

## 1. What SiCo is

SiCo ("Silicon Copilot") is a Chinese-language AI-assistant platform for
**Cadence Virtuoso** custom-IC design, built on the OpenAI Codex CLI. Its
README lists GPT, DeepSeek and OpenCAD as authors, in that order. The public
repo is a subset of the full product:

- **Agent host** (`tools/sico/`): a PyQt desktop app plus a session
  service that brokers Codex tool calls into Virtuoso.
- **Flow wrappers** around commercial engines: Calibre DRC/LVS, Quantus/
  StarRC parasitic extraction, a pure-Python DSPF analyzer (`tools/rce/`),
  a multi-tech netlister (`tools/mtsnl/`), LEF generation via Abstract
  Generator replay (`tools/lef/`), and LSF job monitoring.
- **Governance tooling** (`tools/utility/`): a static CI gate that limits
  how far the AI-written code can sprawl.

The circuit-design knowledge (prompts, tool catalog, an LDO design agent)
lives in packages that are **not published**: `cadai`, `aivw`, and
`aiassistant`. From the manifests, the LDO agent appears to have
qualification gates (`QUALIFIED`, `FAIL_REPEAT`, `NEEDS_MORE_EVIDENCE`,
`BLOCKED_INPUT`, `STALE_SOURCE`, …), a negative-injection test matrix, and
a candidate → gate-feedback → revision loop. **No analog design content is
recoverable from this repo.**

## 2. Adoptable findings

### 2.1 Parasitic-network analysis (strongest)

SiCo's DSPF analyzer treats an extracted RC netlist as data an agent can
query:

- **Per-net integrity.** Opens are found with union-find over the net's
  terminals. The analyzer also reports floating islands, orphan nodes,
  cross-net resistors under a threshold (short candidates), negative,
  zero, non-finite or self-loop resistors, and declared vs. computed total
  capacitance. Long detail lists are capped with a `details_truncated` flag.
  Results are status values (`pass`/`open`/`not_applicable`), not
  exceptions.
- **Point-to-point resistance.** It finds the lowest-resistance path
  between two terminals of one net (Dijkstra), with an explicit size cap.
  The status is one of `ok`/`limit_exceeded`/`node_not_found`/`no_path`,
  and the path itself is returned.
- **Index once, query many times.** It parses a DSPF file into a SQLite
  index, keyed on file identity (size, mtime, inode) so an unchanged file
  is not re-parsed.

This repo emits lumped per-net R/C. Its distributed ladder
(`--distributed-rc`) and `klt power`'s resistive grid produce real
networks, but nothing checks how those networks are built or answers
two-terminal queries on them. → #2764, #2765. Indexing is not filed:
our networks are small enough today that re-parsing is not a measured
bottleneck.

### 2.2 Governance for an AI-written codebase

SiCo's gate is one static CI entry point (exit 0/1/2, with a JSON report).
Two of its mechanisms carry over directly:

- **A size ratchet compared against the merge-base.** Per-file line
  ceilings live in a JSON baseline, which is read from the merge-base
  rather than the working tree, so a PR cannot loosen its own gate.
  Ceilings can only go down, and a ceiling that is no longer tight must be
  lowered. Vendored code is recognized by content hash, not directory
  name. This repo has no such gate (`extract.py` ≈ 9.8k lines). → #2766
- **Declarative import boundaries.** Allowed imports are declared in JSON
  and checked against the AST. Shared internals list the exact consumers
  that may import their private symbols. That would turn this repo's
  "verb modules stay self-contained except PDK resolution" rule from a
  doc statement into a CI failure. → #2767

Not adopted:

- **Surface-hash review records.** A recorded review goes stale when a
  module's import/export/signature hash changes. The idea is interesting,
  but this repo's JSON contract is already pinned by docs and tests.
- **Compatibility-alias manifests**, each with an owner and an exit
  condition. Revisit if re-export shims accumulate.
- **The gate's own doc.** It is mostly a dated log of agent tasks. Treat it
  as a warning about how verbose AI-written governance gets, not as a model.

### 2.3 Smaller `klt` additions

- **DRC rule selection.** Discover a deck's rule groups and run only the
  selected rules, so the DRC loop iterates faster. → #2768
- **LEF post-emission check.** Check that VERSION is present, MACRO/END
  lines balance, and the emitted macro set exactly equals the expected
  set (report missing and unexpected names). → #2769
- **Run-directory lock.** A non-blocking lock on the output directory, so
  concurrent runs cannot interleave artifacts. This matters more here than
  in SiCo, because Loom runs many agents. → #2770

### 2.4 Agent-host patterns (recorded, not filed)

These shape how an agent drives the tools, rather than adding tool
capabilities. They are worth recalling when the MCP surface or the design
skills are revised:

- **Typed result statuses.** `waiting_user`, `needs_reconcile`,
  `preflight_failed` and similar each carry a `next_action`. Large outputs
  are moved to a file and replaced by a pointer.
- **No replay when the outcome is unknown.** Every mutating operation
  carries a `request_id`. After an interruption the agent must query that
  id and must never resubmit.
- **Run outcomes are kept distinct.** Tool failure, design mismatch (LVS
  "completed but incorrect") and explicit waiver are three separate
  outcomes. Stale stage outputs are rejected by input signature. Here, the
  `design-signoff` skill already checks staleness. Whether every verb keeps
  "tool failed" separate from "design mismatch" has not been audited.
- **Evidence-bound reports.** A report cites only listed evidence ids,
  whose checksums are re-verified, and "completed" never means "spec met".
  This is close to the `klt signoff` T1–T4 tiers.
- **Recorded decisions.** Before asking the user anything, the agent
  records the decision with its options, a recommendation and the evidence.
  Independent work continues while the question is pending.

## 3. Checked and already covered here

- **Model-corner detection** from `.lib` sections rather than file names,
  and **stripping generated `temp=` lines**: already handled by `klt sim`
  (see `docs/cli/sim.md` → per-section corners and `.options` temperature
  handling).
- **Pin-order checks** against the reference subcircuit: `klt lvs` resolves
  library pin orders (`parse_subckt_pin_orders`, `lvs_mismatch.py`).
- **Scoping a netlist to one top subcircuit:** partly covered by
  `klt netlist --block` and `extract_subcircuit.py`. Not filed.

## 4. Follow-up issues

| Issue | Finding |
| --- | --- |
| [#2764](https://github.com/2AMLogic/klayout-tools/issues/2764) | pex: per-net RC network integrity report |
| [#2765](https://github.com/2AMLogic/klayout-tools/issues/2765) | pex: point-to-point resistance query |
| [#2766](https://github.com/2AMLogic/klayout-tools/issues/2766) | ci: monotonic per-file size ratchet against the merge-base |
| [#2767](https://github.com/2AMLogic/klayout-tools/issues/2767) | ci: static verb-module import boundaries |
| [#2768](https://github.com/2AMLogic/klayout-tools/issues/2768) | drc: list rules and run a selected subset |
| [#2769](https://github.com/2AMLogic/klayout-tools/issues/2769) | lef-abstract: structural self-check |
| [#2770](https://github.com/2AMLogic/klayout-tools/issues/2770) | output-directory lock for concurrent runs |

## 5. Not adopted

- **Cadence/Calibre glue**, i.e. Virtuoso SKILL, runset generation and the
  `nl2view` OA import. It is outside the open-PDK, headless scope.
- **The desktop GUI and the menu generator.**
- **The unpublished design agents.** There is no content to mine. Revisit
  if `cadai`/`aivw` are ever published under a compatible license.
