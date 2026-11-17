"""xschemrc PDK-root and symbol-path env helpers for ``netlist`` (size split)."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

from . import pdk as pdk_module

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


def _pdk_env_note(rcfile: str | None, exported_env: dict[str, str] | None) -> str:
    """Name what klt can observe about xschem's PDK setup, for error text.

    ``exported_env`` is the env klt built *because it resolved a PDK*, and is
    therefore guaranteed to carry ``PDK_ROOT``/``PDK``. Pass ``None`` whenever
    klt did not resolve one — including when it built an env for another
    reason, such as ``--symbol-path`` (issue #5441) — so the note reports what
    xschem actually inherited instead of claiming klt exported it.
    """
    if exported_env is not None:
        seen = (
            f"PDK_ROOT={exported_env['PDK_ROOT']} PDK={exported_env['PDK']} "
            "(exported by klt)"
        )
    else:
        root = os.environ.get("PDK_ROOT")
        variant = os.environ.get("PDK")
        seen = (
            f"PDK_ROOT={root or 'not set'} PDK={variant or 'not set'} "
            "(inherited, not set by klt)"
        )
    return f"xschem ran with --rcfile {rcfile or 'none'} and {seen}. "


#: ``payload.warnings`` code for a run whose PDK variant was defaulted, not
#: selected (issue #5639): the generation-inputs record carries
#: ``pdk_variant: null`` rather than asserting the first-match guess as fact.
PDK_VARIANT_UNESTABLISHED_CODE = "pdk_variant_unestablished"

#: The prefix :func:`klayout_tools.pdk.alternative_variants_warning` puts on
#: its text. Stripped for the entry's ``message`` so the CLI's ``_warn``
#: channel -- which adds its own ``klt netlist: warning:`` -- does not double
#: it.
_ALT_WARNING_PREFIX = "klt: warning: "


def unestablished_variant_warning(report: dict[str, Any]) -> dict[str, Any] | None:
    """The ``pdk_variant_unestablished`` warnings entry for a
    :func:`klayout_tools.pdk.find_pdk` report whose variant was defaulted,
    or ``None`` when it was established.

    Reuses #3814's ``alternative_variants`` signal, which is non-empty
    exactly when no variant was explicitly selected (no ``--pdk``, no
    ``$PDK``) *and* the chosen root holds more than one -- the "cannot
    establish which variant was used" predicate of issue #5639, so its
    emptiness is the verdict. Pure, like the formatter it wraps; the caller
    prints the entry.
    """
    alternatives = report.get("alternative_variants") or []
    if not alternatives:
        return None
    # ``removeprefix``, not an ``if``: with ``alternatives`` non-empty the
    # formatter always returns its prefixed text, so a conditional strip
    # leaves an unreachable branch (the coverage floor on this module is
    # 100%). Same result either way if that text ever loses the prefix.
    message = (pdk_module.alternative_variants_warning(report) or "").removeprefix(
        _ALT_WARNING_PREFIX
    )
    return {
        "code": PDK_VARIANT_UNESTABLISHED_CODE,
        "variant": report["variant"],
        "alternative_variants": list(alternatives),
        "root": report["root"],
        "message": message,
    }


def _pdk_xschem_env(
    pdk_block: dict[str, Any] | None, resolved_symbol_path: Sequence[str]
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """The env xschem runs under, plus the snapshot taken at the PDK export.

    First element: ``os.environ`` with the resolved PDK exported, so an
    xschemrc reading ``$env(PDK_ROOT)``/``$env(PDK)`` agrees with
    ``--pdk``/``--pdk-root`` (issue #4186), with ``--symbol-path`` dirs
    prepended to ``XSCHEM_USER_LIBRARY_PATH`` (issue #4952). Second: the
    snapshot *before* that merge -- only a resolved PDK means klt exported
    ``PDK_ROOT``/``PDK``; ``--symbol-path`` alone builds an env that merely
    inherits them (issue #5441), and error text must not claim klt set what
    it inherited.
    """
    xschem_env: dict[str, str] | None = None
    if pdk_block is not None:
        xschem_env = {
            **os.environ,
            "PDK_ROOT": pdk_block["root"],
            "PDK": pdk_block["variant"],
        }
    exported_pdk_env = xschem_env
    xschem_env = _with_symbol_path_env(xschem_env, resolved_symbol_path)
    return xschem_env, exported_pdk_env
