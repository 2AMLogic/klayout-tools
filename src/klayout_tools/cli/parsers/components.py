"""``klt components`` argument parser (split out of ``parser.py``, issue #5552).

Parser registration lives here; ``run()`` stays in ``components_cmd``. Shared
helpers are passed in as callables, following ``parsers/extract.py``. Moved
out rather than grown in place because ``parser.py`` is over the
repository's per-file line limit and may not grow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import components_cmd


def add_components_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``components`` verb: report connected components across
    a caller-selected conductor/via stack.
    """
    components_parser = subparsers.add_parser(
        "components",
        help="report connected components across a caller-selected conductor/via stack",
        description=(
            "Report which shapes form one electrically connected geometric "
            "component across a caller-selected set of conductor and via "
            "layers -- no PDK deck, no device recognition. Same-layer "
            "touching shapes on one conductor always merge; two different "
            "conductors join only where an explicit via is declared and "
            "actually lands on both -- bare XY overlap between un-via'd "
            "conductors never joins two components. Useful for inspecting "
            "unnamed, device-free metal, annotation geometry, an incomplete "
            "layout, or a deliberately limited layer subset that `klt "
            "extract`'s curated, PDK-specific decks are not a fit for. Runs "
            "fully headless via KLayout's native batch database API -- no "
            "GUI, no Qt. See docs/cli/components.md."
        ),
    )
    components_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    components_parser.add_argument(
        "--conductors",
        required=True,
        help=(
            "conductor layer set as a path to a JSON file, or an inline JSON "
            'array of {"name": str, "layer": [layer, datatype]} objects '
            '(e.g. \'[{"name": "m1", "layer": [68, 20]}]\'); an entry may '
            'add "and"/"not": lists of [layer, datatype] pairs that keep only '
            "the part of its layer overlapping / outside those layers. Not "
            "validated by argparse -- an empty/malformed value exits 1 with "
            "a clean error, per docs/cli/components.md's exit-code contract, "
            "rather than argparse's usage-error exit 2."
        ),
    )
    components_parser.add_argument(
        "--vias",
        default=None,
        help=(
            "optional via mapping as a path to a JSON file, or an inline "
            'JSON array of {"name": str, "layer": [layer, datatype], '
            '"between": [conductor_name_a, conductor_name_b]} objects '
            '(e.g. \'[{"name": "mcon", "layer": [67, 44], "between": '
            '["li", "m1"]}]\'); each via joins the two named --conductors '
            "entries only where its own shapes actually land on both. Takes "
            'the same optional "and"/"not" qualifiers as --conductors. Omit '
            "for a purely same-layer connectivity report."
        ),
    )
    components_parser.add_argument(
        "--label-layers",
        dest="label_layers",
        default=None,
        help=(
            "optional GDS text layers to scan for names/labels/pins, as a "
            'path to a JSON file or an inline JSON array of {"name": str, '
            '"layer": [layer, datatype], "conductor": str} objects. When '
            "'conductor' names one of the --conductors entries, that label "
            "layer's texts are only matched against that conductor's own "
            "shapes, so a text sitting on a lower conductor's pin/label "
            "layer is never attributed to a different, upper-layer "
            "component whose geometry merely crosses over it in XY with no "
            "via joining them. 'conductor' is optional; omit it to keep the "
            "original any-layer behaviour, matching a text against every "
            "conductor/via layer present in a component. A matched text is "
            "reported in that component's 'labels' list. Omit --label-layers "
            "entirely to skip name/label/pin detection."
        ),
    )
    components_parser.add_argument(
        "--region",
        default=None,
        help=(
            "optional crop window as an inline JSON array of four micrometre "
            "coordinates [left, bottom, right, top] (e.g. '[0, 0, 100, "
            "100]'); every conductor/via/label-layer shape is clipped to "
            "this window before components are computed. Omit to report "
            "every shape on the given layers."
        ),
    )
    components_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to report on when the stream has more than one; omit "
            "to report every top cell"
        ),
    )
    components_parser.add_argument(
        "--probe",
        dest="probes",
        action="append",
        default=None,
        metavar="name=ID,layer=CONDUCTOR,x=UM,y=UM",
        help=(
            "ask which component one point lands on (issue #5552), as "
            "'name=<id>,layer=<conductor>,x=<um>,y=<um>'; repeatable, same "
            "spec grammar as `klt extract --probe`. CONDUCTOR is the 'name' "
            "of one --conductors entry (not a deck role -- this verb has no "
            "deck); x/y are micrometres in the top cell's frame and may be "
            "negative. Answered on the same connectivity graph this report "
            "is built from, so the JSON `probes[]` entry's `component_id` is "
            "always one of `components[].id` -- which is what separates two "
            "interleaved nets whose bounding boxes overlap. A point on empty "
            "space, on a different conductor only, or outside --region is "
            "`found: false`, not an error. Needs a single top cell in scope: "
            "pass --top for a multi-top stream. See "
            "docs/cli/components.md's 'Point-to-component probes'."
        ),
    )
    _add_format_arg(components_parser)
    components_parser.set_defaults(func=components_cmd.run)
