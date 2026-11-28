# Tiny Tapeout benchmark harness (Epic #520)

This page is the README of the Tiny Tapeout benchmark harness. It records
what the harness measured, where the per-design data and its attribution
live, the exact commands, pins and tool versions that reproduce a reported
number, and the status of the upstream offers. It is Phase 3 of
[#520](https://github.com/2AMLogic/klayout-tools/issues/520) (issue #8119).
Phase 1 built the fetch cache, the `drc`/`lvs`/`extract` runner and the
triage tool, and ran `drc` and `extract` over the GF180 shuttles. Phase 2
measured synthesis baselines, ran one optimization configuration against
them and gated every result on formal equivalence.

The designs are third-party Tiny Tapeout projects used under their own
licenses. Every outcome below is a measurement of klayout-tools on a pinned
input. None is a claim about a design, and nothing in the corpus is
redistributed as this project's own work.

## Guardrails

These apply to the harness, its data and anything written about them.

- **Attribution is not optional.** Every published row names its design,
  author, repository, pinned commit and license.
- **This is a benchmark, not a source of designs.** Nothing in the corpus is
  reimplemented, rebranded or shipped as this project's work.
- **No claim outruns the measurement.** A result is reported with its sample
  size and its exclusions together.
- **Respect the community.** These are largely student and hobbyist
  projects. Anything published about them leads with what the harness
  found and credits whose designs made it findable.
- **Rate-limit the ingest.** The fetcher sends an identifying `User-Agent`,
  paces requests and caches every artifact by content hash.

## What is published

| File | Holds | SHA-256 |
| --- | --- | --- |
| `tests/corpus/tt06_sky130_sample.json` | Phase 2 sample: shuttle, repository and pinned commit of the four `tt06` designs | `29153b9409dfaae30bc83fab2e626c6285bf3f60313c6cb72816d29286a2ee19` |
| `tests/corpus/tt06_sky130_attribution.json` | Title and author of each Phase 2 design, read from the pinned shuttle index | `28b3842360ac95327c515a636fe96831945697c39f14b90ab47c64dda3153483` |
| `tests/corpus/tt_sky130_baseline.json` | Phase 2 baseline: per-design instance count, source digest and license, and the tool pins | `c8b1e1410fbe0cdbd4fab4ce4770c7afe152983945efc3fc4cfd2a414559854f` |
| `tests/corpus/tt_sky130_optimized.json` | Phase 2 results: per-design baseline, optimized count, signed delta and formal verdict, or the exclusion reason | `f7f0e1429a96a7a8cd2e6753733f8b420a854ae2cd3df79aa3185178e550ef28` |
| `tests/corpus/tt_gf180_slots.tsv` | Phase 1 catalogue: repository, commit, SPDX license and license blob of each of the 108 GF180 slots | `0ce96c2b0c925db05db25203ff95eb5f2722cdc22d5d1412562f7b87179f06e1` |
| `tests/corpus/tt_gf0p2_drc_sample.json` | Phase 1 CI sample: the two pinned GF180 designs with title and author | `f2dbaf4bc66f5797c373d75fd0cacd77806da142e18cbbabde43970a232ec5e2` |

[`docs/reports/tt-gf180mcu-corpus-run.md`](../reports/tt-gf180mcu-corpus-run.md)
is the Phase 1 run record: every slot's `drc` and `extract` outcome, wall
time, markers and device counts, with provenance and cost. It is a written
report, so it carries no hash here.

The tools are `scripts/tt_corpus_fetch.py`, `tt_corpus_run.py`,
`tt_corpus_triage.py`, `tt_corpus_sample.py`, `tt_corpus_batch.py`,
`tt_corpus_baseline.py`, `tt_corpus_optimize.py` and `tt_corpus_equiv.py`.
Each one's module docstring is its usage line.
`tests/test_tt_benchmark_attribution.py` fails when this page's tables,
hashes or headline drift from the files above.

## Attribution

### Phase 2: the `tt06` sky130A sample

Titles and authors are read from the shuttle index at
`TinyTapeout/tinytapeout-index` commit
`dfa8764c60911bd85ec42b9c30572c747cb81444` (`index/tt06.json`, SHA-256
`b133c63f2b78d345e7676abd76f71965af29c6efb4a8c8f9d4750286418d3703`) and
stored in `tests/corpus/tt06_sky130_attribution.json`. The license is read
from each design's own repository at the pinned commit.

| Design | Title | Author(s) | Repository | Commit | License |
| --- | --- | --- | --- | --- | --- |
| `tt_um_8bitALU` | 8bit ALU | David Parent, Chih-Kaun Ho, Edric Ong | [EdricOngKhaiJieh/8bitALU_verilog](https://github.com/EdricOngKhaiJieh/8bitALU_verilog) | `f9bcfd7d3cb6da2972d1678d9ec5e9402ae9eecb` | Apache-2.0 |
| `tt_um_ALU` | 3-bit ALU | José Raña Gámez | [JoseKaisen/ALU_3bits](https://github.com/JoseKaisen/ALU_3bits) | `aef5319d47e2f6644e4df935d932ae1136e0cc20` | Apache-2.0 |
| `tt_um_CKPope_top` | X/Y Controller | Charles Pope | [CKPope/tt06-verilog-template](https://github.com/CKPope/tt06-verilog-template) | `c93539faf09c864f1e4ed1ac31a8b16fcb5d7b73` | Apache-2.0 |
| `tt_um_LFSR_shivam` | 10-bit Linear feedback shift register | Shivam Bhardwaj, Sachin Sharma, Pankaj Lodhi and Ambika Prasad Shah | [beaprog/tt06-LFSR](https://github.com/beaprog/tt06-LFSR) | `6bffe5f606707446700dc5dc43db8fa4d6a74394` | Apache-2.0 |

The published Phase 2 files record each design's repository, commit, source
archive digest and license, so every row of
`tt_sky130_baseline.json` and `tt_sky130_optimized.json` joins to this table
by macro, repository and commit.

### Phase 1: the GF180 shuttles

`tests/corpus/tt_gf180_slots.tsv` carries attribution for all 108 slots of
`ttgf0p1`, `ttgf0p2` and `ttgf0p3`: the indexed repository URL and commit, the
SPDX identifier of the root license read from the repository at that commit,
and the Git blob of the license file. 103 slots are Apache-2.0 and 2 are
AGPL-3.0. Three slots have no attribution to carry and are explicit skips in
the run record: `tt_um_pad_test` has no indexed source, and `tt_um_essen` on
`ttgf0p2` and `tt_um_rahulbhagwat_gf180_bandgap_reference` on `ttgf0p3` have
no recognized root license at their pinned commit. Every measured slot has a
repository, a commit and a license.

The CI sample names its two designs in
`tests/corpus/tt_gf0p2_drc_sample.json`: Dog Battle Game by Sophus Andreassen
(`jorgenkraghjakobsen/tt_gf_dog_fight`) and easy PAL by Ken Pettit
(`kdp1965/ttgf-um-easy-PAL`).

The harness publishes digests and counts, not design source. Source archives
are fetched at their pinned commits into a local cache and stay there. The
one place this repository vendors Tiny Tapeout RTL is
`tests/corpus/synth_e2e_validation/sources/`, under the Apache-2.0 terms its
README records.

## The reported numbers

Phase 2 applied one optimization configuration, `abc-area-map`, to the four
pinned designs. It replaces the synthesis request's ABC script with
`area.abc` (`strash`, `dch -f`, `map -a`). The gate proved three pairs
equivalent and could not establish the fourth.

| Design | Baseline | Optimized | Delta | Verdict |
| --- | ---: | ---: | ---: | --- |
| `tt_um_8bitALU` | 110 | 118 | +8 | equivalent |
| `tt_um_ALU` | 97 | 127 | +30 | equivalent |
| `tt_um_CKPope_top` | 169 | – | – | unestablished |
| `tt_um_LFSR_shivam` | 38 | 46 | +8 | equivalent |

Counts are mapped instances in `sky130_fd_sc_hd` at `tt_025C_1v80`, taken
from `klt synthesize`'s whole-design `instance_count`. The gate's headline,
computed only from proven pairs, is:

```text
mean -19.7511% across 3 designs, 1 excluded
```

What this says: under this configuration every proven design used more
instances than under the default flow. It is a regression of the tested
configuration against klayout-tools' own default, measured on four designs.
It says nothing about the designs, whose authors' own flows were not
measured. `tt_um_CKPope_top` is excluded because the sequential proof
blacklisted 400 internal wires it could not match; the gate reports that as
`unestablished`, never as a mismatch.

## Reproducing a Phase 2 number

The number reproduced here is `tt_um_LFSR_shivam`: 38 instances at baseline,
46 under `abc-area-map`, delta +8, verdict `equivalent`. The same commands
regenerate both published Phase 2 files byte for byte when every pin below
matches.

### Pins

These are the `pins` recorded in `tests/corpus/tt_sky130_baseline.json`. The
runners refuse to mix a row measured under any other identity into a report.

| Pin | Value |
| --- | --- |
| klt | `0.9.3.dev5792+g66333db112d3`, git commit `66333db112d3d24abbf4cbb4db5886f661e80709` |
| Yosys | `Yosys 0.67+post (git sha1 b8e7da6f40ae8f552c116bf6c359b07c6533e159, Release, GNU /usr/bin/c++ 13.3.0)` |
| ABC | `UC Berkeley, ABC 1.01 (compiled Nov 19 2026 03:14:20)`, `yosys-abc` binary SHA-256 `7a05c2739dc522da9ee08bdb2e4c269478ef63d6b36f9a6c1bb62e373320e408` |
| PDK | `sky130A`, `open_pdks f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7` |
| Liberty | `sky130_fd_sc_hd__tt_025C_1v80`, SHA-256 `8e78e14442062dba34d414fca6490b2f6b96038d4510d1438ca44fee31487135` |
| ABC script | `area.abc`, SHA-256 `f7ab4fd5aaa2fca7ecbd4809d8f92a17cfcf543e063b26214e359e5c5bdad191` |

klt at that commit requires Python 3.10 or later. The published files do not
record the Python interpreter or the `klayout` wheel of the measuring host;
the pins above are the identities the runners check.

### Toolchain

Yosys must be on `PATH` as the `0.67+post` build at git sha1 `b8e7da6f`, with
its `yosys-abc` beside it, which is how the runners locate ABC. For a
byte-exact run that binary must be the one hashed above. It is not
published, and a rebuild of the same source carries a new compile stamp and
a new hash, so the pins cannot reconstruct it. "Off the pinned build" below
says what reproduces without it.

Install `sky130A` at the pinned `open_pdks` revision, for example with
[volare](https://github.com/precisioninnovations/volare), and confirm it with
klt, which resolves the install through its documented search order:

```sh
klt pdk find --pdk sky130A --expect-version f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7
```

The scripts live in a current checkout. The pinned klt predates them, so
build it beside the checkout and put it first on `PATH`:

```sh
git clone https://github.com/2AMLogic/klayout-tools.git
cd klayout-tools
git worktree add ../klt-66333db1 66333db112d3d24abbf4cbb4db5886f661e80709
(cd ../klt-66333db1 && uv sync --frozen --extra dev)
export PATH="$PWD/../klt-66333db1/.venv/bin:$PATH"
klt version    # klt 0.9.3.dev5792+g66333db112d3d
```

### Commands

Run from the repository root. The fetch makes four requests, one per design,
and caches the archives under `.cache/tt-corpus`.

```sh
python scripts/tt_corpus_fetch.py --shuttle tt06 --pdk sky130A \
    --index-file tests/corpus/tt06_sky130_sample.json
python scripts/tt_corpus_baseline.py --output /tmp/tt-baseline.json
cmp /tmp/tt-baseline.json tests/corpus/tt_sky130_baseline.json
```

`tt_corpus_baseline.py` runs in about ten seconds. A silent `cmp` means the
four counts and every pin reproduced. `tt_um_LFSR_shivam` is the fourth
design; its `instance_count` is 38.

The sweep maps both sides of every design, the gate proves each pair and the
report admits only proven pairs:

```sh
python scripts/tt_corpus_optimize.py prepare --work-dir run
root=$(klt pdk find --pdk sky130A --format json \
    | python -c 'import json, sys; print(json.load(sys.stdin)["root"])')
lib="$root/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
sha256sum "$lib"    # 8e78e14442062dba34d414fca6490b2f6b96038d4510d1438ca44fee31487135
python scripts/tt_corpus_equiv.py --pairs run/pairs.json --output run/verdicts.json \
    --liberty "$lib"
python scripts/tt_corpus_optimize.py report --pairs run/pairs.json \
    --verdicts run/verdicts.json --output run/results.json
cmp run/results.json tests/corpus/tt_sky130_optimized.json
```

`--work-dir` must be a directory that does not exist yet; it keeps both
requests, both mapped netlists and both reports of every design.
`tt_corpus_equiv.py` prints the headline,
`mean -19.7511% across 3 designs, 1 excluded`. A silent second `cmp` means
the results file reproduced.

To read the number from the regenerated file:

```sh
python - <<'EOF'
import json

with open("run/results.json") as stream:
    results = json.load(stream)
row = {x["macro"]: x for x in results["designs"]}["tt_um_LFSR_shivam"]
print(row["baseline_count"], row["optimized_count"], row["delta"], row["verdict"])
EOF
```

```text
38 46 8 equivalent
```

### Off the pinned build

A host without that exact `yosys-abc` binary reproduces the counts and the
mapped netlists, but not the files. The runners compare the whole pin set and
exclude any row measured under a different one, so
`tt_corpus_optimize.py prepare` reports `baseline synthesis pins differ from
pinned baseline` for every design instead of emitting a results file. On
such a host, run the fetch and `tt_corpus_baseline.py` and compare the four
`instance_count` values with `tests/corpus/tt_sky130_baseline.json`.

That was measured on 2026-11-28 on a host with the pinned Yosys, PDK and
Liberty, a rebuilt `yosys-abc` (`UC Berkeley, ABC 1.01 (compiled Nov 22 2026
07:08:28)`, SHA-256 `339f646104bb8238308cdffbb472aec78088851c2bf593e0f6cd8aef12519eae`)
and KLayout 0.30.13. With the pinned klt on `PATH`, `tt_corpus_baseline.py`
reproduced the whole `designs` array and differed from the published file
only in `abc` and `abc_sha256`. With klt `0.9.3.dev6021+gbfcb6cee90ee` it
reproduced the four counts, and the four mapped baseline netlists had the
`baseline_sha256` digests published in `tt_sky130_optimized.json`.

## Reproducing a Phase 1 number

The Phase 1 number to reproduce is Dog Battle: `klt drc` reports zero
violations on its pinned layout. `scripts/tt_corpus_sample.py` fetches the
two sample layouts at shuttle commit `622aa779a5e7cf4c1007c6f6ad939678d21897b0`
and runs `klt drc` on them, and fails unless every design is clean:

```sh
uv run --frozen python scripts/tt_corpus_sample.py
```

It needs `klt` with a KLayout engine and takes a few minutes. The same check
runs in CI on every pull request; the archived reports of the run on `main`
at `b6df1b14b5b4` show zero violations for both designs
([run 45648219286](https://github.com/2AMLogic/klayout-tools/actions/runs/45648219286)).
The runner's stage command is
`klt drc <layout> --deck gf180mcu --top <macro>`, against klayout-tools'
curated `gf180mcu` deck, which is not the foundry signoff runset.

The full campaign is in
[`docs/reports/tt-gf180mcu-corpus-run.md`](../reports/tt-gf180mcu-corpus-run.md),
which pins the klt build, KLayout version, deck hash, source commit and
shard layout it ran with and links the campaign ledger. It ran as seven paid
Spot launches with a cost bound of $17.86; it is documented for inspection,
not a command to rerun. Each row of its per-design table joins to
`tt_gf180_slots.tsv` by shuttle and macro for the repository, commit and
license.

## Limits of the published data

- Phase 2 covers four designs of one shuttle on one library and corner, and
  one optimization configuration. Three of the four carry a proven result.
- The counts are klayout-tools' own baseline on that library. No external
  tool, no other library and no author's own flow was measured.
- Byte-exact regeneration of the Phase 2 files is limited to hosts holding
  the measuring host's `yosys-abc` binary.
- The Phase 1 campaign ran `klt 0.9.0` on KLayout 0.30.13 with a
  300-second limit per stage. 19 DRC and 43 extraction timeouts are limits
  of that run, not verdicts on the designs. All eight designs with DRC
  findings differ from the foundry decks: the run record pins the cause on
  five and establishes a deck discrepancy, with the causes still open, on
  the other three.

## Upstream offers

**Status: nothing has been submitted.** No pull request, issue or public
write-up has been opened on any design's repository, and none will be
without the operator's approval. The 2026-08-10 classification on #520 holds
that a contribution under this project's identity is an outward-facing
action.

**The sweep produced no improvement to offer.** The epic asks for an
upstream offer where the sweep produces a genuine improvement to a project.
Under the one configuration measured, every proven design used more
instances (see "The reported numbers"). Offering that to an author would
not be an improvement, so no offer is prepared for submission.

**What an offer must carry when one exists.** The harness measures
`klt synthesize` configurations and produces no change to a design's own
files, and a Tiny Tapeout project's flow runs its own synthesis. So what it
can offer an author is the measurement itself, as an issue or discussion on
the design's repository. A configuration that beats the baseline on a proven
pair is offered there with what was measured, the author's name with the
repository and commit, the count with its sample size and exclusions, the
verdict bound to the two netlist digests, and the reproduce commands above.
It is offered, not pushed. If a later configuration yields a change the
author's flow can consume, the same content is the pull request body.
Draft:

```text
Title: <design>: <N> -> <M> instances under <configuration> (klayout-tools benchmark)

Hi <author>. <title> is one of four tt06 designs in the Tiny Tapeout
benchmark we published (docs/guides/tinytapeout-benchmark.md), where it is
credited to you under its Apache-2.0 license.

Mapped to sky130_fd_sc_hd at tt_025C_1v80 with klt synthesize, the design at
<commit> uses <N> instances. With <configuration> it uses <M> (<signed
delta>), and Yosys proved the two mapped netlists equivalent. The proof is
bound to baseline <baseline_sha256> and candidate <candidate_sha256>. This is
one configuration on one library and corner, not a signoff result.

Both requests, both netlists and the reproduce commands are at <link>. We
are offering the measurement in case it is useful to you. It is your design;
close this if it is not.
```

The attribution lines for the four sample designs are in "Attribution" above.
