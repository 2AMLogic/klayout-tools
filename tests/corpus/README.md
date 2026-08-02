# Test corpus

A small, checked-in corpus of open-licensed example layouts from the
sky130 and gf180mcu open PDK ecosystems, plus golden JSON fixtures for the
`klt` CLI. Used by the corpus-backed tests in `tests/test_corpus.py`.

Kept intentionally small (standard-cell-sized GDS files, tens of KB total) —
this is a public repo and the corpus is checked into git.

## Provenance

All files below are individual standard-cell GDSII layouts, downloaded
verbatim (unmodified) from the upstream repositories at the pinned commit.
Both source repositories are licensed **Apache License 2.0**, which permits
redistribution; the `LICENSE` text is unmodified and available at each
source repo linked below (also mirrored in this repo's root `LICENSE`
statement: the corpus files remain Apache-2.0, distinct from this repo's
overall MIT license — see "License note" below).

### sky130 — `tests/corpus/sky130/`

- **Source repo**: [google/skywater-pdk-libs-sky130_fd_sc_hd](https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd)
  (the `sky130_fd_sc_hd` high-density standard cell library, part of the
  SkyWater 130nm open-source PDK ecosystem)
- **Commit**: `ac7fb61f06e6470b94e8afdf7c25268f62fbd7b1` (2020-11-10)
- **License**: Apache License 2.0 (`LICENSE` at repo root)

| File | Upstream path | Size |
| --- | --- | --- |
| `sky130_fd_sc_hd__inv_1.gds` | `cells/inv/sky130_fd_sc_hd__inv_1.gds` | 3.6 KB |
| `sky130_fd_sc_hd__nand2_2.gds` | `cells/nand2/sky130_fd_sc_hd__nand2_2.gds` | 5.0 KB |
| `sky130_fd_sc_hd__buf_4.gds` | `cells/buf/sky130_fd_sc_hd__buf_4.gds` | 5.4 KB |
| `sky130_fd_sc_hd__dfxtp_2.gds` | `cells/dfxtp/sky130_fd_sc_hd__dfxtp_2.gds` | 12.4 KB |

Chosen for variety: a minimal inverter, a 2-input gate, a buffer, and a
D flip-flop (more cells/layers/shapes, exercising sequential-cell geometry).

### gf180mcu — `tests/corpus/gf180mcu/`

- **Source repo**: [google/globalfoundries-pdk-libs-gf180mcu_fd_sc_mcu9t5v0](https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_sc_mcu9t5v0)
  (the `gf180mcu_fd_sc_mcu9t5v0` standard cell library, part of the
  GlobalFoundries 180nm MCU open-source PDK ecosystem)
- **Commit**: `3781fd2951da6e3dc4600e52d997398a9463caeb` (2022-12-05)
- **License**: Apache License 2.0 (`LICENSE` at repo root)

| File | Upstream path | Size |
| --- | --- | --- |
| `gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds` | `cells/clkinv/gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds` | 2.2 KB |
| `gf180mcu_fd_sc_mcu9t5v0__and2_1.gds` | `cells/and2/gf180mcu_fd_sc_mcu9t5v0__and2_1.gds` | 3.1 KB |
| `gf180mcu_fd_sc_mcu9t5v0__dffnq_1.gds` | `cells/dffnq/gf180mcu_fd_sc_mcu9t5v0__dffnq_1.gds` | 7.4 KB |

Chosen for the same variety as the sky130 set: a clock-tree inverter, a
2-input gate, and a D flip-flop.

### License note

The corpus files above are redistributed under their original **Apache
License 2.0** terms (unmodified content, provenance recorded here per
Apache-2.0 §4). This is distinct from — and does not change — the MIT
license that covers the rest of this repository (`klayout-tools`' own
source code); only these specific upstream files carry the Apache-2.0
license.

## Golden fixtures — `tests/corpus/golden/`

`tests/corpus/golden/<pdk>/<cell>.layers.json` holds the expected
`klt layers --format json` output (i.e. the `layers_report()` result) for
the corresponding corpus file, with the `file` field recorded as the
repo-relative path for readability.

Regenerate after adding/removing a corpus file, or after a deliberate
change to the `layers_report()` schema:

```bash
python tests/corpus/generate_golden.py
```

## Adding a new corpus file

1. Download the file into `tests/corpus/<pdk>/`, verifying the upstream
   license permits redistribution (Apache-2.0 / similarly permissive).
2. Record its provenance in the table above (source repo, pinned commit,
   license, upstream path).
3. Run `python tests/corpus/generate_golden.py` to generate its golden
   fixture.
4. `tests/test_corpus.py` picks up new corpus files automatically (it
   globs `tests/corpus/<pdk>/*.gds` / `*.oas`).

## Related: golden-metrics fixtures

The golden fixtures here pin a `klt` verb's **full** JSON response
byte-for-byte. For a narrower, **tolerance-banded scalar-metric** regression
net (device counts, cell/instance counts, DRC violation counts, ...) — the
right bar for a metric drifting slightly without the underlying geometry
becoming byte-different — see [`tests/golden_metrics/README.md`](../golden_metrics/README.md)
(issue #248).
