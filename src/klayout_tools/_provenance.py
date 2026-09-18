"""Shared reproducibility provenance for the JSON envelope.

A ``klt drc`` "clean" verdict (or an ``extract``/``lvs``/``sim`` result) is
only meaningful relative to an exact rule deck, PDK release, and tool build --
two runs made against different deck revisions or PDK releases are otherwise
indistinguishable in the output, so a signoff claim can't be audited or
reproduced later. Every verb whose verdict depends on those inputs (``drc``,
``lvs``, ``extract``, ``sim``, ``precheck``) embeds the ``provenance`` block
this module builds:

    "provenance": {
        "klt_version": "0.4.2",
        "klayout_version": "0.29.8",
        "pdk": {"name": "sky130A", "source": "volare", "version": "<stamp>"},
        "deck": {"name": "sky130", "content_hash": "sha256:...", "released": true},
        "input": {"content_hash": "sha256:..."}
    }

The block is purely *additive* to the shared envelope (see
``docs/json-contract.md``): it never renames or removes an existing field and
needs no ``schema_version`` bump. Fields that can't be resolved (PDK not
installed, no deck involved) are ``null`` -- never fabricated.

This module also owns the single ``sha256_file`` implementation the verbs
share (previously copy-pasted in ``sim.py``, ``extract.py``, and ``lvs.py``),
plus ``_yosys_version``/``_combined_content_hash`` (previously copy-pasted --
and silently diverged -- between ``equiv.py`` and ``synthesize.py``; issue
#1112), plus ``wasi_sandbox_hint_if_applicable`` (issue #1755 -- the #1368
WASI-sandboxed-yosys detection had only been added to ``synthesize.py``,
leaving ``equiv.py``'s independent error-formatting function without it).

Alongside the raw-byte ``sha256_file`` it also owns ``layout_geometry_digest``
(issue #2065) -- a *layout-aware* digest for reports that pin the input streams
they were built from (``klt gen-compose``'s ``blocks[].source_digest``), where
a raw-byte hash would report drift on every re-write of identical geometry.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from . import build_identity
from .decks.history import is_deck_hash_released

_YOSYS_VERSION_RE = re.compile(r"Yosys\s+(\S+)")

_WASI_SANDBOX_SCRIPT_NOT_FOUND_RE = re.compile(
    r"Can't open script file `(.+)' for reading: No such file or directory"
)

_WASI_SANDBOX_HINT = (
    "; the script file exists on disk but yosys could not read it -- this "
    "usually means the 'yosys' on $PATH is a WASI-sandboxed build (e.g. "
    "yowasp-yosys) whose sandbox does not preopen this path. Try prepending "
    "a native yosys build's directory to $PATH."
)


def wasi_sandbox_hint_if_applicable(error_line: str) -> str:
    """``_WASI_SANDBOX_HINT`` if ``error_line`` is Yosys's ``Can't open
    script file `<path>' for reading: No such file or directory`` shape
    *and* ``<path>`` verifiably exists on the host filesystem -- otherwise
    ``""``.

    A script path that genuinely does not exist is a different, unrelated
    failure and must not get the hint. Shared by ``synthesize.py`` and
    ``equiv.py`` (issue #1755) so the #1368 WASI-sandboxed-yosys (e.g.
    ``yowasp-yosys``) detection can't silently diverge between verbs again,
    the same failure mode issue #1112 fixed for ``_yosys_version``/
    ``_combined_content_hash`` above.
    """
    match = _WASI_SANDBOX_SCRIPT_NOT_FOUND_RE.search(error_line)
    if match and os.path.isfile(match.group(1)):
        return _WASI_SANDBOX_HINT
    return ""


def sha256_file(path: str | None) -> str | None:
    """Streamed SHA-256 hex digest of the file at ``path``, or ``None``.

    Returns ``None`` for a falsy path or one that is not an existing file --
    the defensive shape ``sim.py`` already relied on, now shared by every
    caller (``lvs``/``extract`` only pass paths that exist at hash time, so
    the guard is a no-op there).
    """
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Bumped whenever the canonical serialisation below changes shape, so a digest
# computed by an older klt build can never be mistaken for a digest of the same
# geometry computed by a newer one (the two would differ for reasons that have
# nothing to do with the geometry drifting).
_LAYOUT_GEOMETRY_DIGEST_VERSION = "klt-layout-geometry-digest/1"


def _canonical_layer_key(info: Any) -> str:
    """A layer's stable identity for :func:`layout_geometry_digest`.

    ``layer/datatype`` for a numbered layer (what both GDS and OASIS actually
    store), falling back to the layer *name* only for a name-only layer, which
    has no number to key on. A layer that carries both keeps its number as the
    identity -- GDS stores no layer names at all, so folding the name in would
    make the same geometry hash differently depending on the container format.
    """
    if info.is_named():
        return f"name:{info.name}"
    return f"{info.layer}/{info.datatype}"


def _canonical_properties(layout: Any, prop_id: int) -> str:
    """User properties attached to a shape/instance, sorted, or ``""``."""
    if not prop_id:
        return ""
    try:
        props = layout.properties(prop_id)
    except Exception:
        return ""
    rendered = sorted(f"{key!r}={value!r}" for key, value in props)
    return " props=[" + ",".join(rendered) + "]"


def _canonical_shape_token(shape: Any) -> str:
    """One shape, rendered so that geometrically identical shapes render
    identically.

    Boxes are widened to polygons because the two are the same geometry
    written two ways (GDS has no box record at all; OASIS does), and KLayout's
    ``Polygon`` normalises its own point order/winding/collinear points on
    construction -- so a rectangle written as a box and the same rectangle
    written as a four-point boundary produce one token, not two.
    """
    import klayout.db as kdb

    if shape.is_box():
        return f"polygon {kdb.Polygon(shape.box).to_s()}"
    if shape.is_polygon():
        return f"polygon {shape.polygon.to_s()}"
    if shape.is_path():
        return f"path {shape.path.to_s()}"
    if shape.is_text():
        return f"text {shape.text.to_s()}"
    if shape.is_edge():
        return f"edge {shape.edge.to_s()}"
    # Anything the accessors above do not cover (edge pairs, point-like
    # shapes a future KLayout adds) still contributes its own rendering --
    # unknown-but-present beats silently dropped.
    return f"other {shape.to_s()}"


def _canonical_instance_token(inst: Any) -> str:
    """One child-cell instance, keyed by the *name* of the cell it places.

    Deliberately not ``cell_index``: that is an index into this one layout's
    own cell table, so it changes when a writer emits its cells in a different
    order even though nothing about the placement moved.

    A regular array's two axes are sorted rather than reported as written:
    ``(a, na)`` and ``(b, nb)`` name the same lattice of placements in either
    order, and writers really do disagree about which is which (KLayout writes
    the same array with the axes swapped between GDS and OASIS).
    """
    token = f"inst {inst.cell.name} {inst.cplx_trans.to_s()}"
    if inst.is_regular_array():
        axes = sorted([(inst.a.to_s(), inst.na), (inst.b.to_s(), inst.nb)])
        rendered = ",".join(f"{vector}x{count}" for vector, count in axes)
        token += f" array=({rendered})"
    return token


def layout_geometry_digest(path: str | None) -> str | None:
    """A ``sha256:``-prefixed digest of a GDS/OASIS stream's *decoded
    geometry*, or ``None`` when it cannot be computed (issue #2065).

    Deliberately **not** :func:`sha256_file`. A raw-byte hash answers "are
    these two files identical", which is the wrong question for a layout
    stream: re-writing geometrically identical output produces different bytes
    every time (the BGNLIB/BGNSTR timestamp records carry the write time, and
    shape/instance order within a cell follows whatever order the writer
    happened to emit). A consumer comparing raw-byte hashes therefore sees
    drift on every re-run and cannot tell a re-write from a real change --
    exactly the failure mode issue #2065 was filed against. The byte-level
    contract of :func:`sha256_file`/:func:`_content_hash` is unchanged and
    still what ``drc``/``lvs``/``extract``/``sim`` record; this is a second,
    layout-aware digest for callers that need equality to mean "same
    geometry".

    The digest covers, in a fixed order that no writer can perturb:

    - the layout's database unit;
    - every cell, sorted by name;
    - within each cell, every shape (keyed by layer/datatype) and every
      child-cell instance (keyed by the placed cell's *name*), each rendered
      canonically and then sorted, so element order in the file is irrelevant;
    - user properties attached to those shapes/instances.

    Container metadata that says nothing about the geometry -- timestamps,
    record order, cell-table indices, the format itself -- is never read.

    Returns ``None`` (never a fabricated value, matching this module's
    convention for everything it cannot resolve) for a falsy path, a path that
    is not an existing file, a stream KLayout cannot read, or a missing
    ``klayout`` engine.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        import klayout.db as kdb

        layout = kdb.Layout()
        layout.read(path)

        digest = hashlib.sha256()

        def feed(line: str) -> None:
            digest.update(line.encode("utf-8", errors="backslashreplace"))
            digest.update(b"\n")

        feed(_LAYOUT_GEOMETRY_DIGEST_VERSION)
        feed(f"dbu {layout.dbu:.12g}")

        layer_keys = [
            (index, _canonical_layer_key(layout.get_info(index)))
            for index in layout.layer_indexes()
        ]
        for cell in sorted(layout.each_cell(), key=lambda c: c.name):
            tokens: list[str] = []
            for index, layer_key in layer_keys:
                for shape in cell.shapes(index).each():
                    tokens.append(
                        f"shape {layer_key} {_canonical_shape_token(shape)}"
                        f"{_canonical_properties(layout, shape.prop_id)}"
                    )
            for inst in cell.each_inst():
                tokens.append(
                    f"{_canonical_instance_token(inst)}"
                    f"{_canonical_properties(layout, inst.prop_id)}"
                )
            feed(f"cell {cell.name}")
            for token in sorted(tokens):
                feed(f"  {token}")
    except Exception:
        return None
    return f"sha256:{digest.hexdigest()}"


def _yosys_version() -> str | None:
    """Yosys's own reported version token (``yosys -V``'s output), or
    ``None`` if unresolvable -- never raises. Shared by ``equiv.py`` (SAT
    equivalence checking) and ``synthesize.py`` (RTL synthesis), which both
    invoke Yosys as an external engine and record its version as
    ``engine_version``. Combines the safety checks the two call sites had
    independently accumulated before this dedup (issue #1112): a ``timeout``
    against a hung subprocess, and a non-zero-``returncode`` check against a
    Yosys invocation that printed nothing useful to stdout.
    """
    try:
        completed = subprocess.run(
            ["yosys", "-V"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    match = _YOSYS_VERSION_RE.search(completed.stdout or "")
    return match.group(1) if match else None


def _combined_content_hash(paths: list[str]) -> str | None:
    """A single ``sha256:``-prefixed digest covering every file in ``paths``
    (order-independent -- sorted before hashing), for the multi-``sources``
    case :func:`build_provenance`'s single-path ``input_path`` argument
    cannot express on its own. ``None`` if any file cannot be hashed.

    The digest is computed over file *contents* only, independent of the
    paths themselves -- so the same set of file contents at different paths
    (e.g. a copy in a different worktree) hashes identically. Shared by
    ``equiv.py`` and ``synthesize.py`` (issue #1112 -- the two previously had
    independently-copied implementations that produced different digests for
    the same inputs, one mixing the path into the hash and one not; this is
    the path-independent scheme both now use).
    """
    digests = []
    for path in sorted(paths):
        digest = sha256_file(path)
        if digest is None:
            return None
        digests.append(digest)
    combined = hashlib.sha256("".join(digests).encode("utf-8")).hexdigest()
    return f"sha256:{combined}"


def _content_hash(path: str | None) -> str | None:
    """The envelope's ``sha256:``-prefixed deck-hash form of ``path``, or
    ``None`` when the file can't be hashed."""
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def _klt_version() -> str | None:
    """``klayout_tools.__version__`` (the ``klt --version`` string), or
    ``None`` if unresolvable."""
    try:
        import klayout_tools

        return getattr(klayout_tools, "__version__", None)
    except Exception:
        return None


def _klayout_version() -> str | None:
    """``klayout.__version__`` (the KLayout Python engine build), or ``None``
    -- mirrors ``lvs.py``'s existing engine-version lookup."""
    try:
        import klayout
    except Exception:
        return None
    return getattr(klayout, "__version__", None)


def _klayout_version_mismatch(actual: str | None, expected: str | None) -> bool:
    """Whether the resolved ``klayout`` engine (``actual``) differs from the
    version this ``klayout-tools`` build/commit was tested against
    (``expected``, :func:`klayout_tools.build_identity.klayout_version_expected`)
    -- issue #1490.

    Always a plain boolean, never a tri-state: ``klayout_version_mismatch``
    is documented as ``true|false``, so "cannot determine" (either side
    unresolvable -- an old build predating this field, or no ``klayout``
    importable at all) renders as ``False`` -- "no *confirmed* mismatch" --
    rather than fabricating a signal from missing data.
    """
    if actual is None or expected is None:
        return False
    return actual != expected


def _warn_klayout_version_mismatch(actual: str | None, expected: str | None) -> None:
    """The stderr warning printed once per report when
    :func:`_klayout_version_mismatch` is ``True`` (issue #1490) -- a caller
    piping ``--format json`` to a file still sees this on the terminal,
    since JSON output goes to stdout only (``docs/json-contract.md``)."""
    message = (
        f"klt: warning: resolved klayout engine version {actual!r} differs "
        f"from klayout=={expected}, the version this klayout-tools build/"
        "commit was tested against -- DRC/LVS report counts (deck content "
        "hash, rules_skipped, category_counts, ...) may differ from a "
        f"report generated with klayout=={expected}, even though the "
        "verdict itself is unaffected. To reproduce the exact engine: "
        f'`uv tool install "klayout-tools @ git+...@<sha>" '
        f"--with klayout=={expected}` -- see docs/json-contract.md's "
        '"Shared `provenance` block".'
    )
    print(message, file=sys.stderr)


def _pdk_block(pdk: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a :func:`klayout_tools.pdk.find_pdk`-style dict into the
    provenance ``pdk`` shape ``{name, source, version}``; ``None`` when no PDK
    was resolved for the run."""
    if not pdk:
        return None
    return {
        "name": pdk.get("variant"),
        "source": pdk.get("resolved_via"),
        "version": pdk.get("version"),
    }


def _deck_block(
    name: str | None,
    path: str | None,
    deck_options: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The provenance ``deck`` shape ``{name, content_hash, released}``;
    ``None`` when no rule/model deck was involved (e.g. LVS against a
    pre-extracted netlist).

    ``released`` (issue #1193) is the generation-time answer to "does any
    released ``klayout-tools`` version ship this exact deck content hash" --
    the same question ``klt deck resolve --content-hash`` answers on demand,
    asked automatically so a project doesn't accumulate committed evidence
    against an unreproducible deck without ever being told. It is a tri-state
    signal, not a plain boolean, via
    :func:`klayout_tools.decks.history.is_deck_hash_released`:

    - ``True`` -- the hash matches a released version; nothing to warn about.
    - ``False`` -- the deck history table loaded fine and confirms this hash
      shipped in no release (a dev checkout, an uncommitted deck edit, or a
      deck added after the last tag -- e.g. ``sg13g2`` before its first
      tagged release).
    - ``None`` -- the answer is unknown, either because ``content_hash``
      itself couldn't be computed, or the history table
      (``decks/_history.json``) is missing, unreadable, or malformed.
      Deliberately distinct from ``False``: a broken/missing history table
      must never be reported as a confirmed "this deck is unreleased" claim.

    ``deck_options`` (issue #595, ``klt extract --deck-option``) is echoed
    verbatim as an additional ``options`` key when non-empty -- e.g. gf180mcu's
    caller-selectable resistor sheet-rho flavour (``{"poly_res": "2k"}``) --
    so a record can pin exactly which flavour of a shared-geometry device
    family a run resolved. Omitted entirely when ``deck_options`` is
    ``None``/empty, keeping the block byte-identical to before this
    parameter existed for every call site that does not pass it.
    """
    if name is None:
        return None
    content_hash = _content_hash(path)
    block: dict[str, Any] = {
        "name": name,
        "content_hash": content_hash,
        "released": is_deck_hash_released(name, content_hash),
    }
    if deck_options:
        block["options"] = dict(deck_options)
    return block


def _input_block(path: str | None) -> dict[str, Any] | None:
    """The provenance ``input`` shape ``{content_hash}``, mirroring ``deck``;
    ``None`` when no input path was given (or it can't be hashed)."""
    if path is None:
        return None
    return {"content_hash": _content_hash(path)}


class UnknownProvenanceDeckError(Exception):
    """Raised by :func:`deck_identity` for a name no built-in deck registers."""


def deck_identity(name: str) -> dict[str, Any]:
    """The ``provenance.deck`` block for a built-in deck, by name alone
    (issue #1202) -- no layout file, no engine run.

    ``provenance.deck.content_hash`` is the identity that actually pins a
    run's rule set, but obtaining it used to require *running a check* (and
    therefore having some layout handy to run it against). This is the same
    block a report would carry for ``name``, computed the only way that can
    stay honest: by calling :func:`_deck_block` itself, so the answer cannot
    drift from what a real run records.

    Raises :class:`UnknownProvenanceDeckError` for a name no deck registers --
    without which :func:`~klayout_tools.decks.deck_source_path` would happily
    import and hash any sibling module of the deck package.
    """
    from .decks import deck_names, deck_source_path

    known = deck_names()
    if name not in known:
        raise UnknownProvenanceDeckError(
            f"unknown deck '{name}' (available: {', '.join(known)})"
        )
    block = _deck_block(name, deck_source_path(name))
    if block is None:  # unreachable: _deck_block is None only for name=None
        raise UnknownProvenanceDeckError(f"unknown deck '{name}'")
    return block


def build_provenance(
    *,
    deck_name: str | None = None,
    deck_path: str | None = None,
    pdk: dict[str, Any] | None = None,
    input_path: str | None = None,
    deck_options: Mapping[str, str] | None = None,
    include_klayout_version_mismatch: bool = False,
) -> dict[str, Any]:
    """Build the shared ``provenance`` envelope block.

    ``deck_name``/``deck_path`` identify the rule (or model) deck a run used
    and the file whose content is hashed to pin it; pass both ``None`` when no
    deck was involved. ``pdk`` is a :func:`klayout_tools.pdk.find_pdk`-style
    dict (``variant``/``resolved_via``/``version``), or ``None`` when the run
    resolved no PDK. ``input_path`` is the input layout stream a verb ran
    against; when given, its content hash is recorded as ``provenance.input``
    (the same ``{content_hash}`` shape as ``deck``) so a stale committed
    report is a one-line diff against a freshly computed hash. Pass ``None``
    (the default) only when the verb genuinely has no single input stream to
    pin. A verb that pins its input under a *verb-specific* key of its own is
    **not** such a case: ``klt lvs`` carries
    ``environment.layout_sha256``/``reference_sha256`` and was originally
    (issue #331) left with ``input: null`` on exactly that reasoning, but
    ``klt signoff --manifest``'s staleness gate reads
    ``provenance.input.content_hash`` generically across every check kind and
    cannot see an LVS-only field -- so a ``content_hash``-pinned "LVS clean"
    citation always graded ``stale_evidence``. Issue #1969 reversed that:
    ``klt lvs`` now passes its layout-side hash source here too, and the
    per-verb duplication is the intended cost of a field generic consumers
    can actually read.
    ``deck_options`` (issue #595) is echoed onto ``provenance.deck.options``
    via :func:`_deck_block` when non-empty -- see that function's docstring.
    ``klt_version``/``klayout_version`` are read at call time.

    ``include_klayout_version_mismatch`` (issue #1490, opt-in and ``False``
    by default) adds ``provenance.klayout_version_mismatch``: ``True`` when
    the resolved ``klayout_version`` differs from
    :func:`klayout_tools.build_identity.klayout_version_expected` (the
    version this ``klayout-tools`` build/commit was tested against), else
    ``False``. Passed by ``klt drc``/``klt lvs`` only -- see
    ``docs/design/klayout-engine-version-pin.md`` for why those two verbs and
    not every ``build_provenance`` caller. A ``True`` result also prints a
    one-line warning to stderr (:func:`_warn_klayout_version_mismatch`) so a
    caller sees the drift even without inspecting the JSON.
    """
    actual_klayout_version = _klayout_version()
    block: dict[str, Any] = {
        "klt_version": _klt_version(),
        "klayout_version": actual_klayout_version,
        "pdk": _pdk_block(pdk),
        "deck": _deck_block(deck_name, deck_path, deck_options),
        "input": _input_block(input_path),
    }
    if include_klayout_version_mismatch:
        # Build-time-recorded only -- deliberately *not*
        # `build_identity.klayout_version_expected()`'s live-checkout
        # fallback, which shells out to `git` (issue #1490). That fallback is
        # fine as a one-shot cost for `klt version --format json`; calling it
        # from every `klt drc`/`klt lvs` run would add a subprocess call to
        # every single report on a dev/editable install, and no
        # `uv tool install ... @<sha>` install (the reproducibility scenario
        # this field exists for) is ever editable -- `hatch_build.py` always
        # records `KLAYOUT_VERSION_EXPECTED` for that install path, so the
        # live probe would only ever fire for a case this field need not
        # cover. An editable/dev checkout with no build-time record simply
        # reports `klayout_version_mismatch: False` ("no confirmed
        # mismatch"), matching `_klayout_version_mismatch`'s documented
        # "unresolvable renders as False" rule.
        expected_klayout_version = build_identity._recorded_klayout_version_expected()
        mismatch = _klayout_version_mismatch(
            actual_klayout_version, expected_klayout_version
        )
        block["klayout_version_mismatch"] = mismatch
        if mismatch:
            _warn_klayout_version_mismatch(
                actual_klayout_version, expected_klayout_version
            )
    return block
