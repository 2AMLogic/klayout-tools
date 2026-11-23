"""CLI for digest and local loader verification of a PDK's OSDI model set."""

from ..pdk import PdkNotFoundError
from ..pdk_osdi import OsdiError, verify_osdi
from .output import emit_error, emit_success


def add_parser(subparsers, add_pdk_args, add_format_arg):
    parser = subparsers.add_parser(
        "osdi", help="verify the installed ngspice OSDI model set"
    )
    add_pdk_args(parser)
    parser.add_argument(
        "--expect",
        metavar="FILE",
        help="JSON map of model basenames to pinned SHA-256 digests",
    )
    parser.add_argument(
        "--load-probe",
        action="store_true",
        help="verify that local ngspice registers each declared model",
    )
    add_format_arg(parser)
    parser.set_defaults(func=run)


def run(args):
    try:
        report = verify_osdi(args.pdk, args.pdk_root, args.expect, args.load_probe)
    except PdkNotFoundError:
        return emit_error(
            "pdk osdi",
            "Could not resolve the requested PDK. Use klt pdk find for local paths.",
            args.format,
            code="pdk_not_found",
        )
    except OsdiError as exc:
        return emit_error(
            "pdk osdi", str(exc), args.format, code=exc.code, details=exc.details
        )
    emit_success(report, args.format, _text)
    return 0


def _text(report):
    identity = report["identity"]
    print(f"{identity['variant']} OSDI models: {len(identity['models'])}")
    for row in identity["models"]:
        print(f"{row['name']}  {row['sha256']}")
    if report["load_probe"]:
        print(
            f"Verified registration on ngspice {report['load_probe']['engine_version']}"
        )
