"""The tab-delimited timing-path row parsers shared by every ``klt sta`` path
query (issue #8106). Four parsers -- ``_parse_worst_path_rows``,
``_parse_violating_endpoint_rows``, the ``PATH`` tail of ``_apply_port_record``
and ``_apply_instance_path_record`` -- accept the same
``<max|min>\\t<check_role>\\t<slack_s>\\t<startpoint>\\t<endpoint>`` payload,
and every ``reason`` they produce is a JSON response field (``reason`` on an
``unavailable`` record), so this file pins those strings for all four sites.
The per-port copy had no failure-string test at all before this one."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from klayout_tools import (
    post_route_sta_instance_paths,
    post_route_sta_path_groups,
    post_route_sta_violating_endpoints,
)
from klayout_tools.post_route_sta_instance_paths import (
    _INSTANCE_PATHS_BEGIN,
    _INSTANCE_PATHS_END,
    _instance_paths,
    _parse_instance_path_rows,
)
from klayout_tools.post_route_sta_path_groups import (
    _PORT_PATHS_BEGIN,
    _PORT_PATHS_END,
    _WORST_PATHS_BEGIN,
    _WORST_PATHS_END,
    _parse_port_path_rows,
    _parse_worst_path_rows,
    _port_paths,
    parse_path_fields,
    parse_side_path_block,
)
from klayout_tools.post_route_sta_violating_endpoints import (
    _VIOLATING_ENDPOINTS_BEGIN,
    _VIOLATING_ENDPOINTS_END,
    _parse_violating_endpoint_rows,
)

#: A well-formed five-field payload.
GOOD = "max\tsetup\t-1.5e-10\trst_n\tff_a/D"
GOOD_ROW = {
    "engine_check_role": "setup",
    "slack_ns": -1.5e-10 * 1e9,
    "startpoint": "rst_n",
    "endpoint": "ff_a/D",
}
_ENGINE_ERROR = 'invalid command name "find_timing_paths"'


def _worst_path_rows(stdout: str) -> dict[str, list[dict[str, Any]]] | None:
    return _parse_worst_path_rows(stdout)[0]


def _worst_path_reason(stdout: str) -> str | None:
    return _parse_worst_path_rows(stdout)[1]


def _violating_endpoint_rows(stdout: str) -> dict[str, list[dict[str, Any]]] | None:
    return _parse_violating_endpoint_rows(stdout)[0]


def _violating_endpoint_reason(stdout: str) -> str | None:
    return _parse_violating_endpoint_rows(stdout)[1]


def _port_path_rows(stdout: str) -> dict[str, list[dict[str, Any]]] | None:
    return _parse_port_path_rows(stdout, ["p"])["p"][1]


def _port_path_reason(stdout: str) -> str | None:
    return _port_paths(stdout, ["p"])["p"]["setup"]["reason"]


def _instance_path_rows(stdout: str) -> dict[str, list[dict[str, Any]]] | None:
    return _parse_instance_path_rows(stdout, ["u1"])["u1"]["into"][0]


def _instance_path_reason(stdout: str) -> str | None:
    return _instance_paths(stdout, ["u1"])["u1"]["into"]["setup"]["reason"]


@dataclass(frozen=True)
class _Site:
    """One parser site: how it spells a row around the five-field payload,
    and how to read its parsed rows / failure reason back."""

    label: str
    begin: str
    end: str
    #: payload -> the site's row text.
    row: Callable[[str], str]
    #: Rows every block needs before its path rows (the per-port ``DIR``).
    lead: tuple[str, ...]
    extra: dict[str, Any]
    error_row: str
    error_reason: str
    absent: str
    rows: Callable[[str], dict[str, list[dict[str, Any]]] | None]
    reason: Callable[[str], str | None]


_SITES: dict[str, _Site] = {
    "worst-path": _Site(
        label="worst-path",
        begin=_WORST_PATHS_BEGIN,
        end=_WORST_PATHS_END,
        row=lambda payload: "PATH\t" + payload,
        lead=(),
        extra={},
        error_row="ERROR\t" + _ENGINE_ERROR,
        error_reason="engine path introspection failed: " + _ENGINE_ERROR,
        absent="worst-path records absent from engine output",
        rows=_worst_path_rows,
        reason=_worst_path_reason,
    ),
    "violating-endpoint": _Site(
        label="violating-endpoint",
        begin=_VIOLATING_ENDPOINTS_BEGIN,
        end=_VIOLATING_ENDPOINTS_END,
        row=lambda payload: "VPATH\t" + payload + "\tclk",
        lead=(),
        extra={"path_group": "clk"},
        error_row="ERROR\t" + _ENGINE_ERROR,
        error_reason="engine violating-endpoint introspection failed: " + _ENGINE_ERROR,
        absent="violating-endpoint records absent from engine output",
        rows=_violating_endpoint_rows,
        reason=_violating_endpoint_reason,
    ),
    "port path": _Site(
        label="port path",
        begin=_PORT_PATHS_BEGIN,
        end=_PORT_PATHS_END,
        row=lambda payload: "PATH\tp\t" + payload,
        lead=("DIR\tp\tinput",),
        extra={},
        error_row="ERROR\tp\t" + _ENGINE_ERROR,
        error_reason="engine path introspection failed: " + _ENGINE_ERROR,
        absent="per-port path records absent from engine output",
        rows=_port_path_rows,
        reason=_port_path_reason,
    ),
    "instance path": _Site(
        label="instance path",
        begin=_INSTANCE_PATHS_BEGIN,
        end=_INSTANCE_PATHS_END,
        row=lambda payload: "IPATH\tu1\tinto\t" + payload,
        lead=(),
        extra={},
        error_row="ERROR\tu1\t" + _ENGINE_ERROR,
        error_reason="engine path introspection failed: " + _ENGINE_ERROR,
        absent="per-instance path records absent from engine output",
        rows=_instance_path_rows,
        reason=_instance_path_reason,
    ),
}

#: (payload, reason template). ``{label}`` is the site's label and ``{line}``
#: its whole row's ``repr``.
_BAD_PAYLOADS = [
    ("max\tsetup\t1e-10\tonly_start", "malformed {label} record: {line}"),
    ("max\tsetup\t1e-10\ta\tb\textra", "malformed {label} record: {line}"),
    ("sideways\tsetup\t1e-10\ta\tb", "malformed {label} record: {line}"),
    ("max\tsetup\t1e-10\ta\t", "malformed {label} record: {line}"),
    ("max\t\t1e-10\ta\tb", "malformed {label} record: {line}"),
    ("max\tsetup\tfast\ta\tb", "malformed {label} slack: {line}"),
    ("max\tsetup\tnan\ta\tb", "non-finite {label} slack: {line}"),
    ("max\tsetup\tinf\ta\tb", "non-finite {label} slack: {line}"),
    ("max\tsetup\t-inf\ta\tb", "non-finite {label} slack: {line}"),
    ("max\tsetup\t1e999\ta\tb", "non-finite {label} slack: {line}"),
]


def _block(site: _Site, *rows: str) -> str:
    return "\n".join([site.begin, *site.lead, *rows, site.end, ""])


@pytest.mark.parametrize("site", _SITES.values(), ids=list(_SITES))
def test_rows_are_grouped_by_side_and_scaled_to_ns(site):
    stdout = _block(
        site,
        site.row(GOOD),
        "Warning: a stray engine line that is no row at all",
        site.row("min\thold\t2e-10\trst_n\tff_a/D"),
    )
    assert site.rows(stdout) == {
        "max": [{**GOOD_ROW, **site.extra}],
        "min": [
            {
                "engine_check_role": "hold",
                "slack_ns": 2e-10 * 1e9,
                "startpoint": "rst_n",
                "endpoint": "ff_a/D",
                **site.extra,
            }
        ],
    }
    assert site.reason(stdout) is None


@pytest.mark.parametrize("site", _SITES.values(), ids=list(_SITES))
def test_absent_block_is_unavailable_with_the_sites_own_reason(site):
    assert site.reason("no markers here") == site.absent
    # Truncated output: the begin marker printed, the end marker never did.
    assert site.reason(site.begin + "\n" + site.row(GOOD)) == site.absent


@pytest.mark.parametrize("site", _SITES.values(), ids=list(_SITES))
def test_engine_error_row_is_unavailable(site):
    assert site.reason(_block(site, site.error_row)) == site.error_reason


@pytest.mark.parametrize("site", _SITES.values(), ids=list(_SITES))
@pytest.mark.parametrize(
    ("payload", "template"),
    _BAD_PAYLOADS,
    ids=[p[0].replace("\t", "|") for p in _BAD_PAYLOADS],
)
def test_bad_row_reason_strings(site, payload, template):
    row = site.row(payload)
    expected = template.format(label=site.label, line=repr(row))
    # A bad row after a good one still reports nothing -- never a partial list.
    stdout = _block(site, site.row(GOOD), row)
    assert site.rows(stdout) is None
    assert site.reason(stdout) == expected


@pytest.mark.parametrize(
    "row",
    [
        "IPATH\tu1",
        "IPATH\tu1\tinto",
        "IPATH\tu1\tsideways\tmax\tsetup\t1e-9\ta\tb",
    ],
)
def test_instance_path_row_too_short_or_unknown_group_is_malformed(row):
    reason = _instance_paths(_block(_SITES["instance path"], row), ["u1"])["u1"]["into"]
    assert reason["setup"]["reason"] == f"malformed instance path record: {row!r}"


# -- the shared helpers ----------------------------------------------------- #


def test_parse_path_fields_returns_side_row_and_no_reason():
    side, row, reason = parse_path_fields(GOOD.split("\t"), "any", label="x")
    assert (side, reason) == ("max", None)
    assert row == GOOD_ROW


def test_parse_path_fields_appends_extra_keys_after_the_path_fields():
    side, row, reason = parse_path_fields(
        [*GOOD.split("\t"), "clk"], "any", label="x", extra_keys=("path_group",)
    )
    assert (side, reason) == ("max", None)
    assert list(row) == [*GOOD_ROW, "path_group"]
    assert row["path_group"] == "clk"


def test_parse_path_fields_missing_extra_column_is_a_malformed_record():
    assert parse_path_fields(
        GOOD.split("\t"), "any", label="x", extra_keys=("path_group",)
    ) == (None, None, "malformed x record: 'any'")


@pytest.mark.parametrize(
    ("payload", "template"),
    _BAD_PAYLOADS,
    ids=[p[0].replace("\t", "|") for p in _BAD_PAYLOADS],
)
def test_parse_path_fields_rejects_every_bad_payload(payload, template):
    line = "row\t" + payload
    assert parse_path_fields(payload.split("\t"), line, label="x") == (
        None,
        None,
        template.format(label="x", line=repr(line)),
    )


_BLOCK_ARGS = {
    "begin": "===B===",
    "end": "===E===",
    "row_prefix": "VPATH",
    "label": "violating-endpoint",
    "error_prefix": "engine violating-endpoint introspection failed: ",
    "extra_keys": ("path_group",),
}


def test_parse_side_path_block_absent_is_none_with_the_labelled_reason():
    assert parse_side_path_block("no markers here", **_BLOCK_ARGS) == (
        None,
        "violating-endpoint records absent from engine output",
    )


def test_parse_side_path_block_groups_rows_and_skips_every_other_line():
    stdout = "\n".join(
        [
            "===B===",
            # A PATH row is not a VPATH row: the prefix filter is exact.
            "PATH\t" + GOOD,
            "Warning: stray engine line",
            "VPATH\t" + GOOD + "\tclk",
            "VPATH\tmin\thold\t2e-10\trst_n\tff_a/D\tclk",
            "===E===",
            "",
        ]
    )
    assert parse_side_path_block(stdout, **_BLOCK_ARGS) == (
        {
            "max": [{**GOOD_ROW, "path_group": "clk"}],
            "min": [
                {
                    "engine_check_role": "hold",
                    "slack_ns": 2e-10 * 1e9,
                    "startpoint": "rst_n",
                    "endpoint": "ff_a/D",
                    "path_group": "clk",
                }
            ],
        },
        None,
    )


def test_parse_side_path_block_error_row_is_none_with_the_prefixed_reason():
    stdout = "\n".join(
        ["===B===", "VPATH\t" + GOOD + "\tclk", "ERROR\t  boom\t", "===E==="]
    )
    assert parse_side_path_block(stdout, **_BLOCK_ARGS) == (
        None,
        "engine violating-endpoint introspection failed: boom",
    )


def test_parse_side_path_block_bad_row_is_none_with_the_row_reason():
    row = "VPATH\tmax\tsetup\tfast\ta\tb\tclk"
    stdout = "\n".join(["===B===", "VPATH\t" + GOOD + "\tclk", row, "===E==="])
    assert parse_side_path_block(stdout, **_BLOCK_ARGS) == (
        None,
        f"malformed violating-endpoint slack: {row!r}",
    )


# -- every site delegates to the shared helpers ------------------------------ #


def test_every_path_row_parser_goes_through_the_shared_field_parser(monkeypatch):
    """Guards against re-duplication (the ``test_sta_marker_block`` idea): a
    parser that grew back its own copy of the five-field validation would
    never reach this spy."""
    labels: list[str] = []
    real = post_route_sta_path_groups.parse_path_fields

    def spy(fields, line, *, label, extra_keys=()):
        labels.append(label)
        return real(fields, line, label=label, extra_keys=extra_keys)

    for module in (post_route_sta_path_groups, post_route_sta_instance_paths):
        monkeypatch.setattr(module, "parse_path_fields", spy)
    for site in _SITES.values():
        assert site.rows(_block(site, site.row(GOOD))) is not None
    assert labels == ["worst-path", "violating-endpoint", "port path", "instance path"]


def test_block_parsers_delegate_to_the_shared_block_parser(monkeypatch):
    """``_parse_worst_path_rows`` and ``_parse_violating_endpoint_rows`` are
    the two marker-block parsers; neither keeps its own marker loop."""
    labels: list[str] = []
    real = post_route_sta_path_groups.parse_side_path_block

    def spy(stdout, *, begin, end, row_prefix, label, error_prefix, extra_keys=()):
        labels.append(label)
        return real(
            stdout,
            begin=begin,
            end=end,
            row_prefix=row_prefix,
            label=label,
            error_prefix=error_prefix,
            extra_keys=extra_keys,
        )

    for module in (post_route_sta_path_groups, post_route_sta_violating_endpoints):
        monkeypatch.setattr(module, "parse_side_path_block", spy)
    for name in ("worst-path", "violating-endpoint"):
        assert (
            _SITES[name].rows(_block(_SITES[name], _SITES[name].row(GOOD))) is not None
        )
    assert labels == ["worst-path", "violating-endpoint"]
