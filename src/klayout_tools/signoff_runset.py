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
- **Re-hash.** The runset file named by the report must still hash to the
  recorded value, exactly like the layout/reference re-hash a curated-deck
  report gets from ``klt lvs --check``. A drifted file is
  :data:`REASON_RUNSET_HASH_MISMATCH`; a file that can no longer be read is not
  a mismatch but an unverifiable one, left to the caller.
- **Full device recognition.** The engine reports ``status: "match"`` when the
  devices it *did* recognise agree, so a class it declared but never
  recognised (``coverage.declared_not_recognized``), or a per-class device count
  that differs between layout and reference, must still fail
  (:func:`unrecognised_device_gaps`).

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
_REVIEW = (
    "klayout-tools#4309: fetched at the commit and hashed; digest reviewed in that PR"
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
        "reviewed_by": "klayout-tools#4395: fetched at the commit and hashed; digest reviewed in that PR",
    },
}


def is_runset_report(envelope: dict[str, Any]) -> bool:
    return envelope.get("engine") == RUNSET_ENGINE


def runset_block(envelope: dict[str, Any]) -> dict[str, Any]:
    block = (envelope.get("provenance") or {}).get("runset")
    return block if isinstance(block, dict) else {}


def unrecognised_device_gaps(envelope: dict[str, Any]) -> list[str]:
    """Why a runset report does not prove every device was recognised -- an
    empty list when it does. Entries are human-readable and stable enough to
    quote in a check's ``detail``."""
    gaps: list[str] = []
    coverage = envelope.get("coverage")
    census = envelope.get("device_census")
    if not isinstance(coverage, dict) or not isinstance(census, dict):
        return ["report carries no coverage/device_census to prove recognition"]
    for cls in coverage.get("declared_not_recognized") or []:
        gaps.append(f"device class '{cls}' declared but not recognised")
    layout = census.get("layout") or {}
    reference = census.get("reference") or {}
    for cls in sorted(set(layout) | set(reference)):
        if layout.get(cls, 0) != reference.get(cls, 0):
            gaps.append(
                f"device class '{cls}': {layout.get(cls, 0)} in layout vs "
                f"{reference.get(cls, 0)} in reference"
            )
    return gaps


def runset_refusal(envelope: dict[str, Any]) -> str | None:
    """The reason a *passing* runset report must still be refused, or ``None``
    (also for any non-runset report, which this never touches)."""
    if not is_runset_report(envelope):
        return None
    recorded = runset_block(envelope).get("content_hash")
    if not isinstance(recorded, str) or recorded not in PINNED_RUNSETS:
        return REASON_RUNSET_NOT_PINNED
    deck_file = envelope.get("deck_file")
    live = sha256_file(deck_file) if isinstance(deck_file, str) else None
    if live is not None and f"sha256:{live}" != recorded:
        return REASON_RUNSET_HASH_MISMATCH
    return None


def runset_disclosure(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """``engine`` plus the runset's hash/pin, for a check's ``detail`` and a
    ``met`` citation; ``None`` for a non-runset report so curated-deck output is
    byte-for-byte unchanged."""
    if not is_runset_report(envelope):
        return None
    block = runset_block(envelope)
    recorded = block.get("content_hash")
    return {
        "engine": RUNSET_ENGINE,
        "runset_content_hash": recorded,
        "pdk_pin": PINNED_RUNSETS.get(recorded) if isinstance(recorded, str) else None,
    }
