"""Wire-load evidence from one linked, constraint-configured Verilog session."""

from __future__ import annotations

import re

# The marker comes after the user's setters. A rejected model or mode stops the
# Tcl script before this point. Query the process doing the timing itself.
SESSION_MARKER_TCL = (
    "if {![catch {ord::openroad_version} klt_wire_build]} "
    '{puts "KLT_WIRE_LOAD_SESSION $klt_wire_build"}'
)

# OpenROAD 6cb3f2b704 embeds OpenSTA 509913b. Sdc starts with null model
# slots and an unknown mode. The delay calculator consults those slots, not
# LibertyLibrary::defaultWireload(). A different build needs its own evidence.
_VERIFIED_UNSET_BUILD = "26Q3-1510-g6cb3f2b704"


def corner_fields(
    stdout: str, model: str | None, mode: str | None, *, has_def: bool
) -> dict[str, dict[str, str | None]]:
    """Record the effective wire-load settings of a Verilog timing session."""
    if has_def:
        return {}
    marker = re.search(r"^KLT_WIRE_LOAD_SESSION ([^\s]+)[ \t]*$", stdout, re.MULTILINE)
    if marker is None:
        model = mode = None
    unset_source = (
        "none" if marker and marker[1] == _VERIFIED_UNSET_BUILD else "unknown"
    )
    return {
        "wire_load": {
            "model": model,
            "model_source": "request" if model is not None else unset_source,
            "mode": mode,
            "mode_source": "request" if mode is not None else unset_source,
        }
    }
