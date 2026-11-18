"""Literal Tcl words shared by OpenROAD script generators."""

_TCL_ESCAPES = {ch: "\\" + ch for ch in ' \\"$[]{};#'}
_TCL_ESCAPES.update({"\n": r"\n", "\r": r"\r", "\t": r"\t"})


def tcl_literal(text: str) -> str:
    """Quote one word, preserving ordinary names and paths byte-for-byte.

    Control characters can occur in paths or HDL names. Encode newlines as
    ``\\n``: a backslash followed by a physical newline is Tcl continuation.
    """
    return "".join(_TCL_ESCAPES.get(ch, ch) for ch in text) if text else "{}"
