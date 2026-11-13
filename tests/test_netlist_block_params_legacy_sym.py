"""`klt netlist --block` reads a `K`-less sibling `.sym` from `G {}` (#5468).

A third companion to test_netlist_block.py / test_netlist_block_params.py,
split off so neither crosses the per-file size ratchet. The oracle is the
same one issue #5439 established: the `--block` top-level `.subckt` card must
be the card real xschem writes for that cell when a parent instantiates it.

xschem 3.4.7's rule for a `.sym`, measured rather than read out of its
source, is that a `K` element is the symbol-attribute element whenever the
file has one -- even an empty `K {}`, in either order relative to `G` -- and
`G {...}` is the symbol-attribute element only for a `.sym` with no `K` at
all. `file_version` does not enter into it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from klayout_tools.netlist import run_netlist
from klayout_tools.netlist_block import subckt_params
from test_netlist_block_params import (  # noqa: F401
    _PIN_SYMS,
    _real_card,
)

#: The symbol attributes every cell below carries, minus the `template`.
_SYM_ATTRS = 'type=subcircuit\nformat="@name @pinlist @symname"\n'


def _sym_text(file_version: str | None, elements: str) -> str:
    """A two-port `.sym` whose top-level elements are ``elements``."""
    version = f" file_version={file_version}" if file_version else ""
    return (
        f"v {{xschem version=3.4.7{version}}}\n{elements}\n"
        "V {}\nS {}\nE {}\nB 5 -22.5 -2.5 -17.5 2.5 {name=A dir=in}\n"
        "B 5 17.5 -2.5 22.5 2.5 {name=B dir=out}\n"
    )


def _write_cell(tmp_path: Path, cell: str, sym_elements: str | None, **kwargs) -> str:
    """A two-pin cell `.sch` plus, when ``sym_elements`` is given, a sibling
    `.sym` whose top-level elements are exactly that. Returns the `.sch`."""
    for name, k_block in _PIN_SYMS.items():
        (tmp_path / name).write_text(
            f"v {{xschem version=3.4.7 file_version=1.2}}\nG {{}}\n{k_block}\n"
            "V {}\nS {}\nE {}\nB 5 -1.25 -1.25 1.25 1.25 {name=p dir=inout}\n",
            encoding="utf-8",
        )
    (tmp_path / f"{cell}.sch").write_text(
        "v {xschem version=3.4.7 file_version=1.2}\nG {}\n"
        f'K {{{_SYM_ATTRS}template="name=x1"}}\n'
        "V {}\nS {}\nE {}\nN 0 0 0 40 {lab=A}\nN 0 80 0 120 {lab=B}\n"
        "C {pin_in.sym} 0 0 0 0 {name=p1 lab=A}\n"
        "C {pin_out.sym} 0 120 0 0 {name=p2 lab=B}\n",
        encoding="utf-8",
    )
    if sym_elements is not None:
        (tmp_path / f"{cell}.sym").write_text(
            _sym_text(kwargs.get("file_version", "1.2"), sym_elements),
            encoding="utf-8",
        )
    return str(tmp_path / f"{cell}.sch")


def _parent(tmp_path: Path, cell: str, instance: str) -> str:
    parent = tmp_path / f"parent_{cell}.sch"
    parent.write_text(
        "v {xschem version=3.4.7 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
        f"C {{{instance}}} 200 0 0 0 {{name=x1}}\n",
        encoding="utf-8",
    )
    return str(parent)


def _rcfile(tmp_path: Path) -> str:
    path = tmp_path / "xschemrc"
    path.write_text(f"set XSCHEM_LIBRARY_PATH {tmp_path}\n", encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# subckt_params() over every row of the measured table
# --------------------------------------------------------------------------- #

_TEMPLATE = 'template="name=x1 w=1 l=0.15"'
_G_ONLY = f"G {{{_SYM_ATTRS}{_TEMPLATE}}}"
_K_ONLY = f"K {{{_SYM_ATTRS}{_TEMPLATE}}}"
_G_FROMG = f'G {{{_SYM_ATTRS}template="name=x1 fromg=1"}}'
_K_FROMK = f'K {{{_SYM_ATTRS}template="name=x1 fromk=2"}}'


@pytest.mark.parametrize(
    "file_version,elements,expected",
    [
        ("1.1", _G_ONLY, "w=1 l=0.15"),
        (None, _G_ONLY, "w=1 l=0.15"),
        ("1.2", _G_ONLY, "w=1 l=0.15"),
        ("1.2", _K_ONLY, "w=1 l=0.15"),
        ("1.2", f"{_G_FROMG}\n{_K_FROMK}", "fromk=2"),
        ("1.1", f"{_G_FROMG}\n{_K_FROMK}", "fromk=2"),
        ("1.2", f"{_K_FROMK}\n{_G_FROMG}", "fromk=2"),
        ("1.2", f"{_G_ONLY}\nK {{}}", ""),
    ],
    ids=[
        "g-only-1.1",
        "g-only-no-version",
        "g-only-1.2",
        "k-only-1.2",
        "g-then-k-1.2",
        "g-then-k-1.1",
        "k-then-g-1.2",
        "g-then-empty-k",
    ],
)
def test_subckt_params_follows_xschems_k_wins_g_fallback_rule(
    tmp_path, file_version, elements, expected
):
    """Every row of the table measured against xschem 3.4.7 for issue #5468:
    `K` wins when the `.sym` has one, `G` is read only when it has none, and
    `file_version` never changes the answer."""
    schematic = _write_cell(tmp_path, "cell", elements, file_version=file_version)

    assert subckt_params(schematic) == (expected, None)


def test_a_schematics_own_g_element_is_never_a_parameter_source(tmp_path):
    """With no sibling `.sym` the fallback is off: xschem reads no `template`
    from a `.sch`'s `G {}` (measured), so neither does klt."""
    schematic = tmp_path / "cell.sch"
    schematic.write_text(
        f"v {{xschem version=3.4.7 file_version=1.2}}\n{_G_ONLY}\n"
        "V {}\nS {}\nE {}\n",
        encoding="utf-8",
    )

    assert subckt_params(str(schematic)) == ("", None)


@pytest.mark.parametrize("file_version", ["1.2", "1.1"])
def test_a_schematics_own_g_is_not_read_even_without_a_k_element(
    tmp_path, file_version
):
    """The `.sch` has no `K` either -- still no parameters, at either
    `file_version`."""
    schematic = tmp_path / "cell.sch"
    version = f" file_version={file_version}"
    schematic.write_text(
        f"v {{xschem version=3.4.7{version}}}\n{_G_ONLY}\nV {{}}\nS {{}}\nE {{}}\n",
        encoding="utf-8",
    )

    assert subckt_params(str(schematic)) == ("", None)


def test_an_unterminated_g_in_a_k_less_sym_warns_and_names_g(tmp_path):
    """Not a silent empty result: the existing unterminated-element warning
    fires and names the element it actually found unterminated."""
    schematic = _write_cell(tmp_path, "cell", None)
    (tmp_path / "cell.sym").write_text(
        f"v {{xschem version=3.4.7 file_version=1.1}}\nG {{{_SYM_ATTRS}{_TEMPLATE}\n",
        encoding="utf-8",
    )

    params, warning = subckt_params(schematic)

    assert params == ""
    assert "unterminated 'G {...}'" in warning
    assert "cell.sym" in warning


def test_an_unterminated_k_still_names_k(tmp_path):
    """The warning names the element, so a `K`-carrying `.sym` is unchanged."""
    schematic = _write_cell(tmp_path, "cell", None)
    (tmp_path / "cell.sym").write_text(
        f"v {{xschem version=3.4.7 file_version=1.2}}\nK {{{_SYM_ATTRS}{_TEMPLATE}\n",
        encoding="utf-8",
    )

    _, warning = subckt_params(schematic)

    assert "unterminated 'K {...}'" in warning


def test_the_g_fallback_reads_the_template_only_not_bare_assignments(tmp_path):
    """Reading a `K`-less `.sym` from `G {}` picks up its `template` and
    nothing else: bare `key=value` text in `G {}` is not a parameter source
    for any cell (issue #5439's rejected variant)."""
    schematic = _write_cell(tmp_path, "cell", f"G {{{_SYM_ATTRS}bare=9\n{_TEMPLATE}}}")

    assert subckt_params(schematic) == ("w=1 l=0.15", None)


# --------------------------------------------------------------------------- #
# real xschem: the block card is the card xschem writes for the same cell
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
@pytest.mark.parametrize(
    "file_version", ["1.1", None, "1.2"], ids=["1.1", "no-version", "1.2"]
)
def test_block_card_matches_xschem_for_a_k_less_sibling_symbol(tmp_path, file_version):
    """The oracle, against real xschem: for a cell whose sibling `.sym` has
    no `K` element and carries its `template` in `G {}`, the `--block` card
    must be the card xschem writes for the same cell as a child."""
    schematic = _write_cell(tmp_path, "cell", _G_ONLY, file_version=file_version)
    parent = _parent(tmp_path, "cell", "cell.sym")
    rcfile = _rcfile(tmp_path)

    run_netlist(
        schematic,
        str(tmp_path / "block.spice"),
        rcfile=rcfile,
        block=True,
        provenance_header=False,
    )
    run_netlist(
        parent,
        str(tmp_path / "parent.spice"),
        rcfile=rcfile,
        provenance_header=False,
    )

    block_card = _real_card(str(tmp_path / "block.spice"), "cell")
    child_card = _real_card(str(tmp_path / "parent.spice"), "cell")
    assert block_card == child_card
    # ...and it is not the parameter-less card the defect produced.
    assert block_card == [".subckt", "cell", "A", "B", "w=1", "l=0.15"]


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
def test_an_empty_k_still_means_no_parameters_against_real_xschem(tmp_path):
    """The `K`-wins half of the rule, live: `G {}` carrying the template plus
    an empty `K {}` restores no parameters.

    There is no child card to compare against here -- an empty `K {}` carries
    no `type=subcircuit`, so xschem does not treat the cell as a subcircuit
    at all: the parent instantiates nothing and emits no `.subckt cell` card
    (measured, xschem 3.4.7). Both halves are asserted, because "xschem
    restores nothing either" is what makes restoring nothing correct.
    """
    schematic = _write_cell(tmp_path, "cell", f"{_G_ONLY}\nK {{}}")
    parent = _parent(tmp_path, "cell", "cell.sym")
    rcfile = _rcfile(tmp_path)

    run_netlist(
        schematic,
        str(tmp_path / "block.spice"),
        rcfile=rcfile,
        block=True,
        provenance_header=False,
    )
    run_netlist(
        parent, str(tmp_path / "parent.spice"), rcfile=rcfile, provenance_header=False
    )

    block_card = _real_card(str(tmp_path / "block.spice"), "cell")
    assert [token for token in block_card if "=" in token] == []
    assert ".subckt cell" not in Path(tmp_path / "parent.spice").read_text(
        encoding="utf-8"
    )
