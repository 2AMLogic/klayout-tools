"""``klt layout-plan execute`` command: run a ``klt.layout_plan.request/1``.

A thin shell over :func:`klayout_tools.layout_plan_execute.
execute_layout_plan_document`; it adds no placement, routing or geometry
logic. Output goes through the shared envelope helpers in :mod:`.output` --
see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/layout-plan-execute.md``):
    0 - plan executed; every net routed and mapped
    1 - application error: unreadable/invalid-JSON request, plan validation
        failure, or an execution failure -- returned by ``emit_error``
    2 - usage error (missing request path, bad --format value) -- from
        argparse
    3 - partial success: placed, but one or more nets unrouted or unmapped;
        the full payload is still emitted on stdout

``run_check`` (``klt layout-plan check``) is the read-only sibling: exit 0
compliant, 1 error, 3 when any plan-compliance check is a violation (see
``docs/cli/layout-plan-check.md``).
"""

import argparse
import json
import os

from ..layout_plan import LayoutPlanError
from ..layout_plan_check import check_layout_plan_document
from ..layout_plan_check import exit_code_for as check_exit_code_for
from ..layout_plan_execute import (
    LayoutPlanExecuteError,
    execute_layout_plan_document,
    exit_code_for,
)
from .output import emit_error, emit_success


def run_execute(args: argparse.Namespace) -> int:
    request_dir = os.path.dirname(os.path.abspath(args.request))
    try:
        with open(args.request, encoding="utf-8") as handle:
            request = json.load(handle)
    except OSError as exc:
        return emit_error(
            "layout-plan",
            f"could not read request '{args.request}': {exc}",
            args.format,
        )
    except json.JSONDecodeError as exc:
        return emit_error(
            "layout-plan",
            f"request '{args.request}' is not valid JSON: {exc}",
            args.format,
        )

    try:
        response = execute_layout_plan_document(request, request_dir=request_dir)
    except (LayoutPlanError, LayoutPlanExecuteError) as exc:
        return emit_error("layout-plan", str(exc), args.format)

    emit_success(response, args.format, _print_text)
    return exit_code_for(response)


def run_check(args: argparse.Namespace) -> int:
    request_dir = os.path.dirname(os.path.abspath(args.request))
    try:
        with open(args.request, encoding="utf-8") as handle:
            request = json.load(handle)
    except OSError as exc:
        return emit_error(
            "layout-plan",
            f"could not read request '{args.request}': {exc}",
            args.format,
        )
    except json.JSONDecodeError as exc:
        return emit_error(
            "layout-plan",
            f"request '{args.request}' is not valid JSON: {exc}",
            args.format,
        )

    try:
        response = check_layout_plan_document(
            request,
            args.layout,
            request_dir=request_dir,
            cell_name=args.cell,
            tolerance_um=args.tolerance_um,
        )
    except LayoutPlanError as exc:
        return emit_error("layout-plan", str(exc), args.format)

    emit_success(response, args.format, _print_check_text)
    return check_exit_code_for(response)


def _print_check_text(response: dict) -> None:
    print(f"cell_name: {response['cell_name']}")
    print(f"status: {response['status']}")
    print(response["summary"])
    for violation in response["violations"]:
        subject = (
            violation.get("pair") or violation.get("group") or violation.get("row")
        )
        print(
            f"  VIOLATION {violation['check']} {subject}: "
            f"expected {violation['expected']}, observed {violation['observed']}"
        )


def _print_text(response: dict) -> None:
    print(f"cell_name: {response['cell_name']}")
    print(f"gds_path: {response['gds_path']}")

    groups = response.get("device_groups") or []
    if groups:
        print()
        print("device_groups:")
        for group in groups:
            print(f"  {group['id']} ({group.get('generator', '-')})")

    nets = response.get("nets") or []
    if nets:
        print()
        print("nets:")
        for net in nets:
            status = "routed" if net.get("routed") else "UNROUTED"
            print(f"  {net['net']}  {status}")

    for key in ("unrouted_nets", "unmapped_netlist_nets", "warnings"):
        items = response.get(key) or []
        if items:
            print()
            print(f"{key}:")
            for item in items:
                print(f"  {item if isinstance(item, str) else item.get('net', item)}")
