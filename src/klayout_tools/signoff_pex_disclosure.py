"""The cited ``klt pex`` envelope's own extraction disclosures for ``klt
signoff`` (issues #1983, #6559), split out of ``signoff.py`` for its
size-ratchet ceiling -- the ``signoff_yield_disclosure`` precedent: pure
functions over an envelope dict, importing nothing from the module they were
split out of, re-imported there by name so ``signoff.<name>`` keeps resolving.

Both are **reported, never graded on**: item 7's verdict is still
``status == "pass"`` alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _pex_body_bias_disclosure(
    kind: str, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    """The cited `klt pex` envelope's own statement about whether the
    extracted netlist it re-simulated had a DC bias path for every device
    body (issue #1983) -- or ``None`` when there is nothing to quote.

    Quoted verbatim off the envelope, never re-derived: `klt pex` builds it
    from the extraction it drove itself (``docs/cli/pex.md`` ->
    ``body_bias``), which is the only place the information exists.

    ``None`` in exactly two cases, and they mean the same thing to a reader
    ("this artifact makes no body-bias statement"), never "the bodies were
    biased":

    - ``kind != "pex"``. No other envelope kind carries a ``body_bias``
      block. (`klt lvs`'s parallel ``body_verification`` block is surfaced
      separately, as ``detail.body_verification_status`` -- it answers a
      related but distinct question: whether the *compare* verified the body
      ties, not whether the *simulated* netlist had a bias path.)
    - The envelope has no ``body_bias`` key at all, or a non-object one:
      post-layout evidence committed before `klt pex` reported it.
      Back-compat is the whole reason this is ``None`` rather than a
      fabricated ``"biased"`` -- an old record must not read as a netlist
      that was checked and found clean.

    ``unbiased_pmos_body_nets`` is deliberately **not** carried through: the
    per-device list can run to hundreds of entries on a real block, and a
    reader who needs it has the cited envelope. The count and the distinct
    net names are enough to tell a clean run from a compromised one and to
    find the devices in the source artifact.
    """
    if kind != "pex":
        return None
    body_bias = envelope.get("body_bias")
    if not isinstance(body_bias, dict):
        return None
    nets = body_bias.get("unbiased_nets")
    return {
        "status": body_bias.get("status"),
        "unbiased_device_count": body_bias.get("unbiased_device_count"),
        "unbiased_nets": list(nets) if isinstance(nets, list) else [],
    }


def _pex_engine_disclosure(
    kind: str, envelope: Mapping[str, Any]
) -> dict[str, Any] | None:
    """What the cited ``klt pex`` envelope says about *which extractor* built
    its extracted leg (issue #6559) -- or ``None`` when there is nothing to
    quote.

    ``klt pex --engine klayout-runset`` extracts a device-level netlist with
    **no parasitics** (``docs/cli/pex.md``, "Runset engine"), so its
    ``delta[]`` rows are device-representation deltas, not parasitic ones: a
    passing report does not by itself earn T1 item 7's intent. The envelope
    says so in ``extraction.engine``/``parasitics_carried``/
    ``params_not_carried``; this quotes them verbatim, never re-deriving them,
    so a reader of the citation sees the caveat beside the verdict.

    Returns ``{"pex_extraction": {...}}`` -- already keyed, so a call site
    merges it with ``update(... or {})`` and adds no branch to its (complexity-
    ratcheted) builder.

    ``None`` in exactly two cases, both meaning "this artifact makes no
    engine statement", never "the extraction had parasitics": ``kind !=
    "pex"``, and an envelope whose ``extraction`` carries no ``engine`` key --
    every curated-deck run, and every record committed before the route
    existed, which therefore renders exactly as it did before.

    Grading policy for a parasitic-free leg is deliberately not decided here
    (issue #6606): this is disclosure only.
    """
    if kind != "pex":
        return None
    extraction = envelope.get("extraction")
    if not isinstance(extraction, dict) or not extraction.get("engine"):
        return None
    uncarried = extraction.get("params_not_carried")
    return {
        "pex_extraction": {
            "engine": extraction.get("engine"),
            "parasitics_carried": extraction.get("parasitics_carried"),
            "params_not_carried": (
                dict(uncarried) if isinstance(uncarried, dict) else {}
            ),
        }
    }
