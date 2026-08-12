#!/usr/bin/env python3
"""Converts Yosys `write_json` output (after `synth -run coarse:fine;
techmap; simplemap; opt -full; opt_clean`) into `klt.synth.generic-netlist/1`
(docs/schemas/synth-generic-netlist.schema.json) -- the interim on-ramp
docs/design/synth-techmap-stage-contract.md section 2.4 describes, before a
native elaboration stage exists. This converter is owned by issue #874, not
part of the contract itself.

Handles:
  - Yosys's "simple cell" set ($_NOT_/$_AND_/$_OR_/$_XOR_/$_XNOR_/$_NAND_/
    $_NOR_/$_MUX_) -> the ten-primitive vocabulary 1:1 (docs/design/
    synth-techmap-stage-contract.md section 2.2). `$_MUX_`'s own A/B/S/Y
    pins and `Y = S ? B : A` semantics already match `$mux2` exactly, so no
    translation is needed there beyond the type-name rename.
  - Every $_DFF_*_ / $_DFFE_*_ posedge-clock, reset-to-0 (or no-reset)
    variant this repo's own corpus designs use (measured empirically against
    gcd/modexp/mult8 -- see README.md), normalising Yosys's own clock/reset/
    enable *polarity* encoding (embedded in the cell-type name itself, e.g.
    `$_DFF_PN0_` = posedge clock, active-low async reset to 0) to the
    generic vocabulary's fixed active-high convention -- by inserting a
    `$not` cell in the *generic* netlist itself wherever a source polarity
    is active-low, so the emitted file always matches the contract's stated
    `$dff` semantics without asking `native/techmap`'s own mapper to guess
    a source encoding it was never given.
  - Yosys's `"0"`/`"1"` constant bit strings -> the `"$const0"`/`"$const1"`
    reserved pseudo-net names `native/techmap/src/celllib.rs`'s
    `TieCandidate` resolves -- this issue's own proposed answer to the
    contract's section 8 "constant nets" open question (an additive
    convention, not a schema field, since the schema itself only requires
    net names to be non-empty strings).

Unsupported shapes (negedge clock, reset-to-1, latches, an `x`/`z` bit, a
multi-bit connection left over from an incomplete techmap pass) raise
loudly rather than silently emitting something structurally wrong -- this
mirrors `docs/design/synth-techmap-stage-contract.md` section 2.2's "state
the gap plainly" discipline for `v1` scope.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

SIMPLE_CELL_MAP = {
    "$_NOT_": ("$not", {"A": "A", "Y": "Y"}),
    "$_BUF_": ("$buf", {"A": "A", "Y": "Y"}),
    "$_AND_": ("$and2", {"A": "A", "B": "B", "Y": "Y"}),
    "$_NAND_": ("$nand2", {"A": "A", "B": "B", "Y": "Y"}),
    "$_OR_": ("$or2", {"A": "A", "B": "B", "Y": "Y"}),
    "$_NOR_": ("$nor2", {"A": "A", "B": "B", "Y": "Y"}),
    "$_XOR_": ("$xor2", {"A": "A", "B": "B", "Y": "Y"}),
    "$_XNOR_": ("$xnor2", {"A": "A", "B": "B", "Y": "Y"}),
    "$_MUX_": ("$mux2", {"A": "A", "B": "B", "S": "S", "Y": "Y"}),
}

# Yosys's own internal simple-cell naming for D flip-flops encodes clock/
# reset/enable polarity in the type name itself:
#   $_DFF_<CP>_                    plain (no reset, no enable)
#   $_DFF_<CP><RP><RV>_            async reset
#   $_DFFE_<CP><EP>_               clock-enable, no reset
#   $_DFFE_<CP><RP><RV><EP>_       async reset + clock-enable
# CP/RP/EP in {P (active-high/posedge), N (active-low/negedge)}, RV in {0,1}.
DFF_RE = re.compile(r"^\$_DFF_([PN])_$")
DFF_RST_RE = re.compile(r"^\$_DFF_([PN])([PN])([01])_$")
DFFE_EN_RE = re.compile(r"^\$_DFFE_([PN])([PN])_$")
DFFE_RST_EN_RE = re.compile(r"^\$_DFFE_([PN])([PN])([01])([PN])_$")


class ConvertError(Exception):
    pass


class Converter:
    def __init__(self, module: dict):
        self.module = module
        self.net_names: dict = {}
        self.gen_cells: list = []
        self.fresh_counter = 0
        self.ports: list = []
        self._build_port_nets()

    def _build_port_nets(self) -> None:
        for name, info in self.module["ports"].items():
            bits = info["bits"]
            direction = info["direction"]
            n = len(bits)
            if n == 1:
                names = [name]
            else:
                # Yosys's own `bits` array is LSB-first (bits[0] == bit 0 ==
                # name[0]); the contract requires MSB-first, so build the
                # name list MSB-first and index back with `n - 1 - i`.
                names = [f"{name}[{i}]" for i in range(n - 1, -1, -1)]
            for i, b in enumerate(bits):
                self.net_names[self._bitkey(b)] = names[n - 1 - i]
            self.ports.append({"name": name, "direction": direction, "bits": names})

    @staticmethod
    def _bitkey(bit):
        # JSON round-trips ints as ints; constant bit markers are strings
        # ("0"/"1"/"x"/"z") -- keep them distinguishable as dict keys.
        return bit

    def _net(self, bit) -> str:
        if isinstance(bit, str):
            if bit == "0":
                return "$const0"
            if bit == "1":
                return "$const1"
            raise ConvertError(f"unresolved bit value '{bit}' -- not supported by v1")
        key = self._bitkey(bit)
        if key in self.net_names:
            return self.net_names[key]
        name = f"n{bit}"
        self.net_names[key] = name
        return name

    def _one_bit(self, inst_name: str, conns: dict, pin: str):
        bits = conns[pin]
        if len(bits) != 1:
            raise ConvertError(
                f"cell '{inst_name}': pin '{pin}' is {len(bits)} bits wide, "
                "expected 1 -- an incompletely-decomposed coarse cell?"
            )
        return self._net(bits[0])

    def _fresh(self, hint: str) -> str:
        self.fresh_counter += 1
        return f"_{hint}{self.fresh_counter}_"

    def _add_not(self, name_hint: str, a_net: str) -> str:
        out_net = self._fresh(f"{name_hint}_inv")
        self.gen_cells.append(
            {
                "name": self._fresh(f"{name_hint}_notgate"),
                "type": "$not",
                "connections": {"A": a_net, "Y": out_net},
            }
        )
        return out_net

    def convert_cell(self, inst_name: str, cell: dict) -> None:
        ctype = cell["type"]
        conns = cell["connections"]

        if ctype in SIMPLE_CELL_MAP:
            gtype, pinmap = SIMPLE_CELL_MAP[ctype]
            connections = {
                gpin: self._one_bit(inst_name, conns, ypin)
                for gpin, ypin in pinmap.items()
            }
            self.gen_cells.append(
                {"name": inst_name, "type": gtype, "connections": connections}
            )
            return

        m = DFF_RE.match(ctype)
        if m:
            self._emit_dff(inst_name, conns, clk_pol=m.group(1), rst=None, en=None)
            return

        m = DFF_RST_RE.match(ctype)
        if m:
            clk_pol, rst_pol, rst_val = m.groups()
            self._emit_dff(
                inst_name, conns, clk_pol=clk_pol, rst=(rst_pol, rst_val), en=None
            )
            return

        m = DFFE_EN_RE.match(ctype)
        if m:
            clk_pol, en_pol = m.groups()
            self._emit_dff(inst_name, conns, clk_pol=clk_pol, rst=None, en=en_pol)
            return

        m = DFFE_RST_EN_RE.match(ctype)
        if m:
            clk_pol, rst_pol, rst_val, en_pol = m.groups()
            self._emit_dff(
                inst_name, conns, clk_pol=clk_pol, rst=(rst_pol, rst_val), en=en_pol
            )
            return

        raise ConvertError(
            f"cell '{inst_name}' has unsupported type '{ctype}' -- outside this "
            "interim converter's supported shapes (see module docstring)"
        )

    def _emit_dff(self, inst_name: str, conns: dict, clk_pol: str, rst, en) -> None:
        if clk_pol != "P":
            raise ConvertError(f"cell '{inst_name}': negedge clock unsupported by v1")
        connections = {
            "D": self._one_bit(inst_name, conns, "D"),
            "CLK": self._one_bit(inst_name, conns, "C"),
            "Q": self._one_bit(inst_name, conns, "Q"),
        }
        if rst is not None:
            rst_pol, rst_val = rst
            if rst_val != "0":
                raise ConvertError(f"cell '{inst_name}': reset-to-1 unsupported by v1")
            rst_net = self._one_bit(inst_name, conns, "R")
            if rst_pol == "N":
                rst_net = self._add_not(inst_name, rst_net)
            connections["RST"] = rst_net
        if en is not None:
            en_net = self._one_bit(inst_name, conns, "E")
            if en == "N":
                en_net = self._add_not(inst_name, en_net)
            connections["EN"] = en_net
        self.gen_cells.append(
            {"name": inst_name, "type": "$dff", "connections": connections}
        )

    def result(self, top: str) -> dict:
        return {
            "schema": "klt.synth.generic-netlist/1",
            "top": top,
            "ports": self.ports,
            "cells": self.gen_cells,
        }


def convert(yosys_json: dict, top: str) -> dict:
    module = yosys_json["modules"][top]
    conv = Converter(module)
    # Yosys's own internal cell names are frequently not valid plain
    # Verilog identifiers (e.g. "$auto$ff.cc:266:slice$80") -- renumber
    # sequentially instead of trying to sanitise them, since the original
    # name carries no information this benchmark needs.
    for idx, (_orig_name, cell) in enumerate(module["cells"].items()):
        conv.convert_cell(f"_g{idx}_", cell)
    return conv.result(top)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "yosys_json", help="Yosys write_json output (post techmap;simplemap;opt)"
    )
    ap.add_argument("top", help="top module name")
    ap.add_argument(
        "-o", "--output", required=True, help="output klt.synth.generic-netlist/1 path"
    )
    args = ap.parse_args()

    with open(args.yosys_json, encoding="utf-8") as fh:
        data = json.load(fh)
    try:
        generic = convert(data, args.top)
    except ConvertError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(generic, fh, indent=2)
        fh.write("\n")
    print(f"wrote {args.output}: {len(generic['cells'])} generic cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
