"""Resolve standard-cell exclusions for synthesis and place-and-route."""

import re
from fnmatch import fnmatchcase

from .liberty_compare import LibertyCompareError, parse_liberty_text

#: Per-cell-library ``abc -dont_use`` glob list: the non-logic cell classes
#: a real flow keeps out of a mapped netlist. Same sourcing discipline as
#: :data:`_ABC_CONSTR_INPUTS` above -- each entry is ORFS's own
#: ``DONT_USE_CELLS`` for that platform, cross-checked against the installed
#: liberty's actual cell list (read 2026-08-12 from
#: ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @ ``master``):
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__lpflow_*`` (multi-power-domain
#:   isolation/level-shifter/decap cells) and ``sky130_fd_sc_hd__probe*``
#:   (test-probe cells with metal shapes on every layer).
#:   ``platforms/sky130hd/config.mk`` spells its ``DONT_USE_CELLS`` out as
#:   36 explicit cell names with exactly that intent (its own comment: "The
#:   *probe* are for inserting probe points and have metal shapes on all
#:   layers. *lpflow* cells are for multi-power domains"). Verified against
#:   the installed ``sky130_fd_sc_hd__tt_025C_1v80.lib``: the two globs
#:   above match **exactly** those 36 cells -- no cell ORFS excludes is
#:   missed, and no additional cell is caught (set difference empty in both
#:   directions). Globs are used rather than the 36 names because
#:   ``abc -dont_use`` supports them ("supports simple glob patterns in the
#:   cell name", ``help abc``) and because they stay correct if a future
#:   open_pdks release adds another ``lpflow_``/``probe`` cell.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> **deliberately absent**, and this is the
#:   one place this table does not simply mirror ORFS. That platform's
#:   ``DONT_USE_CELLS = *_1`` (``platforms/gf180/config.mk``) excludes every
#:   *minimum-drive* cell in the library -- 62 of the installed
#:   ``gf180mcu_fd_sc_mcu9t5v0__tt_025C_5v00.lib``'s 229 cells -- which is a
#:   drive-strength policy for P&R congestion ("Dont use cells to ease
#:   congestion", that `config.mk`'s own comment), not the "keep non-logic
#:   cells out of the netlist" exclusion this table exists for. The QoR
#:   survey section 3.2 flags exactly this asymmetry and warns that the two
#:   libraries must not be given the same treatment by analogy; measured
#:   live here rather than assumed (issue #807, 8x8 multiplier, Yosys
#:   0.68+48): applying it costs **+31% area** (9217.96 -> 12119.39 um^2)
#:   and **+12% delay** (6150.64 -> 6887.58 ps) against the same run with
#:   ``-constr`` and no exclusions. Every logic function does keep a
#:   higher-drive variant (zero functions lost entirely, checked live), so
#:   the exclusion is *safe* -- it is simply not a QoR win at this stage of
#:   the flow, and a synthesis-time default that costs a third of the area
#:   for a congestion benefit no step of this command can observe would be a
#:   guess dressed as sourcing. gf180 still gets its ``-constr`` entry
#:   above; a caller wanting ORFS's P&R-oriented exclusion can be served
#:   later by an explicit request field, measured on its own terms.
#: - ``sg13g2_stdcell`` (issue #1784) -> the five literal cell names IHP's
#:   own LibreLane platform config names as its
#:   ``SYNTH_EXCLUDED_CELL_FILE``
#:   (``libs.tech/librelane/sg13g2_stdcell/synth_exclude.cells``,
#:   IHP-Open-PDK v0.3.0): ``sg13g2_lgcp_1``, ``sg13g2_sighold``,
#:   ``sg13g2_slgcp_1``, ``sg13g2_sdfbbp_1``, ``sg13g2_dfrbp_2`` -- clock-
#:   gate/scan/sign-hold sequential cells this PDK's own flow keeps out of
#:   mapping, the same "keep non-logic/special-purpose cells out of the
#:   netlist" intent as the sky130hd entry above (not a drive-strength
#:   policy like gf180mcu's deliberately-omitted one). ``abc -dont_use``'s
#:   glob support degrades gracefully to an exact match for a pattern with
#:   no wildcard, so these plain cell names are passed through unchanged
#:   rather than turned into an invented glob.
#:
#: A ``cell_library`` with no entry gets no ``-dont_use`` flags at all --
#: this command's pre-#807 behaviour -- rather than a guessed exclusion.
ABC_DONT_USE_GLOBS: dict[str, tuple[str, ...]] = {
    "sky130_fd_sc_hd": (
        "sky130_fd_sc_hd__lpflow_*",
        "sky130_fd_sc_hd__probe*",
    ),
    "sg13g2_stdcell": (
        "sg13g2_lgcp_1",
        "sg13g2_sighold",
        "sg13g2_slgcp_1",
        "sg13g2_sdfbbp_1",
        "sg13g2_dfrbp_2",
    ),
}

#: Characters a ``constraints.dont_use`` entry may contain: Liberty cell-name
#: characters (``[A-Za-z0-9_]``) plus the glob metacharacters ``abc -dont_use``
#: itself documents ("supports simple glob patterns in the cell name") and the
#: punctuation real library cell names use. Deliberately an allowlist, not a
#: blocklist: each entry is interpolated verbatim into a generated ``.ys``
#: script's own ``abc`` line, so an entry containing whitespace or a command
#: separator would silently corrupt that line into a different Yosys command
#: rather than being rejected.
DONT_USE_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.*?!\[\]-]+$")


def liberty_cell_names(liberty_path: str) -> frozenset[str] | None:
    """Read Liberty cell names, returning None when none can be established."""
    try:
        with open(liberty_path, encoding="utf-8") as handle:
            liberty_text = handle.read()
        library = parse_liberty_text(liberty_text, source=liberty_path)
    except (OSError, UnicodeDecodeError, LibertyCompareError):
        return None
    names = frozenset(cell.args[0] for cell in library.children("cell") if cell.args)
    return names or None


def resolve_dont_use_globs(
    library_defaults: tuple[str, ...], requested: tuple[str, ...], mode: str
) -> tuple[str, ...]:
    """Combine library and request patterns according to the validated mode."""
    if mode == "none":
        return ()
    if mode == "replace":
        return requested
    return tuple(dict.fromkeys((*library_defaults, *requested)))


def first_matching_glob(cell: str, globs: tuple[str, ...]) -> str | None:
    """Return the first exclusion pattern that matches the case-sensitive cell name."""
    return next((pattern for pattern in globs if fnmatchcase(cell, pattern)), None)
