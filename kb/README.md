# Knowledge base

A structured knowledge base of circuit designs from published work —
topologies, sizing strategies, layout idioms — for the LLM reasoning module
(see `docs/ARCHITECTURE.md`, "Knowledge base") to draw on. This is a data
asset consumed by an agent, not by tooling code, so it is a plain directory
of JSON files validated against a JSON Schema rather than a Python package.

This is a **scaffold**: the schema, sourcing rules, and a handful of seed
entries to prove the shape holds. Growing the entry corpus is separate,
ongoing work — see the parent issue/`ROADMAP.md` ("Beyond the phases").

## Layout

```
kb/
  README.md                        # this file
  schema/
    entry.schema.json              # JSON Schema (draft 2020-12) for one entry
  entries/
    <id>.json                      # one file per entry, filename == id
```

One file per entry (not a single index file) so an agent can enumerate and
read the KB with the standard library alone:

```python
import json
from pathlib import Path

for path in sorted(Path("kb/entries").glob("*.json")):
    entry = json.loads(path.read_text())
    print(entry["id"], "-", entry["title"])
```

JSON, not YAML, per `CLAUDE.md`'s "JSON is the contract" — and it avoids
introducing a YAML parser dependency where none currently exists.

## Schema

Full schema: [`schema/entry.schema.json`](schema/entry.schema.json). Summary
of the fields:

| Field | Required? | Meaning |
|---|---|---|
| `id` | required | Stable slug; must match the filename stem (`kb/entries/<id>.json`). |
| `title` | required | Human-readable name of the design. |
| `topology` | required | Circuit topology name/description. |
| `spec_class` | required | What kind of spec this design serves (e.g. `"low-power PHY bias/reference"`). |
| `source.citation` | required | Full citation: authors, title, venue/year, or repo URL + license. |
| `pdk_portability` | optional | `{ primary_pdk, notes }` — what changes vs. what's portable across PDKs. |
| `sizing_approach` | optional | How device sizes/bias points were derived. |
| `layout_idioms` | optional | Array of layout techniques used (e.g. common-centroid matching, guard rings). |
| `source.url` | optional | Link to the source, if available. |
| `source.license_or_openness` | optional | Why the source clears the sourcing bar below. |
| `notes` | optional | Free-form notes. |

Every seed entry under `kb/entries/` populates all fields (including the
optional ones) to prove the schema against real content — new entries may
leave the optional fields null/omitted when a field genuinely doesn't apply
yet.

## Sourcing rules

Same bar as PDKs (`CLAUDE.md`: "Open PDKs only... Never vendor proprietary
PDK data or reference NDA'd design rules") extended to KB content:

- **Open sources only**: peer-reviewed papers, textbooks (cite the
  topology/methodology; do not reproduce copyrighted figures or text
  verbatim), and open-silicon projects (e.g. sky130 open MPW shuttle
  submissions, which are typically Apache-2.0/CC-licensed on acceptance).
- **Every entry's `source.citation` must be a full citation** — authors,
  title, venue/year, or repo URL plus license. No bare "industry knowledge"
  or unsourced claims.
- **No NDA'd or proprietary material** — identical bar to the PDK rule.
- **When in doubt, leave it out.** The sourcing bar errs conservative, same
  spirit as the PDK rule.

## Adding a new entry

1. Pick a stable slug (`kebab-case`) and create `kb/entries/<slug>.json`.
2. Fill in the required fields (`id`, `title`, `topology`, `spec_class`,
   `source.citation`) plus whichever optional fields apply — do real source
   research and record a real, verifiable citation. Do not fabricate a
   placeholder citation.
3. Validate: `uv run pytest tests/test_kb.py -v`. The test suite loads
   `kb/schema/entry.schema.json` and validates every file under
   `kb/entries/*.json` against it.
4. Send a PR. Cite the schema summary above (or link this README) in the PR
   description so reviewers can check the sourcing rule was followed.
