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
        "deck": {"name": "sky130", "content_hash": "sha256:..."},
        "input": {"content_hash": "sha256:..."}
    }

The block is purely *additive* to the shared envelope (see
``docs/json-contract.md``): it never renames or removes an existing field and
needs no ``schema_version`` bump. Fields that can't be resolved (PDK not
installed, no deck involved) are ``null`` -- never fabricated.

This module also owns the single ``sha256_file`` implementation the verbs
share (previously copy-pasted in ``sim.py``, ``extract.py``, and ``lvs.py``).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from typing import Any


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
    """The provenance ``deck`` shape ``{name, content_hash}``; ``None`` when
    no rule/model deck was involved (e.g. LVS against a pre-extracted
    netlist).

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
    block: dict[str, Any] = {"name": name, "content_hash": _content_hash(path)}
    if deck_options:
        block["options"] = dict(deck_options)
    return block


def _input_block(path: str | None) -> dict[str, Any] | None:
    """The provenance ``input`` shape ``{content_hash}``, mirroring ``deck``;
    ``None`` when no input path was given (or it can't be hashed)."""
    if path is None:
        return None
    return {"content_hash": _content_hash(path)}


def build_provenance(
    *,
    deck_name: str | None = None,
    deck_path: str | None = None,
    pdk: dict[str, Any] | None = None,
    input_path: str | None = None,
    deck_options: Mapping[str, str] | None = None,
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
    (the default) when the verb has no single input layout to pin (or already
    covers this itself, as ``klt lvs`` does via
    ``environment.layout_sha256``/``reference_sha256``).
    ``deck_options`` (issue #595) is echoed onto ``provenance.deck.options``
    via :func:`_deck_block` when non-empty -- see that function's docstring.
    ``klt_version``/``klayout_version`` are read at call time.
    """
    return {
        "klt_version": _klt_version(),
        "klayout_version": _klayout_version(),
        "pdk": _pdk_block(pdk),
        "deck": _deck_block(deck_name, deck_path, deck_options),
        "input": _input_block(input_path),
    }
