# Spike: a reproducible synthesis observability negative control

**Status:** design spike, part of
[#2281](https://github.com/2AMLogic/klayout-tools/issues/2281), delivered by
[#2746](https://github.com/2AMLogic/klayout-tools/issues/2746). This spike
does not authorize implementation. It adds no `klt` flag, request field or
response field, and no test asserts an unimplemented feature. Production
work needs a separately reviewed follow-up issue (section 10).

**Decision (short form):** no general-purpose "strip observability" pre-pass.
Selecting signals by name and selecting them by attribute both failed on
concrete RTL. They can silently change functional behavior, they cannot
address one instance of a shared module, and Yosys treats a selector that
matches nothing as a warning, not an error.

The recommended bounded alternative is the **explicit wrapper/top
variant**. The caller writes a small wrapper module that instantiates the
unmodified design and leaves the observation outputs unconnected. Both the
baseline and the wrapper are synthesized **flattened** with the same pinned
toolchain.

That works today except for one gap. `klt synthesize` never flattens. On
the hierarchical flow the wrapper control silently shows **no** collapse
(variant B below), which is a false negative. The one recommended
production change is therefore an opt-in flatten request field, plus
optional storage-bit accounting. The negative control itself stays in the
caller's RTL.

## 1. What a negative control has to show

The #2281 area-calibration proposal wants evidence that probe observability
keeps otherwise removable state alive. If the observation path is removed,
probe-only flops should disappear. The functional outputs, and any state
that still drives them, must stay unchanged. A flop-count delta alone
cannot carry that claim, for two reasons:

- A mechanism that also removes functional state produces a *larger*,
  plausible-looking delta. Variant H below does exactly this.
- A final mapped-cell delta does not say which source registers
  disappeared. That needs pre-mapping evidence (section 6).

So every variant below is judged on three things: pre-mapping register
inventory, final mapped storage bits, and a functional-equivalence check of
the declared functional outputs against the original RTL.

## 2. The current flow (verified against `origin/main` @ `39f6da54`)

`src/klayout_tools/synthesize.py`'s generated script runs these steps:

```
read_verilog ...
hierarchy -check -top <top>
synth -top <top>
dfflibmap -liberty <lib>
abc -liberty <lib> -constr <constr>
clean
setundef -zero
hilomap ...
stat -liberty <lib> -json -top <top>
write_verilog ...
```

The relevant points:

- There is **no `flatten`**. `synth -top` leaves the hierarchy intact, and
  `_aggregate_cell_counts` rolls submodule cells up from `stat -json`.
  Section 5 shows why this matters.
- Only `stat` after mapping is captured. Nothing records which registers
  survived `synth`.
- There is no stripping hook. Issue #2281's earlier curation found the same.

## 3. Fixture

[`tests/corpus/synth_observability/`](../../tests/corpus/synth_observability/README.md)
holds the fixture: original MIT RTL, 28 storage bits.

| State | Bits | Drives | Expected after removing observation |
| --- | --- | --- | --- |
| `acc_q` | 8 | functional `acc` (and `probe_q`'s input) | kept |
| `shared_q` | 4 | functional `parity` **and** `dbg_shared` | kept (shared probe/functional) |
| `u_func.probe_q` (`probe_unit`) | 4 | functional `delayed` | kept |
| `probe_q` | 8 | only `dbg_probe` | removed |
| `u_hist.probe_q` (`probe_unit`) | 4 | only `dbg_hist` | removed |

The fixture covers the cases the issue asks for:

- **Reused signal names:** `probe_q` exists in `obs_core` and in
  `probe_unit`, which is instantiated twice. That gives two module
  definitions and three logical instances.
- **Hierarchy:** one `probe_unit` instance is functional and the other is
  observation-only.
- **A probe that also drives functional logic:** `shared_q`.

The expected result is 28 bits before removal and 16 after.

`obs_core_func.v` is the wrapper variant. It instantiates `obs_core`
unchanged with every `dbg_*` output left unconnected.

## 4. Method and provenance

**Command.** One command reproduces everything:

```sh
python3 tests/corpus/synth_observability/run_experiments.py \
    --workdir /tmp/obs2746 --klt "uv run klt"
```

**Pinned inputs** (recorded in the run's `results.json`):

| Item | Value |
| --- | --- |
| klt | worktree at `origin/main` `39f6da54ae4c13eef51ddc525fcb3bac69b4118f` |
| Yosys | `Yosys 0.69+post (git sha1 143eb14f9cc55d6f8927e68523b0c9d2166ed02c, Release, AppleClang clang++ 21.0.0.21000334)` |
| PDK | `gf180mcuA`, `open_pdks f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7`, resolved by `find_pdk()` from `~/.ciel` |
| Liberty | `gf180mcu_fd_sc_mcu7t5v0__tt_025C_1v80.lib` (klt's nominal corner), sha256 `9000042dd82b66d81d671cfc82120f27fe4721abbdfcbd0330e8f95f4ca45726` |
| `obs_core.v` | sha256 `5479aa3b5b28eaad37f0d957171767decbf02a07bc48d886f5ca2968a6489c86` |
| `obs_core_func.v` | sha256 `b3a59e66e848b15a3bcf7a6c11c65e775211dc37d38088f356735ec34b4a4182` |
| Parameters/defines | none |

**How the runs are produced.**

1. The real, unmodified `klt synthesize` runs with `obs_core` as the top
   (baseline) and again with `obs_core_func` as the top (wrapper).
2. Each other variant's script is derived *from the script klt generated*.
   Only the candidate pre-pass is inserted, right after `hierarchy`, and
   `synth -top` becomes `synth -flatten -top` where a variant says so.
3. Pre-mapping evidence is a read-only `stat -json` immediately after
   `synth`. Register names come from a **separate** evidence run that
   stops after `synth` and calls `write_json`.

**The derivation is checked against klt.** The driver asserts that the
derived A and B scripts reproduce klt's own `instance_count` and
`area_um2` exactly. Both did (`derived_scripts_reproduce_klt`).

**`write_json` is not a passive observer.** Putting `write_json` before
`dfflibmap` in the mapping run changed ABC's result on variant A from
3406.95 µm² to 3402.56 µm². Diagnostics that dump the design can perturb
mapping, so evidence capture must run separately from the measured
mapping run, or be shown not to perturb it.

**Equivalence.** Each mapped netlist is compared with the original RTL
(`obs_core_func` over `obs_core`, `proc; flatten`) on the declared
functional outputs `acc`, `parity` and `delayed`. Two checks run:

- An unbounded `equiv_make` / `equiv_simple -seq 5` /
  `equiv_induct -seq 5` / `equiv_status -assert` proof. These are the same
  primitives
  [`seq_equiv_check.sh`](../../tests/corpus/synth_e2e_validation/seq_equiv_check.sh)
  uses. Liberty cells get functional models through
  `read_liberty -ignore_miss_func`, and `async2sync` handles the
  async-reset flops.
- A 24-cycle `sat` bounded model check from an all-zero initial state.

Debug outputs still present on the gate side lose their port flag only
after mapping, with no optimization afterwards, so the mapped logic is
compared exactly as synthesized.

**Reset and initial state.** `acc_q` and `shared_q` have asynchronous
active-low resets. `probe_q` and `probe_unit` have no reset. The bounded
check assumes zero initial state on both sides. The induction proof
assumes nothing about the initial state beyond the matched outputs.

**Reproducibility.** Two runs in different scratch directories produced
byte-identical mapped netlists for every variant (hashes in section 5).

## 5. Results

`pre` and `map` are storage bits counted with the rule in section 7.
`cells` are physical instances; non-physical `$scopeinfo` cells that
`synth -flatten` leaves behind are excluded. Areas come from liberty
`stat`.

| Variant | Mechanism | flatten | pre | map | cells | area µm² | equiv_induct | BMC (24) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | none (= `klt synthesize`, top `obs_core`) | no | 28 | 28 | 81 | 3406.95 | proven | pass |
| A2 | none | yes | 28 | 28 | 81 | 3406.95 | proven | pass |
| B | wrapper (= `klt synthesize`, top `obs_core_func`) | no | **28** | **28** | 81 | 3406.95 | proven | pass |
| C | wrapper | yes | 16 | 16 | 66 | 2553.02 | proven | pass |
| D | top-level output-port names, `delete -port` | no | 16 | 16 | 67 | 2561.80 | proven | pass |
| E | top-level output-port names, `delete -port` | yes | 16 | 16 | 66 | 2553.02 | proven | pass |
| F | `(* klt_observe *)` on top dbg ports (edited copy) | yes | 16 | 16 | 66 | 2553.02 | proven | pass |
| G | `(* klt_observe *)` on `probe_unit.q` | yes | — | — | — | — | Yosys `ERROR` | — |
| H | internal signal name `shared_q`, deleted after `proc` | yes | 24 | 24 | 73 | 2899.86 | **unproven: `parity`** | **FAIL** |

Mapped storage by cell type:

- A, A2 and B: 16 × `dffq_1` and 12 × `dffrnq_1`.
- C, D, E and F: 4 × `dffq_1` and 12 × `dffrnq_1`.
- H: 16 × `dffq_1` and 8 × `dffrnq_1`. The four `shared_q` flops are gone.

Mapped netlist sha256 prefixes:

| Variant | sha256 prefix |
| --- | --- |
| A | `dc6db6124e81f3a0` (equal to klt's `netlist_sha256`) |
| A2 | `d85896a35896b18b` |
| B | `b48ce6c2adfc2d18` |
| C | `14a60b716d85847e` |
| D | `94a366fcd5779d65` |
| E and F | `e56fda21537d31f9` (identical) |
| H | `328262e851b9fc18` |

Pre-mapping register inventory after `synth`:

- **E** keeps `acc_q[7:0]`, `shared_q[3:0]` and `u_func.probe_q[3:0]`.
  Both `probe_q` and `u_hist.probe_q` are gone.
- **C** keeps the same registers under the `u_core.` prefix.
- **B** keeps all 28 bits, because `obs_core` is still a separate module.
- **H** keeps `probe_q` and `u_hist.probe_q` and drops `shared_q`. It also
  emits 16 "used but has no driver" warnings, and the run still exits 0.

## 6. Findings by mechanism

### Caller-selected internal signal names: rejected

- **Functional change (H).** Removing a named internal signal deletes the
  state, not just its observation. `shared_q` feeds both `parity` and
  `dbg_shared`. Deleting it left `parity` undriven. The equivalence proof
  fails on `parity`, and the BMC finds a counterexample. Yosys only warned
  and exited 0. The plausible-looking result (24 bits, −4) hides a broken
  design.
- **Ambiguity.** `select -list w:probe_q` returns `obs_core/probe_q` and
  `probe_unit/probe_q`. These are module *definitions*, and the second
  covers both the functional `u_func` and the probe-only `u_hist`.
  Without flattening, a name cannot address one instance.
- **Validation.** `delete -port obs_core/dbg_nope` prints
  `Selection "dbg_nope" did not match any object` as a **warning** and
  exits 0. Any selector-based implementation has to add its own
  exact-match assertion (`select -assert-count 1 <module>/<name>`), or a
  typo becomes a silent no-op that reports "no collapse".

### Caller-selected top-level output-port names: works, not chosen

Restricting names to **top-level output ports** and removing only the port
flag (D, E) is semantically sound. It removes observation, not state. The
existing optimizer then drops exactly the logic that no longer reaches any
remaining output. The shared `shared_q` survives, and functional
equivalence is proven.

D worked without flattening only because the probe-only `u_hist` instance
lost its *only* consumer, so `opt_clean` removed the whole instance. When
probe-only state sits inside a module that also has functional state, the
hierarchical flow cannot remove it (see B).

This is a viable fallback. It is not chosen, because it needs new selector
grammar, validation and error codes in the request contract, and its
result is identical to the wrapper's (E and C have the same area and
storage).

### Attributes: rejected

- **Source preservation.** On top-level ports (F) the result is identical
  to E, but the attribute has to be in the source. That means editing the
  RTL under measurement or keeping an edited copy (the driver writes one
  into scratch).
- **Hierarchy (G).** An attribute on a submodule port is
  module-definition-scoped. Removing `probe_unit.q` from the definition
  removes it from the functional `u_func` too. Yosys rejects the result:
  `ERROR: Module 'probe_unit' referenced in module 'obs_core' in cell
  'u_hist' does not have a port named 'q'`. That is fail-closed, but it
  means attributes cannot express "this instance's output is observation".

### Explicit wrapper/top variant: recommended, with flatten

- **Source preservation.** The original RTL is untouched. The wrapper is
  an additive file that can be reviewed in git, and its port list *is*
  the declaration of which outputs are functional.
- **Validation is free.** A wrapper that names a port the design lacks
  fails elaboration: `ERROR: Module 'obs_core' referenced in module
  'bad_wrapper' in cell 'u_core' does not have a port named 'dbg_nope'`
  (`wrapper_unknown_port_rejected` in `results.json`).
- **Hierarchy.** It needs `synth -flatten`, on both runs. C collapses to
  16 bits with functional equivalence proven, and its area and storage
  match E exactly. **B, the wrapper on today's unflattened klt flow,
  shows no collapse at all.** `obs_core` is synthesized as its own module
  with all outputs live, so the 28 bits stay. A caller running this
  control through today's `klt synthesize` would wrongly conclude that
  observability keeps nothing alive. This is the concrete gap.
- **Functional change is explicit and checkable.** Omitting a *functional*
  output from the wrapper is also a functional change, and equivalence on
  the wrapper's own outputs cannot catch it. Review the wrapper's port list
  against the design's interface. The equivalence check proves only that
  the declared functional outputs are unchanged.
- **Debug control inputs** (for example a debug-select input) can be tied
  to constants in the same wrapper. That is a visible, deliberate
  functional change. The fixture does not exercise it.

## 7. Storage-bit accounting rule

Count storage bits per cell from the resolved liberty, never from a single
cell name:

- An `ff` or `latch` group counts as one bit. An `ff_bank` or `latch_bank`
  counts as its declared width (third argument).
- Flip-flop bits and latch bits are reported separately.
- `statetable`-only cells, such as `icgtp`/`icgtn` clock gates and sky130
  `dlclkp`, are flagged and contribute 0 storage bits.
- Groups inside a `test_cell { ... }` block are **skipped**. Scan flops
  repeat their `ff` group there, and counting it reports two bits per scan
  flop. The first version of the driver did exactly that, and it was
  corrected after the classifier was cross-checked.
- Totals are rolled up through the hierarchy the same way
  `_aggregate_cell_counts` does: a submodule's counts are multiplied by its
  instance count.
- Before mapping, each fine-grained Yosys flop primitive (`$_DFF_*`,
  `$_DFFE_*` and so on) is 1 bit, and each `$_DLATCH*` is 1 latch bit.
  Coarse multi-bit cells (`$dff`, `$adff` with `WIDTH`) are not
  width-resolved from `stat` alone. The driver lists them as unaccounted
  rather than guessing. After `synth` none remained on this fixture.

**Survey of the open liberties.** `gf180mcu_fd_sc_mcu7t5v0` has 54
sequential cells: 36 one-bit flops, 12 latches and 6 `statetable` clock
gates. `sky130_fd_sc_hd` has 69: 45 flops, 18 latches and 6 clock gates.
**Neither has an `ff_bank` multi-bit cell**, so the bank-width branch of
the rule is specified but not exercised by a real cell here.

On this fixture, `dfflibmap` mapped the enable flop `$_DFFE_PN0P_` onto
`dffrnq_1` plus a `mux2`. The enable became combinational area, which is
another reason to count bits, not area. The 12 removed bits were all
`dffq_1`, but 12 of the 16 surviving bits are `dffrnq_1`. A rule that
assumed every flop is `dffq_1` would misstate both the before and after
areas.

## 8. What a before/after delta can and cannot show

**Comparable** means:

- the same Yosys build, liberty sha256, corner, ABC constraints and
  exclusions;
- the same flatten setting on both runs. D (2561.80 µm², unflattened)
  versus E (2553.02 µm², flattened) shows that flattening alone moves area
  by 8.78 µm² with identical storage;
- the same sources, byte for byte, plus only the additive wrapper;
- the same parameters and defines.

The baseline for C or E is **A2**, not A. On this fixture A and A2 happen
to have equal area, but nothing guarantees that.

**A delta can show** how many storage bits and how much area the
observation path kept alive under this toolchain. Here that is 12 bits,
763.93 µm² of sequential area (12 × 63.6608 µm² `dffq_1`), and
853.93 µm² in total.

**A delta cannot show:**

- **Which registers disappeared.** The final count drops by 12 in both a
  correct strip and a hypothetical one that removed 12 *different* bits.
  Only the pre-mapping inventory identifies them.
- **That functional behavior is intact.** H shows a plausible −4 bits
  delta from a broken design. That claim needs the equivalence check on
  declared outputs.
- **How the area divides.** The 90.00 µm² of non-sequential area is
  probe-only input logic (`acc_q ^ din`) plus ABC restructuring. The delta
  cannot divide it between "probe logic" and "mapping noise", and ABC's
  result moved by 4.4 µm² from an unrelated `write_json`.
- **Anything after synthesis.** Placement, routing, clock-tree or timing
  effects of the observation path are not measured.
- **Behavior on other designs or toolchains.** This is a 28-bit fixture on
  one Yosys build and one liberty.

## 9. Decision

- **No-go:** a general-purpose `strip observability` pre-pass driven by
  internal signal names or by attributes. Both were shown to be unsafe or
  insufficient on concrete RTL (H, G, and the selector warnings in
  section 6).
- **Go, bounded:** the explicit wrapper/top variant authored by the caller,
  measured as a flatten-matched pair. Production needs only:
  1. an opt-in flatten control, without which the control silently fails
     (B);
  2. optionally, liberty-derived storage-bit reporting so the comparison
     does not depend on cell-name conventions.
- **Fallback, if a caller cannot write a wrapper:** top-level
  output-port-name selection (D/E) with exact-match validation. It
  produced the same result as the wrapper. Its added contract surface
  should be justified by a real caller first.

## 10. Implementation plan for a separate reviewed issue

Nothing below exists today. It is a proposal for a follow-up issue that
needs its own curation.

1. **`synthesis.flatten` request field** (boolean, default `false`). When
   `true`, the generated script uses `synth -flatten -top <top>`. The
   default keeps every existing script, netlist and documented number
   byte-identical.
   - The value is echoed in the response, since it is part of what makes
     two runs comparable.
   - It is an additive field, so `schema_version` does not change
     (`docs/json-contract.md`).
   - `instance_count` must exclude `$scopeinfo`, or flattened runs gain
     non-physical cells. With Yosys 0.69 this fixture showed 2–3 extra.
2. **Optional `sequential` response block** with `flop_bits`,
   `latch_bits`, `clock_gate_cells` and `by_type`, computed with section
   7's rule from the resolved liberty. Additive.
3. **Baseline comparability.** When `baseline.response_path` names a run
   whose liberty hash, engine version or flatten setting differs, emit a
   warning or refuse. Do not report a delta silently.
4. **Docs.** Add a worked example in `docs/cli/synthesize.md` that uses
   this fixture: A2 versus C, wrapper reviewed, equivalence through the
   existing sequential-equivalence recipe.

**Test cases for that follow-up:**

- **Default unchanged:** no `synthesis.flatten` means a byte-identical
  script and the same netlist hash as today (A: `dc6db6124e81…`).
- `flatten: true` on `obs_core` (A2) gives 28 bits, and on
  `obs_core_func` (C) gives 16 bits. Mapped types are 4 × `dffq_1` and
  12 × `dffrnq_1`, with `$scopeinfo` excluded from `instance_count`.
- `flatten: false` on `obs_core_func` (B) still reports 28 bits. This pins
  the documented hierarchy caveat rather than "fixing" it implicitly.
- A non-boolean `flatten` is a request error (exit code per
  `docs/json-contract.md`).
- **Storage-bit classifier** on real liberties:
  - a gf180 scan flop (`sdffq_1`) counts 1 bit, not 2, because of the
    `test_cell` skip;
  - `icgtp_1` counts 0 bits and is flagged;
  - a latch cell counts 1 latch bit and 0 flop bits;
  - a synthetic `ff_bank (IQ, IQN, 4)` liberty counts 4 bits.
- **Baseline mismatch:** a baseline response with a different flatten
  value or liberty hash produces the warning or refusal.
- **Wrapper with a misspelled port:** an elaboration error surfaces as the
  documented synthesis failure, not a 0-delta success.

## 11. Limitations of this spike

- It covers one fixture, one Yosys build (0.69+post, macOS) and one
  gf180mcu 7t corner. CI's pinned Yosys (`scripts/install-yosys.sh`) was
  not run, and absolute areas may differ there. The ratios and the
  functional findings do not depend on the area values.
- sky130 was not measured. The driver supports
  `--cell-library sky130_fd_sc_hd`.
- The memory/ROM mapping question belongs to #2745 and the gf180-dx7
  golden regression belongs to #3069. Neither is addressed here.
- The unbounded proof relies on `equiv_induct` over output-matched
  `$equiv` cells, and the BMC is bounded at 24 cycles. Neither is a full
  reset-reachability proof. Both agreed on every variant, and both caught
  H.
