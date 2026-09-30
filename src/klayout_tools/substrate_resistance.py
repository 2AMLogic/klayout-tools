"""Two-terminal closed-form substrate spreading-resistance estimates
(issue #2561, Phase 2 of issue #2515).

``klt extract --parasitics`` models conductor R along drawn interconnect and
net-to-ground C only. Two substrate/body taps sitting on the same bulk net
are, in that model, perfectly shorted: ``_tie_substrate_nets_to_ground``
hangs one 1 Tohm DC-tie shunt off each *synthesized* substrate identity and
nothing anywhere carries a distance-dependent impedance *through silicon*
between two separate tap locations. This module supplies the missing number
for the one geometry where it can be had in closed form -- **two** contacts
-- and deliberately stops there.

Scope, stated once so it cannot be mistaken
-------------------------------------------

This is a **two-terminal closed-form estimate, not an N-terminal network
solve.** It does not close issue #2515. The general case (a resistive
network between arbitrary tap sets, with the finite substrate shared between
every terminal) needs a volumetric field solve: ``klt mom``'s PEEC
resistance path cannot supply it, because PEEC's MVP restricts every
conductor to a bar-shaped box whose current flows along one axis (see
``docs/cli/mom.md`` -> "The bar-shaped-conductor MVP restriction"), whereas
current spreading radially out of a contact into bulk has no such axis. Epic
#708 (finite-element volumetric solver) is the better-fitting future source
of those numerics.

The closed form
---------------

Take a semi-infinite half-space of resistivity ``rho``, with two circular
contacts of radii ``a1``/``a2`` on its flat surface, centre-to-centre
separation ``d``. Current ``I`` enters contact 1 and leaves contact 2.

*Self term.* A single circular disc contact injecting ``I`` into a
half-space is an equipotential at ``V = rho * I / (4 * a)`` -- Holm's
classic constriction (spreading) resistance ``R_1 = rho / (4 * a)``. It is
the ``d -> infinity`` limit of everything below, and
:func:`single_contact_spreading_resistance_ohm` returns it directly.

*Mutual term.* Far from a contact, its current distribution is
indistinguishable from a point source, whose half-space potential is
``V(r) = rho * I / (2 * pi * r)``. So contact 2's sink ``-I`` depresses
contact 1's potential by ``rho * I / (2 * pi * d)``, and contact 1's source
raises contact 2's by the same amount.

Summing::

    V1 = rho*I/(4*a1) - rho*I/(2*pi*d)
    V2 = -rho*I/(4*a2) + rho*I/(2*pi*d)

    R = (V1 - V2) / I = rho/(4*a1) + rho/(4*a2) - rho/(pi*d)

which is :func:`two_contact_spreading_resistance_ohm`. It is always strictly
positive for non-overlapping contacts (``d >= a1 + a2``): the harmonic-mean
inequality gives ``1/(4*a1) + 1/(4*a2) >= 1/(a1 + a2)``, while
``1/(pi*d) <= 1/(pi*(a1 + a2)) ~= 0.318/(a1 + a2)``.

Accuracy and where it stops being true
--------------------------------------

Two independent approximations bound the result, and both are reported per
pair rather than assumed:

1. **Point-source mutual term.** Replacing contact 1's disc current
   distribution by a point source is a multipole truncation whose leading
   error falls off as ``(a/d)**2``. :data:`FAR_FIELD_SEPARATION_RATIO`
   (``d >= 5 * (a1 + a2)``) is the documented threshold at which
   ``tests/test_substrate_resistance.py`` measures that truncation
   numerically -- by quadrature over the disc against the exact half-space
   Green's function -- and finds it under 1% of the mutual term. Closer than
   that the pair is reported with ``regime: "near_field"``: the number is
   still returned (it remains far better than the shorted-to-zero value the
   model has today), but its mutual term is no longer 1%-accurate. Closer
   still than ``d < a1 + a2`` the contacts physically overlap and no
   resistance is reported at all (``regime: "overlapping"``,
   ``resistance_ohm: null``) -- see :func:`two_contact_spreading_resistance_ohm`
   for the boundary behaviour at and below zero separation.

2. **Semi-infinite half-space.** A real wafer is finite. Once ``d``
   approaches the substrate thickness, current that the half-space model
   sends arbitrarily deep is in reality reflected by the back surface (or
   shorted by a backside contact), and neither correction is in this
   formula. Pairs with ``d`` over :data:`HALF_SPACE_DEPTH_RATIO` times the
   curated substrate thickness are flagged
   ``exceeds_half_space_depth: true``.

Provenance, not an opaque constant
----------------------------------

``rho`` is never invented here. It is read from the curated PDK stackup's
substrate entry (:func:`klayout_tools.pdk_stackup.substrate`, issue #2560)
and every report echoes ``resistivity_ohm_cm``, ``substrate_thickness_um``,
``pdk_family`` **and that entry's full ``source`` string** -- which, for
both curated families today, says in as many words that the figure is a
textbook typical value for a lightly-doped p-epi layer rather than anything
the open PDK itself states. A reviewer can therefore trace the reported ohms
back to its input and judge it, instead of trusting a number. A deck whose
family has no curated stackup reports ``resistivity_ohm_cm: null`` and no
resistances at all rather than substituting a guess.
"""

from __future__ import annotations

import math
from typing import Any

from .pdk_stackup import PdkStackupError, substrate

#: ``model`` discriminator written into every report block -- names the
#: closed form above, so a consumer can tell this estimate apart from the
#: N-terminal network solve issue #2515 still tracks (which, if it ever
#: lands, will carry a different discriminator rather than silently changing
#: what this string means).
SUBSTRATE_SPREADING_MODEL = "two_contact_half_space"

#: 1 ohm-cm is 1e4 ohm-um. The curated stackup states resistivity in
#: ohm-cm (the universal convention for silicon doping); every geometric
#: quantity in this repo is micrometres, so the formula is evaluated in
#: ohm-um throughout and converts exactly once, here.
OHM_UM_PER_OHM_CM = 1.0e4

#: ``d >= FAR_FIELD_SEPARATION_RATIO * (a1 + a2)`` is the documented
#: "far field" regime in which the point-source mutual term is accurate to
#: better than 1% -- measured, not asserted, by
#: ``test_point_source_mutual_term_is_within_one_percent_in_far_field``.
FAR_FIELD_SEPARATION_RATIO = 5.0

#: ``d > HALF_SPACE_DEPTH_RATIO * substrate_thickness_um`` flags a pair as
#: outside the semi-infinite half-space assumption. A pair separated by less
#: than half the wafer thickness keeps essentially all of its current well
#: above the back surface; beyond that the back boundary starts to matter
#: and this formula does not model it.
HALF_SPACE_DEPTH_RATIO = 0.5

#: Largest number of tap contacts one report will pair up. ``n`` taps make
#: ``n * (n - 1) / 2`` pairs, so an uncapped report on a tap-dense block
#: would emit a six-figure JSON array; 32 taps is 496 pairs, which is large
#: but still readable, and the truncation is always declared
#: (``truncated: true`` plus a warning) rather than silent.
MAX_TAPS_PER_REPORT = 32


class SubstrateSpreadingError(ValueError):
    """Raised for a request this module cannot answer at all.

    Distinct from the per-pair ``regime``/``notes`` reporting above: a
    degenerate *pair* is described in the report, whereas a degenerate
    *request* (a named net that carries no substrate-tap geometry, a
    non-positive radius) has no report to describe and is an error.
    """


def effective_contact_radius_um(area_um2: float) -> float:
    """The radius of the circular contact with the same area as a drawn tap.

    The closed form is stated for circular contacts; PDKs draw rectangles.
    Equal-area is the standard substitution (it preserves the quantity the
    spreading resistance is actually set by -- how much silicon surface the
    current has to spread out of), and for the near-square taps PDKs draw it
    is a fraction of a percent off a proper elliptic-integral treatment,
    far inside the accuracy of ``rho`` itself.
    """
    if area_um2 <= 0.0:
        raise SubstrateSpreadingError(
            f"contact area must be positive, got {area_um2} um^2"
        )
    return math.sqrt(area_um2 / math.pi)


def single_contact_spreading_resistance_ohm(
    radius_um: float, resistivity_ohm_cm: float
) -> float:
    """Holm's constriction resistance ``rho / (4 * a)`` in ohms -- one
    circular contact of radius ``radius_um`` injecting into a semi-infinite
    half-space of the given resistivity, measured against a reference
    infinitely far away.

    This is the ``separation -> infinity`` limit of
    :func:`two_contact_spreading_resistance_ohm`, and the independently-known
    analytic answer that function's validation test converges against.
    """
    if radius_um <= 0.0:
        raise SubstrateSpreadingError(
            f"contact radius must be positive, got {radius_um} um"
        )
    if resistivity_ohm_cm <= 0.0:
        raise SubstrateSpreadingError(
            f"substrate resistivity must be positive, got {resistivity_ohm_cm} ohm-cm"
        )
    return resistivity_ohm_cm * OHM_UM_PER_OHM_CM / (4.0 * radius_um)


def two_contact_spreading_resistance_ohm(
    radius_a_um: float,
    radius_b_um: float,
    separation_um: float,
    resistivity_ohm_cm: float,
) -> float:
    """``rho/(4*a1) + rho/(4*a2) - rho/(pi*d)``, in ohms -- the two-terminal
    resistance through the bulk between two circular surface contacts on a
    semi-infinite half-space (see this module's docstring for the
    derivation).

    Pure geometry and resistivity: no layout, no GDS, no KLayout. All
    lengths in micrometres, resistivity in ohm-cm, result in ohms.

    **Boundary behaviour at small separation.** ``separation_um <= 0`` and
    ``separation_um < radius_a_um + radius_b_um`` both raise
    :class:`SubstrateSpreadingError` rather than returning a number. Zero
    separation is not a limit this formula has -- the mutual term diverges,
    and two coincident contacts are one contact, whose resistance to itself
    is zero, not infinite. Overlapping (but not coincident) contacts are
    equally outside the model: the derivation assumes two disjoint
    equipotential discs. Returning *some* number for either would be worse
    than refusing, because the caller cannot tell a physical answer from an
    artefact of a divergence. Callers that must tolerate degenerate geometry
    (:func:`substrate_spreading_report` does) catch this and report
    ``resistance_ohm: null`` with a ``regime`` naming the reason.
    """
    if radius_a_um <= 0.0 or radius_b_um <= 0.0:
        raise SubstrateSpreadingError(
            f"contact radii must be positive, got {radius_a_um} um and {radius_b_um} um"
        )
    if resistivity_ohm_cm <= 0.0:
        raise SubstrateSpreadingError(
            f"substrate resistivity must be positive, got {resistivity_ohm_cm} ohm-cm"
        )
    if separation_um <= 0.0:
        raise SubstrateSpreadingError(
            "contact separation must be positive -- two coincident contacts "
            "are one contact, not a two-terminal geometry; got "
            f"{separation_um} um"
        )
    if separation_um < radius_a_um + radius_b_um:
        raise SubstrateSpreadingError(
            f"contacts overlap (separation {separation_um} um < "
            f"{radius_a_um} + {radius_b_um} um of radii): the two-disc "
            "closed form assumes two disjoint equipotential contacts"
        )
    rho_ohm_um = resistivity_ohm_cm * OHM_UM_PER_OHM_CM
    return rho_ohm_um * (
        1.0 / (4.0 * radius_a_um)
        + 1.0 / (4.0 * radius_b_um)
        - 1.0 / (math.pi * separation_um)
    )


def pair_regime(radius_a_um: float, radius_b_um: float, separation_um: float) -> str:
    """Which accuracy regime a contact pair's separation falls in.

    ``"overlapping"`` (no resistance reported), ``"near_field"`` (reported,
    but the point-source mutual term is not 1%-accurate at this spacing), or
    ``"far_field"`` (the regime the formula is validated in). See this
    module's docstring, "Accuracy and where it stops being true".
    """
    radii_sum = radius_a_um + radius_b_um
    if separation_um <= 0.0 or separation_um < radii_sum:
        return "overlapping"
    if separation_um < FAR_FIELD_SEPARATION_RATIO * radii_sum:
        return "near_field"
    return "far_field"


def resolve_substrate_model(deck_name: str) -> dict[str, Any]:
    """The resistivity/thickness inputs plus their provenance, for the PDK
    family ``deck_name`` names (issue #2560's curated stackup substrate
    entry, read through :func:`klayout_tools.pdk_stackup.substrate`).

    ``klt extract``'s deck names *are* PDK family names (``"sky130"``,
    ``"gf180mcu"``), which is exactly what ``pdk_stackup``'s variant-prefix
    matching resolves, so no separate mapping table is introduced here.

    A family with no curated stackup (``"sg13g2"`` today) is **not** an
    error and is **not** given a substituted default: the returned block
    carries ``resistivity_ohm_cm: null`` plus a ``warnings`` entry naming
    the gap, and :func:`substrate_spreading_report` then reports the tap
    geometry it found with no resistance attached. Inventing a resistivity
    for an uncurated process would produce exactly the opaque constant this
    issue's provenance requirement exists to prevent.
    """
    block: dict[str, Any] = {
        "pdk_family": None,
        "resistivity_ohm_cm": None,
        "substrate_thickness_um": None,
        "source": None,
        "warnings": [],
    }
    try:
        entry = substrate(deck_name)
    except PdkStackupError as exc:
        block["warnings"].append(
            f"no curated substrate resistivity for deck '{deck_name}': "
            f"{exc} -- substrate spreading resistance cannot be estimated "
            "for this family, and no default is substituted"
        )
        return block
    resistivity = entry.get("resistivity_ohm_cm")
    z0 = entry.get("z0_um")
    z1 = entry.get("z1_um")
    block["pdk_family"] = entry.get("family")
    block["resistivity_ohm_cm"] = resistivity
    block["source"] = entry.get("source")
    if z0 is not None and z1 is not None:
        block["substrate_thickness_um"] = float(z1) - float(z0)
    if resistivity is None:
        block["warnings"].append(
            f"curated stackup for deck '{deck_name}' states no "
            "resistivity_ohm_cm for its substrate -- substrate spreading "
            "resistance cannot be estimated, and no default is substituted"
        )
    return block


def tap_contacts_from_region(region: Any, dbu: float) -> list[dict[str, float]]:
    """One entry per connected tap-contact cluster in ``region``.

    ``region`` is a net's own geometry on the ``tap_substrate`` layer, read
    back through ``LayoutToNetlist.polygons_of_net`` -- i.e. exactly the
    substrate-tie detection ``klt extract`` already performs for the NMOS
    body terminal (issue #490's ``tap - nwell_body_cover`` split), reused
    rather than re-derived.

    Merged first: two abutting drawn tap boxes are one physical contact, and
    an unmerged region would pair them against each other at a separation
    smaller than either one's own radius. Position is the cluster's
    bounding-box centre, exact for the rectangular taps PDKs draw and a
    sound centroid proxy for the rest; radius is
    :func:`effective_contact_radius_um` of the cluster's true polygon area
    (not its bounding box, so an L-shaped ring is not inflated).

    Sorted by ``(x, y)`` so that ``taps[]`` indices -- and therefore every
    ``pairs[].a``/``pairs[].b`` reference -- are deterministic across runs
    rather than following raw ``Region`` iteration order.

    This is the only KLayout-touching function in this module, and it is
    deliberately separated from :func:`substrate_spreading_report`: the
    caller (``extract.py``) must run it while ``LayoutToNetlist`` is still
    alive, whereas the report it feeds is pure data and is built later, from
    plain dicts, where the deck name is in scope.
    """
    contacts: list[dict[str, float]] = []
    for polygon in region.merged().each():
        area_um2 = polygon.area() * dbu * dbu
        if area_um2 <= 0.0:
            continue
        bbox = polygon.bbox()
        contacts.append(
            {
                "x_um": 0.5 * (bbox.left + bbox.right) * dbu,
                "y_um": 0.5 * (bbox.bottom + bbox.top) * dbu,
                "area_um2": area_um2,
                "radius_um": effective_contact_radius_um(area_um2),
            }
        )
    contacts.sort(key=lambda c: (c["x_um"], c["y_um"]))
    return contacts


def substrate_spreading_report(
    net_name: str,
    contacts: list[dict[str, float]],
    deck_name: str,
    *,
    net_id: int | None = None,
    max_taps: int = MAX_TAPS_PER_REPORT,
) -> dict[str, Any]:
    """The ``parasitics.substrate_spreading`` report block for one net.

    ``contacts`` is that net's tap-contact list as
    :func:`tap_contacts_from_region` produced it; ``deck_name`` selects the
    curated substrate resistivity via :func:`resolve_substrate_model`. Pure
    data in, pure data out -- no KLayout object crosses this boundary.

    **More than two taps: all pairwise estimates, with the caveat stated.**
    Issue #2561's guidance allows either rejecting an over-two request or
    computing every pair; this reports every pair. Rejecting would make the
    capability useless on real geometry (a substrate tie is normally a ring
    or a row of taps, never exactly two), whereas each individual pair *is*
    a legitimate two-terminal estimate. What pairwise reporting does **not**
    give is a network: the pairs are not simultaneously realisable, because
    each is computed as though the other taps were absent, so current
    sharing between three or more taps in the same bulk is unmodelled --
    which is precisely the N-terminal case issue #2515 still tracks. The
    block therefore sets ``pairwise_approximation: true`` whenever more than
    two taps were found, so a consumer can tell the genuinely two-terminal
    answer from the caveated one without counting the array.

    Raises :class:`SubstrateSpreadingError` when ``contacts`` is empty -- a
    request naming a net with no substrate tie has no estimate to report,
    and silently returning an empty block would read as "the resistance is
    negligible" rather than "nothing was measured".
    """
    model = resolve_substrate_model(deck_name)
    warnings: list[str] = list(model["warnings"])
    contacts = list(contacts)
    if not contacts:
        raise SubstrateSpreadingError(
            f"net '{net_name}' carries no substrate-tap geometry: there is "
            "no tap contact pair to estimate a spreading resistance "
            "between. A substrate spreading estimate needs a drawn "
            "substrate tie (the deck's `tap` layer outside every nwell, or "
            "the `tap_nplus`/`tap_pplus`-derived equivalent) on this net"
        )
    taps_total = len(contacts)
    truncated = taps_total > max_taps
    if truncated:
        # Keep the largest-area taps: they carry the smallest individual
        # constriction resistance and so dominate any real tie network.
        # Re-sorted back into position order afterwards so `taps[]` indices
        # stay in the same deterministic (x, y) order as the untruncated
        # case.
        contacts = sorted(contacts, key=lambda c: -c["area_um2"])[:max_taps]
        contacts.sort(key=lambda c: (c["x_um"], c["y_um"]))
        warnings.append(
            f"net '{net_name}' has {taps_total} substrate-tap contacts; "
            f"only the {max_taps} largest are paired up "
            f"({max_taps * (max_taps - 1) // 2} pairs). Pairwise reporting "
            "is quadratic in tap count and is an approximation regardless "
            "(see parasitics.substrate_spreading.pairwise_approximation)"
        )
    resistivity = model["resistivity_ohm_cm"]
    thickness = model["substrate_thickness_um"]
    pairs: list[dict[str, Any]] = []
    for i in range(len(contacts)):
        for j in range(i + 1, len(contacts)):
            a, b = contacts[i], contacts[j]
            separation = math.hypot(a["x_um"] - b["x_um"], a["y_um"] - b["y_um"])
            regime = pair_regime(a["radius_um"], b["radius_um"], separation)
            notes: list[str] = []
            resistance: float | None = None
            if resistivity is None:
                notes.append(
                    "no curated substrate resistivity for this deck -- "
                    "geometry reported, resistance not estimated"
                )
            elif regime == "overlapping":
                notes.append(
                    "contacts overlap or coincide at this separation; the "
                    "two-disc closed form assumes two disjoint equipotential "
                    "contacts, so no resistance is reported"
                )
            else:
                resistance = two_contact_spreading_resistance_ohm(
                    a["radius_um"], b["radius_um"], separation, resistivity
                )
                if regime == "near_field":
                    notes.append(
                        "separation is under "
                        f"{FAR_FIELD_SEPARATION_RATIO:g}x the summed contact "
                        "radii: the point-source mutual term is not "
                        "1%-accurate here, so this value is an order-of-"
                        "magnitude estimate rather than a validated one"
                    )
            exceeds_depth = bool(
                thickness is not None
                and separation > HALF_SPACE_DEPTH_RATIO * float(thickness)
            )
            if exceeds_depth:
                notes.append(
                    f"separation exceeds {HALF_SPACE_DEPTH_RATIO:g}x the "
                    f"curated substrate thickness ({thickness} um): the "
                    "semi-infinite half-space assumption no longer holds "
                    "and the wafer's back surface, unmodelled here, would "
                    "change this value"
                )
            pairs.append(
                {
                    "a": i,
                    "b": j,
                    "separation_um": separation,
                    "resistance_ohm": resistance,
                    "regime": regime,
                    "exceeds_half_space_depth": exceeds_depth,
                    "notes": notes,
                }
            )
    estimated = [p["resistance_ohm"] for p in pairs if p["resistance_ohm"] is not None]
    if len(contacts) < 2:
        warnings.append(
            f"net '{net_name}' has a single substrate-tap contact: a "
            "two-terminal spreading resistance needs two, so the contact's "
            "own geometry is reported with no pair"
        )
    return {
        "model": SUBSTRATE_SPREADING_MODEL,
        "net": net_name,
        "net_id": net_id,
        "pdk_family": model["pdk_family"],
        "resistivity_ohm_cm": resistivity,
        "substrate_thickness_um": thickness,
        "source": model["source"],
        "taps": contacts,
        "tap_count": len(contacts),
        "taps_total": taps_total,
        "truncated": truncated,
        "pairs": pairs,
        "pair_count": len(pairs),
        # `true` whenever more than two taps were found -- see this
        # function's docstring: each pair is a valid two-terminal estimate,
        # the *set* of them is not a network solve (issue #2515).
        "pairwise_approximation": len(contacts) > 2,
        "min_resistance_ohm": min(estimated) if estimated else None,
        "max_resistance_ohm": max(estimated) if estimated else None,
        "warnings": warnings,
    }
