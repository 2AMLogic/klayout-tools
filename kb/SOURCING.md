# kb/SOURCING.md — sourcing playbook

This is the gate every `kb/entries/*.json` file must clear before it lands.
`kb/README.md` states the hard rule in one line: open sources only, same bar
as `CLAUDE.md`'s "Open PDKs only... Never vendor proprietary PDK data or
reference NDA'd design rules." This document makes that rule concrete: what
sources are allowed, what an entry may record from them, and the checklist to
run per entry before opening a PR.

If you are about to add or edit an entry under `kb/entries/`, read this file
first, then run the checklist at the bottom before you request review.

## Allowed sources

In order of preference:

1. **Open-access papers and preprints** — arXiv, author-hosted preprints,
   open-access journals/proceedings, or any paper whose publisher makes the
   full text freely available. Prefer these when an equivalent open-access
   version exists.
2. **Peer-reviewed literature behind a standard subscription paywall** (e.g.
   an IEEE Xplore or ACM DL page that requires a subscription to read the
   full PDF) — **permitted**, but only as a citation for facts, topology
   descriptions, and methodology that the entry restates in its own words.
   This is ordinary academic citation practice (the same way a textbook or a
   review article cites prior work): you are attributing an idea to its
   published source, not distributing the paywalled content itself. What is
   **not** permitted is reproducing the paper's figures, tables, or text
   verbatim, or copying proprietary numeric data (e.g. a vendor's exact
   process parameters) that the paper itself only has under its own
   restricted license. The three seed entries in this KB use exactly this
   pattern — see "Seed entry audit" below.
3. **Open-source silicon** — efabless/open-MPW shuttle submissions and their
   published design files, open PDK example/reference designs (e.g. the
   `sky130` PDK's own example cells), and other design repositories under a
   permissive license (Apache-2.0, MIT, CERN-OHL, CC-BY, etc.) that explicitly
   allows reuse of the described design knowledge.
4. **Textbook-level common knowledge** — techniques and facts that are
   standard, uncredited knowledge in analog/mixed-signal IC design textbooks
   (e.g. "a two-stage Miller-compensated OTA uses a compensation capacitor
   plus a nulling resistor to push the RHP zero"). Cite the textbook (or the
   canonical paper that originated the technique, if there is one) rather
   than leaving the entry unsourced — see "Provenance is mandatory" below.

### What is explicitly out

- **NDA'd or confidential material** of any kind — the same hard line as the
  PDK rule in `CLAUDE.md`.
- **Paywalled-only proprietary data** — content whose *specific technical
  substance* (not just its typesetting) is only accessible under an NDA or a
  commercial license, e.g. a foundry's proprietary design rule values, a
  vendor's confidential characterization data, or a paid PDK's device models.
  This is different from case 2 above: citing a peer-reviewed paper's
  restated *idea* is fine; reproducing data that is proprietary regardless of
  which paper describes it is not.
- **Proprietary-PDK-specific detail** — design rules, device models, or
  layout constraints tied to a closed PDK. An entry may note that a design
  was *originally* fabricated on a proprietary node (for historical
  attribution) but must not encode that node's proprietary numbers as part of
  the entry's guidance; `pdk_portability` should instead describe how the
  technique maps onto an open PDK (sky130 first, per `CLAUDE.md`).
- **When in doubt, leave it out.** Same conservative default as the PDK rule.

## What gets recorded

An entry may record, in the KB's own words:

- **Facts** about a circuit's behavior, performance, or design trade-offs.
- **Topology names and descriptions**, restated rather than copied — describe
  the structure and how it works, don't paste the source's prose.
- **Sizing strategies** — the *approach* used to derive device sizes/bias
  points (ratios, iteration strategy, what's traded off), not necessarily the
  exact numeric values from a specific paper's example, unless those values
  are themselves open (e.g. from an open-source design's actual netlist).
- **Layout idioms** — named techniques (common-centroid matching, guard
  rings, patterned ground shields, etc.), described generically enough to
  apply beyond the one source design.
- **Measured or reported performance**, always attached to its citation (e.g.
  "reported 0.6V minimum supply operation per [citation]") — never presented
  as if independently verified unless it has been.

An entry must **never** record:

- Reproduced figures (schematics, plots, layout screenshots) from a source.
- Verbatim text lifted from a paper, datasheet, or repository — every field
  is written in the KB's own words.
- Proprietary design rules or any data covered by an NDA.

## Provenance is mandatory

`source.citation` is a **required** field in
[`kb/schema/entry.schema.json`](schema/entry.schema.json) — schema validation
fails without it (see `tests/test_kb.py::test_entry_missing_required_source_citation_fails_validation`).
No entry may claim "industry knowledge" or otherwise ship unsourced. A full
citation means:

- For a paper/preprint: authors, title, venue, and year (see the seed entries
  for the expected format).
- For a repo/open-silicon source: the repo URL plus its license.
- For a textbook: authors, title, edition/year (chapter/section if it helps a
  reader find the specific technique).

`source.url` and `source.license_or_openness` are optional in the schema but
should be populated whenever practical — `license_or_openness` in particular
is where you state *why* this source clears the bar above (e.g. "peer-reviewed
IEEE paper cited for topology/methodology only, no figures or text
reproduced" or "Apache-2.0-licensed open-MPW submission").

## Per-entry ingestion checklist

Run this before opening a PR that adds or edits an entry under
`kb/entries/`:

- [ ] **Source clears the bar above.** It's open-access, a peer-reviewed
      paper cited for restated ideas only, open-source silicon, or
      textbook-level knowledge — not NDA'd, not paywalled-only proprietary
      data, not proprietary-PDK-specific detail.
- [ ] **`id` matches the filename stem** — `kb/entries/<id>.json` has
      `"id": "<id>"` inside it (`tests/test_kb.py::test_entry_id_matches_filename`
      enforces this).
- [ ] **All required fields are populated**: `id`, `title`, `topology`,
      `spec_class`, `source.citation` — non-empty, real content (no
      placeholders).
- [ ] **Optional fields are populated where they genuinely apply**:
      `pdk_portability` (`primary_pdk` + `notes`), `sizing_approach`,
      `layout_idioms`, `source.url`, `source.license_or_openness`, `notes`.
      It's fine to omit or `null` a field that truly doesn't apply yet, but
      don't skip a field just because it takes more research — populating
      more fields makes the entry more useful to the reasoning module.
- [ ] **Citation is complete** per "Provenance is mandatory" above — not a
      bare title, not "TBD".
- [ ] **`license_or_openness` states why the source is allowed**, referencing
      which case above it falls under.
- [ ] **No reproduced content** — re-read the entry and confirm every field
      is written in your own words; no pasted figures, no verbatim text, no
      proprietary numeric design rules.
- [ ] **Schema validation passes**:
      ```bash
      uv run pytest tests/test_kb.py -v
      ```
      This validates the entry against `kb/schema/entry.schema.json`, checks
      `id` == filename stem, and checks a citation is present
      (`tests/test_kb.py` is the enforcement layer for this checklist).
- [ ] **PR description cites this checklist** (or links `kb/SOURCING.md`) so
      reviewers can check the sourcing rule was followed, per
      `kb/README.md`'s "Adding a new entry" step 4.

## Seed entry audit

The three seed entries landed in #26 before this playbook existed. Auditing
them against the rules above:

| Entry | Source | Open-access? | Disposition |
|---|---|---|---|
| `sky130-bandgap-reference` | H. Banba et al., IEEE JSSC, 1999 | No known open-access/preprint version (pre-arXiv-era IC design literature; IEEE Xplore paywall) | **Deviation noted, not fixed.** Cited under case 2 above (peer-reviewed paywalled paper, restated topology/methodology only, no figures/text reproduced) — the entry's `license_or_openness` field already states this explicitly. No proprietary or NDA'd data is recorded. |
| `sky130-spiral-inductor` | C. P. Yue and S. S. Wong, IEEE TED, 2000 | No known open-access/preprint version (same era/venue class as above) | **Deviation noted, not fixed.** Same disposition as above — case 2, restated methodology only, `license_or_openness` already states this. |
| `inverter-based-comparator` | Y. Chae and G. Han, IEEE JSSC, 2009 | No known open-access/preprint version found | **Deviation noted, not fixed.** Same disposition as above — case 2, restated topology/methodology only, `license_or_openness` already states this. |

All three entries were re-checked against the full checklist above:

- `id` matches filename stem for all three — confirmed.
- All required fields populated; all optional fields populated too (schema
  proves out with more than the minimum, per `kb/README.md`).
- Every `source.citation` is a full citation (authors, title, venue, year).
- Every `source.license_or_openness` already explains the citation is for
  topology/methodology only, no reproduced figures or text — consistent with
  case 2 above.
- No field reproduces figures, verbatim text, or proprietary design rules.

No content changes were needed to bring the three seed entries into
compliance — they were already written under the same "cite peer-reviewed
literature for restated facts, never reproduce it" discipline this playbook
formalizes. The only gap was that the discipline wasn't written down
anywhere until now; this file closes that gap. Future entries should still
prefer an open-access/preprint source when an equivalent one exists (case 1
above beats case 2 when both are available).
