# Changelog

`klt` has not yet reached `1.0`; per [`docs/json-contract.md`](docs/json-contract.md),
`schema_version` only bumps for non-additive (breaking) shape changes to a
command's own payload. Additive behavior changes — including new
`mismatches[].category` values `klt lvs` can emit — land under the same
`0.1.0` package version and are recorded here instead. This file is the
source of truth for which categories exist as of a given date; pin
`provenance.deck` (sha256) and `provenance.klayout_version`, not
`klt --version`, if you need to detect this kind of drift.

## 0.1.0 (2026-07-31)

Initial release — the agent-native IC layout toolkit, first cut.

### Added

- `klt` CLI with five headless, JSON-contracted verbs:
  - `klt layers` — layer/datatype enumeration for GDSII/OASIS streams
  - `klt stats` — bounding box, drawn area, density, polygon/vertex counts (`--per-layer`)
  - `klt cells` — cell hierarchy: top cells, shape/instance counts, bboxes (`--top`)
  - `klt drc` — headless DRC via KLayout's native Region check primitives, with
    curated width/space/enclosure decks for sky130 and gf180mcu (`--deck`)
  - `klt pdk find|list|env` — discovery/resolution of open_pdks-layout PDK installs
- Shared JSON output envelope (`schema_version`, error shape, exit codes) across
  all verbs — `docs/json-contract.md` is the API
- `scripts/fetch-pdks.sh` — pinned fetch of lambdapdk open PDK data
- `kb/` knowledge-base scaffold with JSON Schema and seed entries
- sky130/gf180mcu test corpus with golden fixtures; CI (ruff + pytest, Python 3.10–3.13)
- Docs: architecture, JSON contract, per-verb CLI references, macOS KLayout
  source-build guide; site at klayout-tools.org

### Added since release (still reporting `0.1.0`)

`klt` has grown considerably since the initial release above without a
version bump (see the pre-1.0 note at the top of this file); the entries
below are the user-visible, additive behavior changes worth calling out
explicitly because they affect a verb's output under an unchanged reported
version. Not an exhaustive commit-by-commit log.

- Since 0.1.0 the CLI has grown from 5 verbs to 18: `layout-metrics`,
  `render`, `extract`, `lvs`, `gen`, `gen-compose`, `draw`, `sim`, `kb`,
  `precheck`, `socket-check`, `ring-check`, and `report` were added on
  `main`. Each is documented in [`docs/cli/`](docs/cli/); the next release
  will carry them collectively.
- 2026-08-02 — `klt lvs`: new `device.body_unverified` mismatch category
  (`a483ed0`, #281/#285). Warns (`severity: "warning"`, never changes
  `status`) when a MOS body terminal was extracted onto a deck-synthesized
  net rather than a real drawn tap/well-label net — an NMOS entry fires on
  every inline-extraction LVS run with one or more NMOS devices (no curated
  deck draws a distinct NMOS substrate/tap layer), and a PMOS entry
  additionally fires for decks with no distinct well-tap layer (gf180mcu
  today). This is purely additive (no `schema_version` bump) but changes
  `category_counts` for any gf180mcu (and, for the NMOS case, sky130)
  inline-extraction fixture that previously reported an empty
  `category_counts: {}` — see `docs/cli/lvs.md`'s `device.body_unverified`
  subsection for the full trigger conditions.
- 2026-08-02 — `klt lvs`: new top-level `net_correspondence[]` response
  field (#311). Lists every layout↔reference net pairing the comparer
  matched — unambiguous and ambiguously-resolved alike — as `{layout,
  reference, pin}` entries, sorted and deduplicated per circuit scope so
  `len(net_correspondence) == counts.nets.matched` holds even across a
  hierarchy with cross-circuit net-name collisions. Purely additive (no
  `schema_version` bump) — see `docs/cli/lvs.md`'s `net_correspondence[]
  entries` subsection.
- 2026-08-02 — `klt extract`: the "unmodelled device geometry" diagnostic
  (#288/#299) no longer flags a recognised drawn resistor's own terminal
  head (#324) — a poly component abutting a body region `_resolve_resistors`
  already recognised is now excluded outright, the same way a real MOS gate
  already was, removing a false positive that previously fired on any
  resistor whose wide terminal head carries an ordinary (2+) contact array.
  New top-level `unmodelled_poly[]` response field lists the bounding
  box + `reason` (`"unmarked"` / `"marked_unrecognised"`) of every shape the
  diagnostic still flags, alongside the existing prose `warnings[]` strings.
  Purely additive (no `schema_version` bump) — see `docs/cli/extract.md`'s
  "Known limitation: unmodelled device geometry" subsection. Ordinary poly
  routing tracks sharing the same resistor-body signature remain a known,
  documented false-positive class with a client-side filtering workaround
  via `unmodelled_poly[]`.
