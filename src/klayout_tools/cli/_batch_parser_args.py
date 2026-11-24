"""Batch submission arguments shared by sim, pex and characterize."""

from __future__ import annotations

import argparse


def add_wait_for_capacity_arg(parser: argparse.ArgumentParser, tail: str) -> None:
    """Register the shared capacity-wait option with verb-specific help."""
    parser.add_argument(
        "--wait-for-capacity",
        dest="wait_for_capacity",
        metavar="SECONDS",
        type=float,
        default=None,
        help=(
            "with --backend batch, wait up to SECONDS (>= 0) for fleet "
            "capacity when the launch is refused (batch_no_capacity / "
            "batch_concurrency_cap), re-launching the same job with jittered "
            "backoff; overrides the request's `batch.capacity_wait_s`. 0 "
            "means one attempt. " + tail
        ),
    )
