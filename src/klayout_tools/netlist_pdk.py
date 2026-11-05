"""xschemrc PDK-root and symbol-path env helpers for ``netlist`` (size split)."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

_RC_PDK_ROOT_RE = re.compile(
    r"^\s*set\s+(?:::)?(?:env\(PDK_ROOT\)|PDK_ROOT)\s+(\"[^\"]*\"|\{[^}]*\}|\S+)",
    re.MULTILINE,
)


def _rcfile_pdk_root(rcfile: str) -> str | None:
    """The PDK root a ``xschemrc`` statically declares, or ``None``.

    Recognises ``set PDK_ROOT <path>`` and ``set env(PDK_ROOT) <path>`` (the
    last such assignment wins). A value that is computed (contains ``$`` or
    ``[``) cannot be evaluated without a Tcl interpreter and is reported as
    undeclared rather than guessed at.
    """
    try:
        with open(rcfile, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    found = _RC_PDK_ROOT_RE.findall(text)
    if not found:
        return None
    value = found[-1].strip()
    if value[:1] in ('"', "{") and value[-1:] in ('"', "}"):
        value = value[1:-1]
    if not value or "$" in value or "[" in value:
        return None
    return os.path.realpath(os.path.expanduser(value))


def _pdk_root_consistent(declared: str, root: str, variant: str) -> bool:
    """True if a declared ``xschemrc`` PDK root agrees with the resolved PDK.

    Consistent when ``declared`` equals the install ``root`` itself (the
    ``$PDK_ROOT`` convention), or equals/sits under the resolved variant
    directory ``root/variant`` (the ``$PDK_ROOT/sky130A`` convention).
    Deliberately *not* consistent when ``declared`` merely sits above
    ``root`` (too permissive to mean anything, e.g. ``$PDK_ROOT=/home/me``)
    or names a different variant under the same install root (e.g. declares
    ``sky130A`` while the resolved PDK is ``sky130B``).
    """
    declared = os.path.realpath(declared)
    root = os.path.realpath(root)
    if declared == root:
        return True
    variant_dir = os.path.realpath(os.path.join(root, variant))
    return declared == variant_dir or declared.startswith(variant_dir + os.sep)


def _with_symbol_path_env(
    xschem_env: dict[str, str] | None, resolved: Sequence[str]
) -> dict[str, str] | None:
    """Prepend ``resolved`` dirs to ``XSCHEM_USER_LIBRARY_PATH`` (issue #4952).

    Explicit ``--symbol-path`` dirs go first, ahead of any value the caller
    already exported. Returns ``xschem_env`` unchanged when ``resolved`` is empty.
    """
    if not resolved:
        return xschem_env
    env = dict(os.environ) if xschem_env is None else xschem_env
    existing = env.get("XSCHEM_USER_LIBRARY_PATH")
    env["XSCHEM_USER_LIBRARY_PATH"] = os.pathsep.join(
        [*resolved, *([existing] if existing else [])]
    )
    return env
