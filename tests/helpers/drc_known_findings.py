"""The DRC findings `klt gen`'s own sky130 output is known to carry, and the
single assertion the generator tests that would otherwise demand an
unconditional `status == "clean"` use instead.

Why this exists. Issue #2642 transcribed sky130's own on-by-default `OFFGRID`
rule group into the curated DRC deck (`<layer>.ongrid.1`, a 0.005um
manufacturing grid, official rule `x.1b`). That immediately surfaced a real,
pre-existing defect in `klt gen`: **it does not snap derived coordinates to the
PDK's manufacturing grid.** Two observed faces of the one defect:

- gate-contact `licon1` cuts, centred on an unsnapped midpoint of the poly comb
  (`gen.py`'s `_mos_unit_layout`) -- the cut size is on-grid, the position is
  not. Affects `diff_pair`, `esd_device`, and `mos_array`/`res_array` under
  their gate-contact and guard-ring parametrizations;
- `li1`/`poly` tie geometry when a parameter resolves to a half-database-unit
  length (`tests/test_gen.py::test_res_array_half_dbu_tie_length_...`, which
  exercises exactly that rounding on purpose).

Tracked as **issue #2648**, with the reproducer, the per-generator survey and
the suspected call sites.

So the generator tests that assert "this generator's output is DRC clean"
cannot say `status == "clean"` until #2648 lands, but they must not be loosened
into saying nothing either. :func:`assert_drc_clean_except_known_gen_offgrid`
is the narrow middle: it permits findings from the **enumerated** rule ids in
:data:`KNOWN_OFFGRID_GEN_RULES` and fails on anything else -- including an
`ongrid` rule on a *fourth* layer, so further grid drift is still caught, and
including any ordinary width/space/enclosure/area regression, which is what
these tests were written for.

It deliberately does **not** require a known finding to be present: several
call sites are parametrized across generators and decks where only some
combinations draw a gate contact at all, and a presence requirement there would
fail the combinations that are genuinely clean. The "this allowance must not
outlive the fix" tripwire is one dedicated test instead --
`tests/test_gen.py::test_known_offgrid_licon1_defect_is_still_present` -- which
starts failing the moment #2648 lands, naming these call sites as the thing to
delete.

Not a `pytest.xfail`: these tests assert many other things besides the DRC
verdict, and an xfail would stop checking all of them.
"""

from __future__ import annotations

from typing import Any

#: The rule id every affected call site trips, and the one the tripwire test
#: watches: "licon1 vertices must be on the 0.005um manufacturing grid",
#: transcribed from `sky130.lydrc`'s `licon.ongrid(0.005)` (`x.1b`).
KNOWN_OFFGRID_LICON1_RULE = "licon1.ongrid.1"

#: Every rule id `klt gen`'s sky130 output is known to trip, all of them the
#: same unsnapped-coordinate defect (issue #2648) on a different layer. Keep
#: this list exact: widening it to "any `ongrid` rule" would stop the tests
#: noticing a new layer drifting off-grid.
#:
#: Re-measured empirically against the post-#2594 deck and generators (that
#: change clamps `licon1`/`mcon` cuts to their fixed 0.17um size about each
#: cut's own centre, and adds `licon1.width.1`/`mcon.width.1` to the deck):
#: the observed set is unchanged. `mcon` deliberately has **no** entry -- it
#: is drawn on-grid -- and neither new `width` rule fires on any generator
#: output, so this set is still exactly the three ids below.
KNOWN_OFFGRID_GEN_RULES = frozenset(
    {KNOWN_OFFGRID_LICON1_RULE, "li1.ongrid.1", "poly.ongrid.1"}
)

#: The issue whose fix removes the need for this helper.
KNOWN_OFFGRID_GEN_ISSUE = 2648


def assert_drc_clean_except_known_gen_offgrid(report: dict[str, Any]) -> None:
    """Assert ``report`` (a ``run_drc`` envelope over `klt gen` output) carries
    no violation outside :data:`KNOWN_OFFGRID_GEN_RULES`.

    Equivalent to the ``status == "clean"`` assertion it replaces for every
    defect class except those -- see this module's docstring.
    """
    offending = sorted(
        {
            violation["rule"]
            for violation in report["violations"]
            if violation["rule"] not in KNOWN_OFFGRID_GEN_RULES
        }
    )
    assert not offending, (
        "unexpected DRC violations beyond the known unsnapped-coordinate "
        f"findings (issue #{KNOWN_OFFGRID_GEN_ISSUE}): {offending}; "
        f"full list: {report['violations']}"
    )
