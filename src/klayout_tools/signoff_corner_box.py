"""Derive item 7 corner coverage from the PEX rows in cited reports."""

from dataclasses import dataclass, field
from typing import Any

from .signoff_row_extent import label_list
from .signoff_unmet_reasons import (
    _REASON_CHECK_FAILED,
    _REASON_INVALID_EVIDENCE,
    _REASON_NO_EVIDENCE,
    _REASON_PARTIAL_COVERAGE,
    _REASON_WRONG_KIND,
)

Key = tuple[str | None, str]

#: Why a declared box refuses a citation that is not a PEX report (#7700).
_WRONG_KIND_PROBLEM = (
    "corner_box applies to this partition, and only a PEX report with "
    "delta[] corner rows can satisfy it"
)


def _row_problem(row: Any) -> str | None:
    if not isinstance(row, dict):
        return "each PEX delta row must be an object"
    if row.get("status") not in ("pass", "fail", "error", "skipped"):
        return "each PEX delta row needs a recognised status"
    if row["status"] == "skipped":
        return None
    if not isinstance(row.get("corner_id"), str) or not row["corner_id"]:
        return "each graded PEX delta row needs a non-empty corner_id"
    if "testbench" not in row or not isinstance(row["testbench"], (str, type(None))):
        return "each graded PEX delta row needs its testbench (null for command mode)"
    if not isinstance(row.get("measurement"), str) or not row["measurement"]:
        return "each graded PEX delta row needs its measurement name"
    return None


@dataclass
class CornerBox:
    corners: tuple[str, ...] = ()
    problem: str | None = None
    original_reasons: dict[int, str] = field(default_factory=dict)
    wrong_kind_problem: str = _WRONG_KIND_PROBLEM

    def member_disclosure(self, spec: dict, status: str) -> dict[str, str]:
        reason = self.original_reasons.get(id(spec))
        if status == "met" and reason:
            return {"status": "unmet", "reason": reason}
        return {}

    def coverage(self, envelopes: dict[int, dict[str, Any]]) -> dict[str, Any]:
        keys: dict[Key, None] = {}
        passing: dict[str, set[Key]] = {corner: set() for corner in self.corners}
        for envelope in envelopes.values():
            for row in envelope["delta"]:
                if row["status"] == "skipped":
                    continue
                key = (row["testbench"], row["measurement"])
                keys[key] = None
                if row["status"] == "pass" and row["corner_id"] in passing:
                    passing[row["corner_id"]].add(key)
        missing = []
        for corner, measured in passing.items():
            absent = [
                {"testbench": bench, "measurement": name}
                for bench, name in keys
                if (bench, name) not in measured
            ]
            if absent or not keys:
                missing.append({"corner_id": corner, "keys": absent})
        return {
            "declared": len(passing),
            "covered": len(passing) - len(missing),
            "missing": missing,
        }

    def grade(self, envelopes: dict[int, dict], graded: list, extra: dict) -> tuple:
        summary = self.coverage(envelopes)
        detail = {"corner_box": summary}
        if not graded:
            # Issue #7700: nothing cited is `no_evidence`, as without a box.
            return "unmet", _REASON_NO_EVIDENCE, None, detail, {}
        if summary["missing"]:
            return "unmet", _REASON_PARTIAL_COVERAGE, None, detail, extra
        citations = [grade[2] for _, _, grade in graded]
        return "met", None, citations[0], detail, {**extra, "citations": citations}


def corner_member_outcome(
    box: CornerBox | None,
    spec: dict,
    kind: str,
    envelope: dict,
    unmet: tuple[Any, ...] | None,
) -> tuple[Any, ...] | None:
    """Relax only a PEX error that its delta rows can account for.

    The caller still checks coverage, parasitics, and provenance before
    storing this envelope for use in coverage().
    """
    if box is None:
        return unmet
    if kind != "pex":
        return "unmet", _REASON_WRONG_KIND, None, {"problem": box.wrong_kind_problem}
    rows = envelope.get("delta")
    if not isinstance(rows, list):
        return (
            "unmet",
            _REASON_INVALID_EVIDENCE,
            None,
            {"problem": "PEX delta must be a list"},
        )
    for row in rows:
        if problem := _row_problem(row):
            return "unmet", _REASON_INVALID_EVIDENCE, None, {"problem": problem}
    if any(row["status"] == "fail" for row in rows) or envelope.get("failed", 0):
        return unmet or ("unmet", _REASON_CHECK_FAILED, None, {})
    errors = sum(row["status"] == "error" for row in rows)
    if not errors:
        return unmet
    if envelope.get("status") != "error" or envelope.get("errored", errors) != errors:
        return unmet or ("unmet", _REASON_CHECK_FAILED, None, {})
    if (
        unmet
        and unmet[1] == _REASON_CHECK_FAILED
        and not unmet[3].get("critical_metric_blockers")
    ):
        box.original_reasons[id(spec)] = unmet[1]
        return None
    return unmet


def read_corner_box(manifest: dict, item: int, partition: str) -> CornerBox | None:
    if item != 7 or "corner_box" not in manifest:
        return None
    raw = manifest["corner_box"]
    allowed = {"7", "7.analog", "7.digital"}
    if not isinstance(raw, dict) or not raw or set(raw) - allowed:
        return CornerBox(
            problem="corner_box must map 7, 7.analog or 7.digital to corner lists"
        )
    for key in (f"7.{partition}", "7"):
        if key in raw:
            corners = raw[key]
            if (
                not label_list(corners)
                or not corners
                or len(set(corners)) != len(corners)
            ):
                return CornerBox(
                    problem=f"corner_box.{key} needs a list of unique non-empty IDs"
                )
            return CornerBox(
                tuple(corners),
                wrong_kind_problem=_wrong_kind_problem(
                    key, partition, manifest.get("kind")
                ),
            )
    return None


def _wrong_kind_problem(key: str, partition: str, block_kind: Any) -> str:
    """Name the box on a ``wrong_kind`` refusal and the remedy that works.

    Only a bare ``"7"`` box on a mixed-signal block's digital partition is
    helped by scoping the box to ``"7.analog"``; anywhere else the box has
    to be satisfied or removed.
    """
    if key == "7" and partition == "digital" and block_kind == "mixed-signal":
        return (
            f'{_WRONG_KIND_PROBLEM}; declare the box under "7.analog" '
            "to scope it to the analog partition"
        )
    return f'{_WRONG_KIND_PROBLEM}; cite a PEX report, or remove corner_box["{key}"]'


def print_corner_box(item: dict) -> None:
    box = item.get("detail", {}).get("corner_box")
    if box is None:
        return
    print(f"    corner box: {box['covered']}/{box['declared']} covered")
    for missing in box["missing"]:
        print(f"      missing corner: {missing['corner_id']}")
