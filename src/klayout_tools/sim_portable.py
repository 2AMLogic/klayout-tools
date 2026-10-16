"""Portable per-corner deck and log for ``klt sim`` (issue #3052).

With ``options.keep_artifacts`` a corner's generated deck (``corner.cir``)
and the engine log carry the absolute paths of the machine that ran them --
the PDK model directory, the checkout, the per-corner scratch directory. A
project committing those files as the raw record of a run would have to
rewrite them, so the committed bytes would differ from what the tool wrote.

``options.portable_artifacts: true`` (default off; a no-op without
``keep_artifacts``) makes ``klt sim`` do that rewrite itself, once the corner
has finished: every host root is replaced by a stable ``$TOKEN``,
most specific root first, in the deck, the engine log, the rewritten include copy
(``netlist.expanded.cir``) and ``.spiceinit``. The engine always *executes*
the real-path deck -- ngspice does not expand ``$VAR`` in include paths -- so
the portable file is the persisted record, not a re-runnable input.

The substitution map (token -> what it stood for) is echoed in
``environment.portable_artifacts``; the inputs themselves stay identified by
``environment.models_lib_sha256`` and ``environment.netlist_closure``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

#: Token for the per-corner scratch directory (varies per corner, so it is
#: added per corner rather than to the run-wide root list).
CORNER_DIR_TOKEN = "$CORNER_DIR"

#: Per-corner files rewritten besides the deck and the log.
_EXTRA_FILES = ("netlist.expanded.cir", ".spiceinit")

Roots = tuple[tuple[str, str], ...]


def _variants(path: str) -> list[str]:
    """``path`` as given (absolute, normalised) and its symlink-resolved form."""
    out = []
    for candidate in (os.path.abspath(path), os.path.realpath(path)):
        candidate = candidate.rstrip(os.sep)
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def build_roots(
    *,
    request_dir: str,
    artifacts_dir: str,
    models_lib: str | None,
    section_libs: Iterable[str | None] = (),
    osdi_preload: Iterable[str] = (),
    environ: dict[str, str] | None = None,
) -> Roots:
    """The run-wide ``(token, root)`` pairs, in declaration order.

    ``$PDK_ROOT`` (when the environment sets it), ``$MODELS_DIR`` (the
    directory of ``models.lib``), one ``$SECTION_LIB_DIR_<n>`` /
    ``$OSDI_DIR_<n>`` per further model-input directory, ``$REQUEST_DIR``,
    ``$ARTIFACTS_DIR`` and finally ``$HOME``. A symlink-resolved variant of
    each root is substituted too, so the result does not depend on whether
    the host reached a tree through a link.
    """
    env = os.environ if environ is None else environ
    raw: list[tuple[str, str]] = []
    pdk_root = env.get("PDK_ROOT")
    if pdk_root:
        raw.append(("$PDK_ROOT", pdk_root))
    if models_lib:
        raw.append(("$MODELS_DIR", os.path.dirname(os.path.abspath(models_lib))))
    for index, lib in enumerate(dict.fromkeys(p for p in section_libs if p)):
        raw.append((f"$SECTION_LIB_DIR_{index}", os.path.dirname(os.path.abspath(lib))))
    for index, lib in enumerate(dict.fromkeys(osdi_preload)):
        raw.append((f"$OSDI_DIR_{index}", os.path.dirname(os.path.abspath(lib))))
    raw.append(("$REQUEST_DIR", request_dir))
    raw.append(("$ARTIFACTS_DIR", artifacts_dir))
    home = env.get("HOME") or os.path.expanduser("~")
    if home and home != "~":
        raw.append(("$HOME", home))
    roots: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token, path in raw:
        for variant in _variants(path):
            if variant in seen:
                continue
            seen.add(variant)
            roots.append((token, variant))
    return tuple(roots)


def substitute(text: str, roots: Roots) -> str:
    """Replace every root in ``text`` by its token; where roots nest, the
    earlier one in ``roots`` wins (:func:`build_roots` lists the most
    specific first, ``$HOME`` last).

    A root only matches as a whole path prefix (not preceded or followed by a
    path character), so ``/home/u`` never rewrites part of ``/home/user2``.
    """
    ordered = roots
    if not ordered:
        return text
    lookup = {root: token for token, root in ordered}
    pattern = re.compile(
        r"(?<![\w.\-/])(?:"
        + "|".join(re.escape(root) for _token, root in ordered)
        + r")(?![\w.\-])"
    )
    return pattern.sub(lambda match: lookup[match.group(0)], text)


def corner_roots(roots: Roots, corner_dir: str) -> Roots:
    """``roots`` plus this corner's scratch directory as ``$CORNER_DIR``."""
    extra = tuple((CORNER_DIR_TOKEN, v) for v in _variants(corner_dir))
    return extra + roots


def rewrite_corner_files(
    corner_dir: str, deck_path: str, log_path: str, roots: Roots
) -> None:
    """Rewrite the corner's deck, log and companion text files in place.

    Call after the engine has exited and its log has been read. Missing
    files are skipped; an unreadable or unwritable one raises ``OSError``.
    """
    full = corner_roots(roots, corner_dir)
    targets = [deck_path, log_path] + [
        os.path.join(corner_dir, n) for n in _EXTRA_FILES
    ]
    for path in targets:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()
        rewritten = substitute(text, full)
        if rewritten != text:
            with open(
                path, "w", encoding="utf-8", errors="surrogateescape", newline=""
            ) as fh:
                fh.write(rewritten)


def rewrite_corner_artifacts(
    corner_dir: str, deck_path: str, log_path: str, roots: Roots | None
) -> list[dict[str, str]]:
    """:func:`rewrite_corner_files`, as a ``diagnostics`` fragment for the caller.

    ``roots=None`` (``options.portable_artifacts`` off) is a no-op. A failed
    rewrite is a warning, not an error: the host paths simply remain.
    """
    if roots is None:
        return []
    try:
        rewrite_corner_files(corner_dir, deck_path, log_path, roots)
    except OSError as exc:
        return [
            {
                "severity": "warning",
                "code": "unknown",
                "message": (
                    "options.portable_artifacts: could not rewrite the "
                    f"persisted deck/log (host paths remain): {exc}"
                ),
            }
        ]
    return []
