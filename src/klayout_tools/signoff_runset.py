"""Grading rules for a ``klt lvs`` report produced by the PDK's own KLayout LVS
runset (``"engine": "klayout-runset"``; issue #2870, building on #2849).

The operator ruling of 2026-10-08 makes such a report accepted evidence for
T1 item 4 (and the LVS half of item 11) for device families the curated
extraction deck does not recognise. Accepting it safely needs three things the
engine's own envelope cannot assert about itself, all decided here:

- **Pin.** The runset must be the PDK's own, at a pinned revision. A block
  can not self-declare that (a field the report carries is a field the block
  author chooses), so the pin is a signoff-side allowlist,
  :data:`PINNED_RUNSETS`, mapping a runset's content hash to the PDK revision
  it was published at. A report whose ``provenance.runset.content_hash`` is not
  in the table is refused with :data:`REASON_RUNSET_NOT_PINNED`.
- **Re-hash.** A readable runset file named by the report must still hash to
  the recorded value, exactly like the layout/reference re-hash a curated-deck
  report gets from ``klt lvs --check``. A drifted file is
  :data:`REASON_RUNSET_HASH_MISMATCH`. If the digest is unavailable, an
  otherwise passing citation discloses that the re-hash was unverifiable.
- **Full device recognition.** The engine reports ``status: "match"`` when
  the devices it *did* recognise agree, so a per-class device count that
  differs between layout and reference must still fail, and so must a
  reference device the runset's reader dropped on both sides -- a card in
  the reference source that survives in neither census reads ``match``
  while proving nothing (issue #6902). :func:`unrecognised_device_gaps`
  measures exactly that: case-folded per-class census equality plus the
  report's own ``device_census.reference_source`` evidence, read by the
  engine from the reference source with the plain SPICE reader. Classes the
  runset declares with zero instances anywhere are **not** graded -- no
  real block instantiates every class a PDK runset declares -- but stay in
  the report as disclosure (``coverage.declared_not_recognized``).

An entry is only added after someone has fetched the PDK's published runset
at a named commit and recorded its digest here (one entry, one reviewed
commit; the procedure is in ``docs/design-evidence-tiers.md`` item 4). A
runset report whose hash is not in the table is refused as unpinned rather
than trusted. The hash covers the top-level runset file only, not the
``rule_decks/`` files it includes, so the pinned ``commit`` is what ties the
entry to a full tree.
"""

from __future__ import annotations

from typing import Any

from ._provenance import sha256_file

RUNSET_ENGINE = "klayout-runset"

REASON_RUNSET_NOT_PINNED = "runset_not_pinned"
REASON_RUNSET_HASH_MISMATCH = "runset_hash_mismatch"
REASON_RUNSET_UNRECOGNISED_DEVICE = "runset_unrecognised_device"

_IHP_REPO = "IHP-GmbH/IHP-Open-PDK"
_GF_PV_REPO = "fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pv"
_SKY130_REPO = "efabless/sky130_klayout_pdk"
_REVIEW = (
    "klayout-tools#4309: fetched at the commit and hashed; digest reviewed in that PR"
)
_REVIEW_GF180 = (
    "klayout-tools#4395: fetched at the commit and hashed; digest reviewed in that PR"
)
_REVIEW_SKY130 = (
    "klayout-tools#6902: fetched at the commit and hashed; digest reviewed in that PR"
)

#: ``sha256:<hex>`` of a PDK-published LVS runset file -> ``{"pdk", "revision",
#: "repo", "commit", "path", "reviewed_by"}``. Signoff-side on purpose; see the
#: module docstring. Not pinned (runset not fetchable/runnable at a verifiable
#: commit): none at present. The gf180mcu entry is the ``fd_pv`` repo's
#: ``klayout/lvs/gf180mcu.lvs``; open_pdks installs that file byte-identically
#: into each of the gf180mcuA-D variant trees.
PINNED_RUNSETS: dict[str, dict[str, str]] = {
    "sha256:e01bcb1ec5858e8069bade8f7fd7ec28c1026fc2fd0590e8f611d3398a5bbbd7": {
        "pdk": "ihp-sg13g2",
        "revision": "v0.3.0",
        "repo": _IHP_REPO,
        "commit": "5cccb161f7492697cfa52eb14dc03beb00bdca9e",
        "path": "ihp-sg13g2/libs.tech/klayout/tech/lvs/sg13g2.lvs",
        "reviewed_by": _REVIEW,
    },
    "sha256:0d91cb6e4c4aba974ffb07175614c183faab1cb8c6b7fab483e38d3cfcb5df5c": {
        "pdk": "ihp-sg13cmos5l",
        "revision": "dev@f7bca32",
        "repo": _IHP_REPO,
        "commit": "f7bca32694fe3d58be0cc7d9df4b9f5db7161a14",
        "path": "ihp-sg13cmos5l/libs.tech/klayout/tech/lvs/sg13cmos5l.lvs",
        "reviewed_by": _REVIEW,
    },
    "sha256:09a4f1f1a6fe1fc1e4e193ae5ae1c2b1c49ba190f9e135ffd72d45c14637fde9": {
        "pdk": "gf180mcu",
        "revision": "fd_pv@1a4e4b5",
        "repo": _GF_PV_REPO,
        "commit": "1a4e4b59f5d1ea4dfd7b108a02bc085151609ccb",
        "path": "klayout/lvs/gf180mcu.lvs",
        "reviewed_by": _REVIEW_GF180,
    },
    # Issue #6902. open_pdks installs this file byte-identically (KLAYOUT_URL
    # in its sky130 Makefile) into sky130A/libs.tech/klayout/lvs/sky130.lvs;
    # the digest equals the one the committed 2AMLogic/sky130-usb2-phy
    # reports record, so those reports need no re-run for the pin.
    "sha256:5b4ede4bdb4744b60e42c9b29baa93f40bb24430fb8f3a48a276a712a492ac38": {
        "pdk": "sky130",
        "revision": "main@13d2572",
        "repo": _SKY130_REPO,
        "commit": "13d2572144450daba6a9f3508ae4c6e93e852466",
        "path": "tech/sky130/lvs/sky130.lvs",
        "reviewed_by": _REVIEW_SKY130,
    },
}


def is_runset_report(envelope: dict[str, Any]) -> bool:
    return envelope.get("engine") == RUNSET_ENGINE


def runset_block(envelope: dict[str, Any]) -> dict[str, Any]:
    block = (envelope.get("provenance") or {}).get("runset")
    return block if isinstance(block, dict) else {}


def _folded_class_counts(counts: Any) -> dict[str, int]:
    """``counts`` with upper-cased keys -- SPICE names are case-insensitive
    and the two census sides arrive in different case (a sky130 report run
    with ``convert_subckts`` reads layout ``sky130_fd_pr__...`` against
    reference ``SKY130_FD_PR__...``; issue #6902). Non-integer values are
    skipped, not coerced: a census that does not carry counts is not
    evidence."""
    folded: dict[str, int] = {}
    if isinstance(counts, dict):
        for name, count in counts.items():
            if isinstance(count, int) and not isinstance(count, bool):
                key = str(name).upper()
                folded[key] = folded.get(key, 0) + count
    return folded


def unrecognised_device_gaps(envelope: dict[str, Any]) -> list[str]:
    """Why a runset report does not prove every device was recognised -- an
    empty list when it does. Entries are human-readable and stable enough to
    quote in a check's ``detail``.

    Three measurements, all against the 2026-10-08 ruling's condition (4)
    ("every device in the layout is recognised; an unrecognised or dropped
    device is a failure") as corrected by issue #6902:

    - per-class census equality, case-folded -- a class with instances on
      one side only is an unrecognised device;
    - the report must carry ``device_census.reference_source`` (the engine's
      own evidence, read from the reference source with the plain SPICE
      reader); a report without it cannot prove the runset's reader kept
      every reference device and fails closed -- re-run ``klt lvs`` with a
      klt that emits the field;
    - the converted reference must account for every in-scope source card:
      the runset's reader renames some callees (sky130
      ``special_nfet_01v8`` -> ``nfet_01v8``), so per-name equality between
      source and converted census does not hold and the **totals** are
      compared instead -- rename-proof, and still zero-miss for the
      both-sides-drop case the old ``declared_not_recognized``-non-empty
      proxy guarded: a card present in the source but dropped by the reader
      survives in neither census and shows up as converted < source.

    ``coverage.declared_not_recognized`` is disclosure, not graded: a class
    the runset declares with zero instances in the layout, the converted
    reference *and* the reference source is absent from all three counts and
    fails nothing (no real block instantiates every class a PDK runset
    declares)."""
    gaps: list[str] = []
    coverage = envelope.get("coverage")
    census = envelope.get("device_census")
    if not isinstance(coverage, dict) or not isinstance(census, dict):
        return ["report carries no coverage/device_census to prove recognition"]
    layout = _folded_class_counts(census.get("layout"))
    reference = _folded_class_counts(census.get("reference"))
    for cls in sorted(set(layout) | set(reference)):
        if layout.get(cls, 0) != reference.get(cls, 0):
            gaps.append(
                f"device class '{cls}': {layout.get(cls, 0)} in layout vs "
                f"{reference.get(cls, 0)} in reference"
            )
    source = census.get("reference_source")
    if not isinstance(source, dict):
        gaps.append(
            "report carries no device_census.reference_source -- it predates "
            "the reference-source device evidence of issue #6902 (or its "
            "reference could not be re-read) and cannot prove the runset's "
            "reader kept every reference device; re-run 'klt lvs' with a klt "
            "that emits the field"
        )
        return gaps
    source_total = sum(_folded_class_counts(source).values())
    converted_total = sum(reference.values())
    if converted_total < source_total:
        gaps.append(
            f"reference source holds {source_total} device cards the runset's "
            f"reader is responsible for but the converted reference census "
            f"accounts for {converted_total} -- devices were dropped on both "
            "sides (issue #6902)"
        )
    return gaps


def check_runset(envelope: dict[str, Any]) -> tuple[str | None, bool]:
    """Return ``(refusal, rehash_unverifiable)`` for a passing LVS report.

    The flag is true only when the recorded hash is pinned and no live
    digest is available. Curated-deck reports need neither check.
    """
    if not is_runset_report(envelope):
        return None, False
    recorded = runset_block(envelope).get("content_hash")
    if not isinstance(recorded, str) or recorded not in PINNED_RUNSETS:
        return REASON_RUNSET_NOT_PINNED, False
    deck_file = envelope.get("deck_file")
    try:
        live = sha256_file(deck_file) if isinstance(deck_file, str) else None
    except OSError:
        live = None
    if live is None:
        return None, True
    if f"sha256:{live}" != recorded:
        return REASON_RUNSET_HASH_MISMATCH, False
    return None, False


def runset_disclosure(
    envelope: dict[str, Any], *, rehash_unverifiable: bool = False
) -> dict[str, Any] | None:
    """Render runset provenance and any unavailable re-hash from grading."""
    if not is_runset_report(envelope):
        return None
    block = runset_block(envelope)
    recorded = block.get("content_hash")
    disclosure = {
        "engine": RUNSET_ENGINE,
        "runset_content_hash": recorded,
        "pdk_pin": PINNED_RUNSETS.get(recorded) if isinstance(recorded, str) else None,
    }
    if rehash_unverifiable:
        disclosure["runset_rehash_status"] = "unverifiable"
    return disclosure
