"""Local-window enclosure-band severing: geometry equivalence (#6493).

``_sever_terminal_enclosure_bands`` severs each recognised resistor body's
terminal-enclosure band against only the conductor near that body's ring,
instead of a full-layer subtraction and sizing per component. These tests
gate that optimisation against the pre-#6493 full-layer loop (verbatim as
``_reference_sever``) with an empty symmetric difference, including
non-Manhattan conductor: 45-degree routing and off-axis staircases, where
KLayout ``sized()``'s chamfered convex corners make any clip-to-window
variant diverge (#6688 review). Split from
``test_extract_gf180mcu_sab_resistor.py`` to stay under the 500-line
per-file ratchet.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import klayout.db as kdb

from klayout_tools.extract_passive_regions import _sever_terminal_enclosure_bands

_DBU_UM = 0.001
_HALO_UM = 0.22  # SB.10 / poly2.enclosing.sab.1, 220 nm
_HALO_DBU = round(_HALO_UM / _DBU_UM)

_POLY2 = (30, 0)

_BODY = (3.0, 0.0, 9.0, 1.0)


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(
        round(x0 / _DBU_UM),
        round(y0 / _DBU_UM),
        round(x1 / _DBU_UM),
        round(y1 / _DBU_UM),
    )


def _poly_um(points: list[tuple[float, float]]) -> kdb.Polygon:
    """A polygon from um-space vertices (non-Manhattan conductor fixtures)."""
    return kdb.Polygon(
        [kdb.Point(round(x / _DBU_UM), round(y / _DBU_UM)) for x, y in points]
    )


# --------------------------------------------------------------------------- #
# Local-window severing: geometry equivalence and boundary cases (#6493)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _EncSpec:
    """The one ``ResistorDevice`` field ``_sever_terminal_enclosure_bands`` reads."""

    terminal_enclosure_um: float


@dataclass(frozen=True)
class _DbuStub:
    dbu: float


def _reference_sever(
    layout: _DbuStub,
    recognised: list[tuple[_EncSpec, kdb.Region, tuple[int, int]]],
    bases: dict[tuple[int, int], kdb.Region],
) -> None:
    """The pre-#6493 full-layer loop, verbatim: the optimised helper must
    leave its output identical on every fixture below."""
    severed_by_layer: dict[tuple[int, int], kdb.Region] = {}
    for spec, body, terminal_layer in recognised:
        if spec.terminal_enclosure_um <= 0.0:
            continue
        conductor = bases[terminal_layer]
        enc_dbu = max(1, int(round(spec.terminal_enclosure_um / layout.dbu)))
        layer_severed = severed_by_layer.setdefault(terminal_layer, kdb.Region())
        for component in body.merged().each():
            ring = kdb.Region(component).sized(enc_dbu)
            band = conductor & ring
            if band.is_empty():
                continue
            outer = conductor - ring
            layer_severed += band - outer.sized(enc_dbu)
    for layer, severed in severed_by_layer.items():
        if not severed.is_empty():
            bases[layer] = bases[layer] - severed


def _windowed_sever(
    layout: _DbuStub,
    recognised: list[tuple[_EncSpec, kdb.Region, tuple[int, int]]],
    bases: dict[tuple[int, int], kdb.Region],
    slack_dbu: int,
) -> None:
    """The production algorithm with the selection window shrunk by
    ``slack_dbu``: an undersized window stands in for a wrong
    window-bound proof."""
    severed_by_layer: dict[tuple[int, int], kdb.Region] = {}
    for spec, body, terminal_layer in recognised:
        if spec.terminal_enclosure_um <= 0.0:
            continue
        conductor = bases[terminal_layer]
        enc_dbu = max(1, int(round(spec.terminal_enclosure_um / layout.dbu)))
        layer_severed = severed_by_layer.setdefault(terminal_layer, kdb.Region())
        for component in body.merged().each():
            ring = kdb.Region(component).sized(enc_dbu)
            local = conductor.interacting(ring.sized(max(0, 2 * enc_dbu - slack_dbu)))
            band = local & ring
            if band.is_empty():
                continue
            layer_severed += band - (local - ring).sized(enc_dbu)
    for layer, severed in severed_by_layer.items():
        if not severed.is_empty():
            bases[layer] = bases[layer] - severed


def _severed_by(fn, specs_bodies, conductor, **kwargs):
    bases = {_POLY2: conductor.dup()}
    fn(_DbuStub(_DBU_UM), list(specs_bodies), bases, **kwargs)
    return bases[_POLY2]


def _same_geometry(a: kdb.Region, b: kdb.Region) -> bool:
    """Region ``==`` is representation-strict; identical area is ``^`` empty."""
    return (a ^ b).is_empty()


def _assert_matches_reference(specs_bodies, conductor) -> None:
    reference = _severed_by(_reference_sever, specs_bodies, conductor)
    optimized = _severed_by(_sever_terminal_enclosure_bands, specs_bodies, conductor)
    assert _same_geometry(optimized, reference)


def _halo_only_conductor(body: kdb.Box) -> kdb.Region:
    """A floating SB.10 band: halo block, no leads -- nothing outside it."""
    return kdb.Region(body.enlarged(_HALO_DBU, _HALO_DBU)) - kdb.Region(body)


def test_windowed_severing_matches_reference_on_a_randomised_fixture() -> None:
    """AC (#6493): the local-window helper severs the identical conductor
    the full-layer reference loop does, across overlapping neighbourhoods
    (random bodies on one layer -> rings and windows overlap), conductor
    crossing window boundaries (halo blocks straddling several rings),
    entries sharing a terminal layer with different enclosures (including
    one rounding to a single DBU and one disabled), pre-cut views, and
    non-Manhattan conductor: 45-degree triangular and thin-parallelogram
    leads plus off-axis staircase leads near the bands (#6688 review --
    KLayout ``sized()`` chamfers convex corners, so its output depends on
    the polygon decomposition and only whole-polygon windows stay
    equivalent)."""
    rng = random.Random(6493)
    conductor, specs_bodies = kdb.Region(), []
    for enc_um in (0.22, 0.1, 0.0005, 0.0):
        body = kdb.Region()
        for _ in range(7):
            x, y = rng.randrange(0, 40), rng.randrange(0, 40)
            w, h = rng.randrange(2, 9), rng.randrange(2, 7)
            body.insert(_box_um(x, y, x + w, y + h))
            if rng.random() < 0.8:  # halo-ish conductor crossing the rings
                conductor.insert(_box_um(x - 0.3, y - 0.3, x + w + 0.3, y + h + 0.3))
            if rng.random() < 0.6:  # 45-degree lead leaving the band
                lx = x + rng.uniform(0, w)
                ly = rng.choice((y, y + h))
                length = rng.uniform(0.3, 2.0)
                slope = rng.choice((-1, 1))
                dx = rng.choice((-1, 1)) * length
                if rng.random() < 0.5:  # triangular
                    dy = slope * abs(dx)
                    conductor.insert(
                        _poly_um([(lx, ly), (lx + dx, ly + dy), (lx, ly + dy)])
                    )
                else:  # thin parallelogram with two 45-degree edges
                    t = rng.uniform(0.05, 0.22)
                    conductor.insert(
                        _poly_um(
                            [
                                (lx, ly),
                                (lx + dx, ly + slope * abs(dx)),
                                (lx + dx - slope * t, ly + slope * abs(dx) + t),
                                (lx - slope * t, ly + t),
                            ]
                        )
                    )
            if rng.random() < 0.5:  # off-axis staircase lead (square steps)
                cx, cy = x + rng.uniform(0, w), y + rng.uniform(0, h)
                sx, sy = rng.choice((-1, 1)), rng.choice((-1, 1))
                step, width = rng.uniform(0.15, 0.5), rng.uniform(0.08, 0.25)
                for _ in range(3):
                    cx += sx * step
                    cy += sy * step
                    half = width / 2
                    conductor.insert(
                        _box_um(cx - half, cy - half, cx + half, cy + half)
                    )
            if rng.random() < 0.6:  # stray conductor at varying distances
                sx, sy = x + rng.uniform(-3, w + 2), y + rng.uniform(-3, h + 2)
                conductor.insert(_box_um(sx, sy, sx + rng.uniform(0.05, 1.0), sy + 0.2))
        specs_bodies.append((_EncSpec(enc_um), body.merged(), _POLY2))
    for _, body, _ in specs_bodies:  # caller contract: bodies pre-removed
        conductor -= body
    conductor = conductor.merged()
    _assert_matches_reference(specs_bodies, conductor)


def test_conductor_just_outside_the_ring_influences_the_cut() -> None:
    """AC (#6493): a conductor strip 50 nm beyond the ring -- inside the
    ring.sized(enc) window, well inside ``outer.sized(enc_dbu)``'s reach --
    must still keep its nearby band alive, and an equivalence harness that
    lost it would sever the whole floating band instead."""
    body = _box_um(*_BODY)
    strip = _box_um(4.0, 1.27, 6.0, 1.35)  # ring top is y=1.22: gap 0.05um
    conductor = _halo_only_conductor(body) + kdb.Region(strip)
    specs = [(_EncSpec(_HALO_UM), kdb.Region(body), _POLY2)]
    severed = _halo_only_conductor(body) - kdb.Region(strip).sized(_HALO_DBU)
    expected = conductor - severed
    remaining = _severed_by(_sever_terminal_enclosure_bands, specs, conductor)
    assert _same_geometry(remaining, expected) and not severed.is_empty()
    # The deliberately undersized window (ring.sized(40nm) excludes the
    # strip) fails this boundary regression -- the harness is not vacuous.
    undersized = _severed_by(_windowed_sever, specs, conductor, slack_dbu=400)
    assert not _same_geometry(undersized, expected)
    assert _same_geometry(undersized, kdb.Region(strip))


def test_severing_matches_reference_on_45_degree_conductor() -> None:
    """#6688 review: 45-degree leads crossing the band must sever exactly
    as the full-layer reference loop does. The deterministic review repro
    (two haloed bodies, one 45-degree lead each): the clipping-window
    variant left a ~0.09um x 0.22um patch at a ring corner that bridged
    two conductor components the reference keeps apart -- a spurious
    short between a terminal band and neighbouring poly."""
    bodies, conductor = kdb.Region(), kdb.Region()
    fixtures = (
        (
            (2311, 2947, 5528, 4251),
            ((3922, 4251), (5143, 5472), (5245, 5472), (4024, 4251)),
        ),
        (
            (5763, 5592, 11459, 6684),
            ((7986, 6684), (9149, 7848), (9380, 7848), (8216, 6684)),
        ),
    )
    for (left, bot, right, top), lead in fixtures:
        body = kdb.Box(left, bot, right, top)
        bodies.insert(body)
        conductor.insert(
            kdb.Box(
                left - _HALO_DBU, bot - _HALO_DBU, right + _HALO_DBU, top + _HALO_DBU
            )
        )
        conductor.insert(kdb.Polygon([kdb.Point(x, y) for x, y in lead]))
    conductor = (conductor.merged() - bodies).merged()
    specs = [(_EncSpec(_HALO_UM), bodies.merged(), _POLY2)]
    reference = _severed_by(_reference_sever, specs, conductor)
    remaining = _severed_by(_sever_terminal_enclosure_bands, specs, conductor)
    assert _same_geometry(remaining, reference)
    # Connectivity semantics: the reference keeps 3 conductor components;
    # the clipping-window variant merged a band patch into a neighbour (2).
    assert reference.merged().count() == 3
    assert remaining.merged().count() == 3


def test_absent_band_is_untouched_and_a_floating_band_is_fully_severed() -> None:
    """AC (#6493): conductor beyond every window passes through unchanged,
    and a band with no outer conductor at all (no leads) is severed whole."""
    body = _box_um(*_BODY)
    specs = [(_EncSpec(_HALO_UM), kdb.Region(body), _POLY2)]
    far = kdb.Region(_box_um(20.0, 20.0, 26.0, 21.0))
    untouched = _severed_by(_sever_terminal_enclosure_bands, specs, far)
    assert _same_geometry(untouched, far)
    floating = _halo_only_conductor(body)
    assert _severed_by(_sever_terminal_enclosure_bands, specs, floating).is_empty()
