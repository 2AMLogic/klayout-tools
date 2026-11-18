"""ngspice's per-contributor noise vector namespace (issue #5553).

With a trailing ``pts_per_summary`` argument on the analysis line, ngspice's
noise spectra plot (``noise1``) carries one vector per *noise generator* as
well as the ``onoise_spectrum`` total. This module is the reader for that
namespace: which vectors are contributors, which of those are leaves, and
which physical mechanism each leaf belongs to. It knows nothing about
``measurements[]`` -- :mod:`klayout_tools.sim_noise_mechanism` owns the form.

Shape of the namespace (measured, ngspice-47)
---------------------------------------------
Each device contributes a **device-total** vector plus its **leaf** generator
vectors, so summing every vector double-counts. On sky130
(``sky130_fd_pr__nfet_01v8``, BSIM4, reached through its PDK subcircuit)::

    onoise.m.xm1.msky130_fd_pr__nfet_01v8            <- device total
    onoise.m.xm1.msky130_fd_pr__nfet_01v8.1overf     <- leaf (flicker)
    onoise.m.xm1.msky130_fd_pr__nfet_01v8.id         <- leaf (channel thermal)
    onoise.m.xm1.msky130_fd_pr__nfet_01v8.rd .rs .rg <- leaf (terminal thermal)
    onoise.m.xm1.msky130_fd_pr__nfet_01v8.rbdb .rbpb .rbpd .rbps .rbsb
    onoise.m.xm1.msky130_fd_pr__nfet_01v8.igb .igd .igs  <- leaf (gate leakage)
    onoise_rbig                                      <- device total
    onoise_rbig_1overf  onoise_rbig_thermal          <- leaf

**The separator ngspice puts between the parts is not fixed.** The sky130
device above uses ``.`` throughout, while a flat level-1 MOSFET in the same
plot uses ``_`` (``onoise_m1``, ``onoise_m1_1overf``, ``onoise_m1_id``), and a
subcircuit instance mixes them (``onoise_m.x1.mi_1overf``). PDK model and
instance names contain both characters, so nothing here ever *splits* a vector
name. Each leaf is instead matched to its own device-total vector -- the
longest other contributor it extends across a separator -- and the generator
suffix is whatever remains. That is separator-agnostic by construction, and
the separator boundary is also what keeps devices ``r1`` and ``r10`` apart.

Classification
--------------
``flicker`` is any suffix beginning ``1overf`` (so a diode's sidewall
``1overfsw`` counts). ``thermal`` is an explicit table of known thermal
generators scoped by **device class** -- the letter that opens ngspice's
flattened device name -- which is what keeps a diode's ``id`` (junction shot
noise) out of the thermal share even though a MOSFET's ``id`` (channel thermal
noise) spells the same. Everything else is **unclassified**: it belongs to
neither mechanism, and the caller's completeness assertion surfaces it rather
than folding it into either number. Gate leakage (``ig*``) and junction
currents (diode ``id``/``idsw``, BJT ``ib``/``ic``) are shot noise and stay
unclassified deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The ``noise.mechanism`` values.
NOISE_MECHANISMS = ("thermal", "flicker")

#: The output-referred spectrum every contributor vector shares a prefix with.
PREFIX = "onoise"

#: Separators ngspice may put between the prefix, the flattened device name
#: and the generator suffix. Which one it picks varies with the device name
#: (see the module docstring), so both are accepted everywhere.
SEPARATORS = "._"

#: Aggregate vectors of the spectra plot: never contributors.
_AGGREGATE_VECTORS = frozenset({"onoise_spectrum", "onoise_total"})

#: Any generator suffix starting with this is flicker (``1overf``, and a
#: diode's sidewall ``1overfsw``).
_FLICKER_SUFFIX_PREFIX = "1overf"

#: Known *thermal* generator suffixes per device class -- the letter that
#: opens ngspice's flattened device name (``m`` MOSFET, ``r`` resistor, ``d``
#: diode, ``q`` BJT). Scoped by class on purpose: a diode's ``id`` is junction
#: shot noise while a MOSFET's ``id`` is channel thermal noise, and an
#: unscoped suffix table would silently book the former as thermal.
#:
#: Measured on ngspice-47: sky130 BSIM4 (``sky130_fd_pr__nfet_01v8``) emits
#: ``id rd rs rg rbdb rbpb rbpd rbps rbsb`` plus ``1overf`` and the gate-
#: leakage triple ``igb igd igs``; a level-1 MOSFET emits ``id rd rs``;
#: resistors emit ``thermal``/``1overf``; diodes ``rs rsw id idsw 1overf
#: 1overfsw``; BJTs ``rb rc re ib ic 1overf``.
THERMAL_SUFFIXES: dict[str, frozenset[str]] = {
    # MOSFET: channel thermal + the terminal/body parasitic resistances.
    "m": frozenset(
        {"id", "rd", "rs", "rg", "rbdb", "rbpb", "rbpd", "rbps", "rbsb", "rbody"}
    ),
    # Resistor: its own Johnson noise.
    "r": frozenset({"thermal"}),
    # Diode: series resistance only -- `id`/`idsw` are junction shot noise.
    "d": frozenset({"rs", "rsw"}),
    # BJT: base/collector/emitter resistances -- `ib`/`ic` are shot noise.
    "q": frozenset({"rb", "rc", "re"}),
}


@dataclass(frozen=True)
class Leaf:
    """One generator vector: the finest-grained contributor ngspice emits."""

    name: str
    #: ngspice's device-class letter (``m``/``r``/``d``/``q``).
    device_class: str
    #: The generator suffix (``1overf``, ``id``, ``thermal``, ...), or None
    #: when the leaf has no device-total vector to measure it against.
    suffix: str | None
    #: ``"thermal"``, ``"flicker"`` or None (unclassified).
    mechanism: str | None

    @property
    def label(self) -> str:
        """What a completeness diagnostic names this leaf by."""
        return self.suffix if self.suffix is not None else self.name


def contributor_names(names: list[str]) -> list[str]:
    """The output-referred per-contributor vectors among ``names`` -- device
    totals and leaves both, with the plot's own aggregates excluded."""
    return [
        name
        for name in names
        if name not in _AGGREGATE_VECTORS
        and name.startswith(PREFIX)
        and len(name) > len(PREFIX) + 1
        and name[len(PREFIX)] in SEPARATORS
    ]


def _extends(name: str, prefix: str) -> bool:
    """Whether ``name`` is ``prefix`` plus a separator plus something else.

    The separator boundary is what makes this safe: devices ``r1`` and ``r10``
    give ``onoise_r1`` and ``onoise_r10``, where the extension character is
    ``0`` -- so ``onoise_r1`` is not mistaken for ``onoise_r10``'s device
    total. Likewise a diode's ``1overf`` is not swallowed by its ``1overfsw``.
    """
    return (
        len(name) > len(prefix) + 1
        and name.startswith(prefix)
        and name[len(prefix)] in SEPARATORS
    )


def _device_total_of(leaf: str, contributors: list[str]) -> str | None:
    """``leaf``'s own device-total vector: the longest other contributor that
    ``leaf`` extends. None when the plot carries no total for it."""
    best: str | None = None
    for other in contributors:
        if _extends(leaf, other) and (best is None or len(other) > len(best)):
            best = other
    return best


def classify_mechanism(device_class: str, suffix: str | None) -> str | None:
    """``"thermal"``, ``"flicker"`` or None (unclassified) for one generator
    suffix of one device class. See the module docstring for why the thermal
    table is scoped by class and flicker is not."""
    if suffix is None:
        return None
    if suffix.startswith(_FLICKER_SUFFIX_PREFIX):
        return "flicker"
    if suffix in THERMAL_SUFFIXES.get(device_class, frozenset()):
        return "thermal"
    return None


def classify_leaves(names: list[str]) -> list[Leaf]:
    """Every leaf contributor among ``names``, classified.

    A contributor is a *device total* when another contributor extends its
    name across a separator, and a *leaf* otherwise -- so the double-counting
    the issue describes (summing ``onoise_m1`` together with its own
    ``onoise_m1_id``) cannot happen. Device totals are dropped here; only
    leaves carry a mechanism.
    """
    contributors = contributor_names(names)
    leaves: list[Leaf] = []
    for name in contributors:
        if any(_extends(other, name) for other in contributors):
            continue  # a device total, not a leaf
        parent = _device_total_of(name, contributors)
        suffix = name[len(parent) + 1 :] if parent is not None else None
        device_class = name[len(PREFIX) + 1].lower()
        leaves.append(
            Leaf(
                name=name,
                device_class=device_class,
                suffix=suffix,
                mechanism=classify_mechanism(device_class, suffix),
            )
        )
    return leaves
