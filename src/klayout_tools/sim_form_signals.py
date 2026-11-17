"""Signal texts of the structured measurement forms (issue #5559).

``options.save_vectors`` (issue #3211) names the ngspice vectors a run must
keep resident. The free-text fields (``spice``/``expr``, ``crossings.signal``,
``step_resolution.signal``, the ``settled`` signals) are scanned for
``v(...)``/``i(...)`` tokens where they are declared, but every remaining
measurement form names its signals in *structured* fields instead. This module
collects those texts so ``sim._required_save_vectors`` can run the same token
scan (:data:`SAVE_TOKEN_RE`) over them:

* fields that hold expressions or selectors (``cycle_mean.signal``/``align``,
  ``energy.voltage``, ``snm.x``/``y1``/``y2``, ``eye.signal``,
  ``transfer.num``/``den``, ``tian.tv``/``ti``) pass through verbatim;
* fields that hold a bare name (``energy.source``, ``stress.a``/``b``,
  ``tian.series_injection.load_node``/``source``) are wrapped in the
  ``i(...)``/``v(...)`` form their own deck lines read.

Unknown values are skipped: validation of each form rejects them before a deck
is written, and collection must not raise while assembling one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: A ``v(...)``/``i(...)`` token (optionally with ngspice's derivative prefix,
#: e.g. ``vdb(out)``, ``vm(a,b)``) in a measurement's signal text -- the
#: vectors ``options.save_vectors`` must keep resident for that measurement
#: to resolve.
SAVE_TOKEN_RE = re.compile(r"\b([vi][a-z]{0,2})\(([^()]*)\)", re.IGNORECASE)

_Collector = Callable[[dict[str, Any], list[str]], None]


def _add_text(texts: list[str], value: Any) -> None:
    """Append ``value`` when it holds a signal text (else skip it)."""
    if isinstance(value, str):
        texts.append(value)


def _cycle_mean_texts(body: dict[str, Any], texts: list[str]) -> None:
    _add_text(texts, body.get("signal"))
    _add_text(texts, body.get("align"))


def _energy_texts(body: dict[str, Any], texts: list[str]) -> None:
    _add_text(texts, body.get("voltage"))
    if isinstance(body.get("source"), str):
        texts.append(f"i({body['source']})")


def _stress_texts(body: dict[str, Any], texts: list[str]) -> None:
    for key in ("a", "b"):
        if isinstance(body.get(key), str):
            texts.append(f"v({body[key]})")


def _snm_texts(body: dict[str, Any], texts: list[str]) -> None:
    for key in ("x", "y1", "y2"):
        _add_text(texts, body.get(key))


def _eye_texts(body: dict[str, Any], texts: list[str]) -> None:
    _add_text(texts, body.get("signal"))


def _transfer_texts(body: dict[str, Any], texts: list[str]) -> None:
    _add_text(texts, body.get("num"))
    _add_text(texts, body.get("den"))


def _series_injection_texts(series: dict[str, Any], texts: list[str]) -> None:
    if isinstance(series.get("load_node"), str):
        texts.append(f"v({series['load_node']})")
    if isinstance(series.get("source"), str):
        texts.append(f"i({series['source']})")


def _tian_texts(body: dict[str, Any], texts: list[str]) -> None:
    _add_text(texts, body.get("tv"))
    _add_text(texts, body.get("ti"))
    series = body.get("series_injection")
    if isinstance(series, dict):
        _series_injection_texts(series, texts)


#: One collector per structured form, in the order the combined list reports
#: its vectors -- ``_required_save_vectors`` de-duplicates in first-seen
#: order, so this order is part of the generated ``save`` card's shape.
_FORM_COLLECTORS: dict[str, _Collector] = {
    "cycle_mean": _cycle_mean_texts,
    "energy": _energy_texts,
    "stress": _stress_texts,
    "snm": _snm_texts,
    "eye": _eye_texts,
    "transfer": _transfer_texts,
    "tian": _tian_texts,
}


def signal_texts(spec: dict[str, Any]) -> list[str]:
    """The signal texts of every structured form ``spec`` declares (the
    free-text fields are the caller's: this module covers the rest)."""
    texts: list[str] = []
    for form, collect in _FORM_COLLECTORS.items():
        body = spec.get(form)
        if isinstance(body, dict):
            collect(body, texts)
    return texts
