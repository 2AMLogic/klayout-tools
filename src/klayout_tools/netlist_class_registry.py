"""The one-class-object-per-name registry the SPICE-reader recovery paths share.

Moved verbatim out of ``netlist_capacitor_recovery.py`` (file-size ratchet,
issue #6726) when :mod:`.netlist_inductor_recovery` became its second user;
``netlist_capacitor_recovery`` imports it back under its old private name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def shared_device_class(
    netlist: kdb.Netlist,
    class_name: str,
    make_class: Callable[[], kdb.DeviceClass],
    registry: dict[str, kdb.DeviceClass],
) -> kdb.DeviceClass:
    """The one ``kdb.DeviceClass`` object every recovery of ``class_name``
    in one read must share -- looked up in ``registry`` first, then in
    ``netlist``'s registered classes by exact name, and only then built via
    ``make_class`` and registered (issue #1157, PR #2336 CI regression).

    Why not ``Netlist.device_class_by_name``: that lookup misses a class
    this recovery registered earlier in the same read twice over (both
    verified against ``klayout==0.30.10``). First, it does not see a class
    ``netlist.add()``-ed from inside a ``NetlistSpiceReaderDelegate
    .element()`` callback for the rest of that read, even though
    ``each_device_class()`` lists it immediately. Second, even after the
    read completes it normalizes its argument through the netlist's own
    case convention (SPICE netlists read case-insensitively, so it
    *uppercases*) but compares against each registered class's stored name
    verbatim -- a deck's canonical lowercase ``res_high_po`` never matches.
    Either miss alone sends a naive create-and-``add`` fallback down the
    per-card path: one fresh class object *per recovered card*.
    ``Netlist.combine_devices()`` groups devices by ``DeviceClass`` object
    identity (``tl::id_of`` of the device's class against the class being
    combined for), so a per-card class object means no two devices ever
    group and the series fold silently never happens (PR #2336: three
    sky130 ``res_high_po`` segments survived as three devices on Linux CI).
    One class object per name -- what this helper guarantees -- is also
    what makes the fold robust against the klayout-side detail that
    ``DeviceClass``'s copy constructor copies an indeterminate
    ``tl::UniqueId`` (verified against ``klayout==0.30.10``: a
    ``Netlist.dup()`` clone's ``id()`` is 0 on macOS but distinct garbage
    on Linux), so the fold no longer depends on which platform's heap
    happens to make the clone ids coincide.

    ``registry`` is the per-reader cache the caller owns (one
    ``make_capacitor_class_recovery_reader`` build serves exactly one
    ``Netlist.read`` call -- every caller in this repo does); the exact-name
    scan covers a class the *default* reader handler already registered
    under the same verbatim name earlier in the same read.
    """
    device_class = registry.get(class_name)
    if device_class is not None:
        return device_class
    for registered in netlist.each_device_class():
        if registered.name == class_name:
            registry[class_name] = registered
            return registered
    device_class = make_class()
    netlist.add(device_class)
    registry[class_name] = device_class
    return device_class
