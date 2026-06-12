"""CLI entry point for manual controls.

Calls only ManualControlService — never calls BrokerClient,
RiskEngine, or OrderManager directly.

Usage:
    python -m src.app.control status
    python -m src.app.control pause-strategy --strategy-id my_strat
    python -m src.app.control flatten-all --exit-price 0.01
    python -m src.app.control lock-system
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all manual control commands."""
    parser = argparse.ArgumentParser(
        prog="control",
        description="Manual control CLI for the execution system. "
        "Routes all commands through ManualControlService.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -- status
    subparsers.add_parser("status", help="Show system status summary")

    # -- pause-strategy
    p = subparsers.add_parser("pause-strategy", help="Pause a strategy")
    p.add_argument("--strategy-id", required=True, help="Strategy ID to pause")

    # -- resume-strategy
    p = subparsers.add_parser("resume-strategy", help="Resume a paused strategy")
    p.add_argument("--strategy-id", required=True, help="Strategy ID to resume")

    # -- disable-symbol
    p = subparsers.add_parser("disable-symbol", help="Disable a symbol")
    p.add_argument("--symbol", required=True, help="Symbol to disable")

    # -- enable-symbol
    p = subparsers.add_parser("enable-symbol", help="Enable a disabled symbol")
    p.add_argument("--symbol", required=True, help="Symbol to enable")

    # -- reduce-only
    p = subparsers.add_parser("reduce-only", help="Set reduce-only mode")
    p.add_argument(
        "--strategy-id", default=None,
        help="Apply to specific strategy (omit for global)",
    )

    # -- flatten-position
    p = subparsers.add_parser("flatten-position", help="Flatten a specific position")
    p.add_argument("--position-id", required=True, help="Position UUID to flatten")
    p.add_argument("--exit-price", required=True, type=float, help="Exit price")

    # -- flatten-strategy
    p = subparsers.add_parser("flatten-strategy", help="Flatten all positions for a strategy")
    p.add_argument("--strategy-id", required=True, help="Strategy ID to flatten")
    p.add_argument("--exit-price", required=True, type=float, help="Exit price")

    # -- flatten-all
    p = subparsers.add_parser("flatten-all", help="Flatten all open positions")
    p.add_argument("--exit-price", required=True, type=float, help="Exit price")

    # -- cancel-order
    p = subparsers.add_parser("cancel-order", help="Cancel a specific order")
    p.add_argument("--order-id", required=True, help="Order UUID to cancel")

    # -- cancel-all
    subparsers.add_parser("cancel-all", help="Cancel all active orders")

    # -- lock-system
    subparsers.add_parser("lock-system", help="Lock the system — prevent all new orders")

    # -- show-risk
    subparsers.add_parser("show-risk", help="Show risk override state")

    # -- show-positions
    subparsers.add_parser("show-positions", help="Show open positions")

    # -- show-orders
    subparsers.add_parser("show-orders", help="Show active orders")

    # -- show-rejections
    subparsers.add_parser("show-rejections", help="Show recent rejections")

    return parser


def _print_result(result: object) -> None:
    """Pretty-print command results as JSON."""
    if isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result)


async def _run_command(
    service: "ManualControlService",  # noqa: F821 — forward ref for type hint
    args: argparse.Namespace,
) -> Optional[object]:
    """Dispatch parsed args to ManualControlService methods.

    Returns the result for printing.
    """
    from uuid import UUID

    cmd = args.command
    if cmd == "status":
        return service.status()
    elif cmd == "pause-strategy":
        return service.pause_strategy(args.strategy_id)
    elif cmd == "resume-strategy":
        return service.resume_strategy(args.strategy_id)
    elif cmd == "disable-symbol":
        return service.disable_symbol(args.symbol)
    elif cmd == "enable-symbol":
        return service.enable_symbol(args.symbol)
    elif cmd == "reduce-only":
        return service.reduce_only(args.strategy_id)
    elif cmd == "flatten-position":
        return service.flatten_position(UUID(args.position_id), args.exit_price)
    elif cmd == "flatten-strategy":
        return service.flatten_strategy(args.strategy_id, args.exit_price)
    elif cmd == "flatten-all":
        return service.flatten_all(args.exit_price)
    elif cmd == "cancel-order":
        return await service.cancel_order(UUID(args.order_id))
    elif cmd == "cancel-all":
        return await service.cancel_all()
    elif cmd == "lock-system":
        return service.lock_system()
    elif cmd == "show-risk":
        return service.show_risk()
    elif cmd == "show-positions":
        return service.show_positions()
    elif cmd == "show-orders":
        return service.show_orders()
    elif cmd == "show-rejections":
        return service.show_rejections()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        return None


def main(argv: Optional[list[str]] = None) -> None:
    """Entry point for the CLI.

    In production, ManualControlService would be constructed with
    real dependencies from the running system. For now this provides
    the CLI structure; the runner (Phase 15) will wire up the live
    service instance.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # NOTE: In production, the service would be obtained from the
    # running application context. This CLI module defines the
    # parser and dispatcher; the runner (Phase 15) will provide
    # the wiring.
    print(
        f"CLI parsed command: {args.command}. "
        "Full execution requires a running system context (Phase 15)."
    )


if __name__ == "__main__":
    main()
