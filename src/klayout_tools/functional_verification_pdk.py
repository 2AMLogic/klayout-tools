"""``request.pdk`` for ``klt functional-verification`` (issue #5782): pin the
PDK install a gate-level re-simulation's behavioural std-cell models resolve
from, so a committed request carries no host-specific PDK path.

Three responsibilities, kept here rather than in
``functional_verification.py`` (size-ratchet baseline; the #5684 precedent of
moving ``_resolve_sources`` out whole):

1. **Validate + merge the pdk input.** The ``request.pdk`` block
   ``{variant?, root?, cell_library}`` plus the CLI's ``--pdk``/``--pdk-root``
   (CLI wins per key, matching sibling-verb precedence). ``root`` resolves on
   the same single request-relative anchor every other path field uses
   (:func:`functional_verification_from_synthesize._request_relative`,
   issue #5673 constraint). No pdk input at all returns ``None``, and every
   consumer keyed off ``None`` is byte-identical to before this issue.
2. **Derive the compile-time defines** the resolved models need:
   ``FUNCTIONAL`` (the zero-delay branch) only when no ``options.sdf`` is
   present, ``USE_POWER_PINS`` never (a ``klt synthesize`` netlist has no
   power pins). Caller ``options.defines`` win at the merge point, and the
   derived ``FUNCTIONAL`` exists only when ``options.sdf`` is absent, so it
   can never trip
   :func:`functional_verification_sdf._reject_sdf_with_functional_models`
   -- this feature cannot manufacture the contradiction that check rejects.
3. **Cross-check the requested ``cell_library``** against the synthesize
   report's ``provenance.deck.name`` prefix (``<cell_library>__<corner>``)
   when ``options.from_synthesize`` is also present -- resolving models for a
   different library than the netlist instantiates would fail elaboration at
   best and mis-simulate at worst. Refused only when the report carries the
   name (not every committed synthesize report does); the check runs inside
   :func:`functional_verification_from_synthesize.resolve_from_synthesize`,
   which already holds the loaded report.

Also home of :func:`_resolve_defines` (moved out of ``functional_verification``
unchanged, re-exported there), since this module owns the verb's whole define
surface: caller-supplied validation plus the derived contribution.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

from .functional_verification_from_synthesize import _request_relative

PDK_REQUEST_KEYS = ("variant", "root", "cell_library")


class PdkInput(NamedTuple):
    """The merged pdk input one run resolves cell models from."""

    variant: str | None
    root: str | None
    cell_library: str


def _err_cls():
    """The verb's error class. Annotated loosely (the class itself is a
    ``Callable[..., Exception]``: its ``__init__`` takes the stable ``code``
    keyword the named refusals below raise with)."""
    from .functional_verification import FunctionalVerificationError

    return FunctionalVerificationError


def _err(message: str, *, code: str | None = None) -> Exception:
    return _err_cls()(message, code=code)


def validate_pdk_request_block(raw: Any, request_dir: str) -> PdkInput:
    """Validate ``request.pdk`` (``cell_library`` required; ``variant``/
    ``root`` optional) and resolve a relative ``root`` against
    ``request_dir`` -- the same base every other request path field uses."""
    if not isinstance(raw, dict):
        raise _err(
            "request.pdk must be a JSON object "
            "{variant?, root?, cell_library} (cell_library required)"
        )
    unknown = sorted(set(raw) - set(PDK_REQUEST_KEYS))
    if unknown:
        raise _err(f"request.pdk has unknown key(s): {', '.join(unknown)}")
    cell_library = raw.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise _err("request.pdk.cell_library must be a non-empty string")
    variant = raw.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise _err("request.pdk.variant must be a non-empty string or null")
    root = raw.get("root")
    if root is not None and (not isinstance(root, str) or not root):
        raise _err("request.pdk.root must be a non-empty string or null")
    resolved_root = (
        _request_relative(root, request_dir) if isinstance(root, str) else None
    )
    return PdkInput(variant, resolved_root, cell_library)


def merge_pdk_input(
    request_doc: dict[str, Any],
    request_dir: str,
    cli_variant: str | None,
    cli_root: str | None,
) -> PdkInput | None:
    """Merge the CLI flags over the request block; ``None`` when no pdk input
    exists at all (the byte-identical-to-today path, issue #5782 AC1).

    Either source alone is fine for ``variant``/``root``; ``cell_library``
    comes only from the request block, so CLI flags without a block are a
    request error (never a silently-ignored flag). A CLI ``--pdk-root`` is
    passed through for :func:`klayout_tools.pdk.find_pdk` to resolve against
    the cwd, the sibling-verb convention; a *request-block* ``root`` resolves
    against the request's own directory like every other path field.
    """
    raw = request_doc.get("pdk")
    if cli_variant is None and cli_root is None:
        if raw is None:
            return None
        return validate_pdk_request_block(raw, request_dir)
    if raw is None:
        raise _err(
            "--pdk/--pdk-root given but the request has no pdk block -- "
            "request.pdk.cell_library is required to resolve cell models"
        )
    merged = validate_pdk_request_block(raw, request_dir)
    if cli_variant is not None:
        merged = merged._replace(variant=cli_variant)
    if cli_root is not None:
        merged = merged._replace(root=os.path.abspath(os.path.expanduser(cli_root)))
    return merged


def derive_pdk_defines(
    pdk_input: PdkInput | None, options: Any
) -> dict[str, str | None]:
    """The defines a ``pdk`` input contributes (issue #5782 AC5).

    ``FUNCTIONAL`` selects a PDK's zero-delay behavioural branch -- the right
    choice for a pre-CTS ``klt synthesize`` netlist with no SDF -- so it is
    derived only when ``options.sdf`` is absent: with an SDF the request must
    elaborate the *timing* branch instead, and a derived ``FUNCTIONAL`` there
    would be exactly the ``options.sdf`` + ``FUNCTIONAL`` contradiction
    :func:`functional_verification_sdf._reject_sdf_with_functional_models`
    exists to reject. ``USE_POWER_PINS`` is never derived (a ``klt
    synthesize`` netlist has no power pins; defining it is an elaboration
    error). Caller ``options.defines`` are merged **over** these at the run's
    single merge point, and the per-source ``options.source_defines`` hatch
    (#3290) is untouched.
    """
    if pdk_input is None:
        return {}
    if isinstance(options, dict) and "sdf" in options:
        return {}
    return {"FUNCTIONAL": None}


def check_deck_cell_library(cell_library: str, report: dict[str, Any]) -> None:
    """Refuse a ``cell_library`` that cannot be the deck the synthesize
    report's netlist was mapped against (issue #5782 AC4).

    ``klt synthesize`` records the deck as ``provenance.deck.name`` =
    ``<cell_library>__<corner>``; resolving models for any other library
    would hand the netlist cells it never instantiates. Refused only when the
    name is present (not every committed report carries a ``provenance``
    block); an absent name skips the check rather than guessing a refusal.
    """
    provenance = report.get("provenance")
    deck = provenance.get("deck") if isinstance(provenance, dict) else None
    name = deck.get("name") if isinstance(deck, dict) else None
    if not isinstance(name, str) or not name:
        return
    if name == cell_library or name.startswith(cell_library + "__"):
        return
    raise _err(
        f"request.pdk.cell_library '{cell_library}' does not match the "
        f"synthesize report's deck '{name}' (a deck name is "
        f"'<cell_library>__<corner>') -- the report's netlist instantiates "
        f"'{name.split('__', 1)[0]}' cells, so '{cell_library}' models would "
        "not elaborate against it",
        code="pdk_cell_library_mismatch",
    )


def resolve_pdk_cell_models(pdk_input: PdkInput) -> tuple[list[str], dict[str, Any]]:
    """Resolve ``pdk_input``'s behavioural cell models through the shared
    resolver (see :mod:`klayout_tools.pdk_cell_models`)."""
    from .pdk_cell_models import resolve_cell_models_for_cell_library

    return resolve_cell_models_for_cell_library(
        pdk_input.cell_library,
        _err_cls(),
        variant=pdk_input.variant,
        root=pdk_input.root,
    )


def _resolve_defines(defines: Any) -> dict[str, str | None]:
    """Validate the optional ``request.options.defines`` object and return it
    unchanged (or ``{}`` when omitted).

    Forwarded to ``Runner.build(defines=...)`` -- cocotb's own ``Runner``
    accepts string values; a ``None`` value defines the macro with no value
    (e.g. ``` `define USE_POWER_PINS ```). cocotb 2.x cannot serialise
    ``None`` itself, so ``_run_build`` emits those as ``-DNAME`` build args.
    """
    if defines is None:
        return {}
    if not isinstance(defines, dict):
        raise _err(
            "request.options.defines must be a JSON object of string -> string|null"
        )
    for key, value in defines.items():
        if not isinstance(key, str) or not key:
            raise _err("request.options.defines keys must be non-empty strings")
        if value is not None and not isinstance(value, str):
            raise _err(
                f"request.options.defines[{key!r}] must be a string or null "
                f"-- got a {type(value).__name__}"
            )
    return defines
