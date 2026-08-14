#!/usr/bin/env bash
# Sequential RTL-vs-gate-netlist equivalence check via plain Yosys
# (equiv_make/equiv_induct), for designs `klt equiv`'s Phase 0 scope
# rejects outright (any design with flip-flops/latches -- see
# docs/cli/equiv.md's "Scope" section and issue #707).
#
# This is the "via Yosys" arm of Epic #704 Phase 4's acceptance criterion
# ("checked equivalent to the source RTL via Yosys (or klt equiv against
# Yosys as the ground-truth oracle)") for the designs in this corpus that
# are sequential -- klt equiv itself only covers the combinational ones
# (tt_um_ALU here; see README.md's "Equivalence method per design").
#
# Uses Yosys's own equiv_make + equiv_induct k-step temporal-induction
# passes directly (the same primitives SymbiYosys's `mode equiv` expands
# to for a single/bounded-step proof -- this repo's CI has no sby, per
# docs/cli/equiv.md's own "Why plain Yosys, not SymbiYosys" note, which
# applies here too). A full formal sequential-equivalence *phase* for
# `klt equiv` itself (temporal induction / BMC via SymbiYosys, arbitrary
# depth) is out of scope here and tracked as a future phase of #707 --
# this script is a bounded, best-effort check, not that phase.
#
# `read_liberty` (full functional import, not `-lib`/blackbox) is the load-
# bearing trick: it turns each sky130 sequential std-cell's liberty `ff`
# group into a real `$_DFF_*_` primitive Yosys's SAT engine already knows
# how to model, instead of an opaque blackbox equiv_induct cannot reason
# about at all ("No SAT model available for cell ... (sky130_fd_sc_hd__dfrtp_1)").
#
# Usage:
#   seq_equiv_check.sh <workdir> <top> <gate_netlist_relpath> <seq_depth> <gold_verilog_file>...
#
# Exit 0  -- "Equivalence successfully proven!" (equiv_status -assert passed)
# Exit 1  -- either a real counterexample-shaped divergence, OR induction did
#            not converge within <seq_depth> steps (Yosys reports both as a
#            nonzero exit from `equiv_status -assert`; distinguish by reading
#            the log -- see README.md's "Reading a seq_equiv_check.sh log").
set -euo pipefail

if [ "$#" -lt 5 ]; then
  echo "usage: $0 <workdir> <top> <gate_netlist_relpath> <seq_depth> <gold_verilog_file>..." >&2
  exit 2
fi

WORKDIR=$1; shift
TOP=$1; shift
GATE=$1; shift
SEQ=$1; shift
GOLD_FILES=("$@")

LIB="${SKY130_HD_LIB:-$HOME/.volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib}"
if [ ! -f "$LIB" ]; then
  echo "error: sky130_fd_sc_hd liberty not found at '$LIB' (set SKY130_HD_LIB to override)" >&2
  exit 2
fi

cd "$WORKDIR"
SCRIPT="$(mktemp -t seq_equiv_XXXXXX.ys)"
trap 'rm -f "$SCRIPT"' EXIT

{
  echo "read_verilog ${GOLD_FILES[*]}"
  echo "hierarchy -top $TOP"
  echo "proc"
  echo "async2sync"
  echo "flatten"
  echo "rename $TOP gold"
  echo "design -stash gold"
  echo ""
  echo "read_liberty -ignore_miss_dir -ignore_miss_func $LIB"
  echo "read_verilog $GATE"
  echo "hierarchy -top $TOP"
  echo "proc"
  echo "rename $TOP gate"
  echo "flatten gate"
  echo "design -stash gate"
  echo ""
  echo "design -copy-from gold -as gold gold"
  echo "design -copy-from gate -as gate gate"
  echo ""
  echo "equiv_make gold gate equiv"
  echo "hierarchy -top equiv"
  echo "async2sync"
  echo "clean -purge"
  echo "equiv_simple"
  echo "equiv_induct -seq $SEQ equiv"
  echo "equiv_status -assert equiv"
} > "$SCRIPT"

yosys -s "$SCRIPT"
