"""Input validation and campaign-level limits for threshold fit reports."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ._jsonnum import require_finite_number


class ThresholdError(Exception):
    """Raised when ``klt yield-threshold`` cannot run: a bad/missing input
    document or an invalid option. The CLI turns it into a clean stderr
    message + exit code 1, never a traceback (``docs/json-contract.md``).
    """


def load_document(path: str) -> tuple[Any, str]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ThresholdError(
            f"cannot read records document {path!r}: {exc.strerror or exc}"
        ) from exc
    try:
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except ValueError as exc:
        raise ThresholdError(
            f"records document {path!r} is not valid JSON: {exc}"
        ) from exc


def record_number(value: Any, where: str) -> float:
    msg = "{field} must be a finite number, got {value!r}"
    return require_finite_number(value, ThresholdError, where, message=msg)


def _parse_limits(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw or raw.keys() - {"sigma_max", "margin"}:
        raise ThresholdError("limits must declare sigma_max, margin, or both")
    limits: dict[str, dict[str, Any]] = {}
    if "sigma_max" in raw:
        maximum = record_number(raw["sigma_max"], "limits.sigma_max")
        if maximum < 0:
            raise ThresholdError("limits.sigma_max must be nonnegative")
        limits["sigma_max"] = {"maximum": maximum}
    if "margin" in raw:
        margin = raw["margin"]
        if not isinstance(margin, dict) or margin.keys() != {
            "separation",
            "multiple",
            "source",
        }:
            raise ThresholdError(
                "limits.margin requires separation, multiple and source"
            )
        separation = record_number(margin["separation"], "limits.margin.separation")
        multiple = record_number(margin["multiple"], "limits.margin.multiple")
        source = margin["source"]
        if separation < 0 or multiple <= 0:
            raise ThresholdError(
                "limits.margin needs nonnegative separation and positive multiple"
            )
        if not isinstance(source, str) or not source.strip():
            raise ThresholdError(
                "limits.margin.source must identify the separation measurement"
            )
        limits["margin"] = {
            "separation": separation,
            "multiple": multiple,
            "source": source,
        }
    return limits


def _verdict(verdicts: list[str]) -> str:
    if "fail" in verdicts:
        return "fail"
    if not verdicts or "insufficient_evidence" in verdicts:
        return "insufficient_evidence"
    return "pass"


def _check(name: str, operands: dict[str, Any], fit: dict[str, Any]) -> dict[str, Any]:
    upper = fit["sigma_ci"]["upper"]
    check = {**operands, "sigma_ci_upper": upper}
    if fit["status"] != "ok" or upper is None:
        check.update(
            verdict="insufficient_evidence",
            reason=fit["reason"] or "sigma CI upper bound is open",
        )
    else:
        passed = (
            upper <= operands["maximum"]
            if name == "sigma_max"
            else operands["separation"] >= operands["multiple"] * upper
        )
        check["verdict"] = "pass" if passed else "fail"
    return check


def grade_corners(corners: list[dict[str, Any]], raw_limits: Any) -> str:
    """Attach checks to each corner and return the overall campaign verdict."""
    limits = _parse_limits(raw_limits)
    for corner in corners:
        checks = {
            name: _check(name, operands, corner["fit"])
            for name, operands in limits.items()
        }
        corner["checks"] = checks
        corner["verdict"] = _verdict([check["verdict"] for check in checks.values()])
    return _verdict([corner["verdict"] for corner in corners])
