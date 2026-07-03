"""CLI entry point for botflow.

Usage:
    botflow run --workspace PATH --host IP --port NUM
    botflow set llm-key <KEY>
    botflow set mcp-key <KEY>
"""

import argparse
import asyncio
import sys

from botflow.config import load_config
from botflow.workspace import get_workspace_path, init_workspace


def main() -> None:
    parser = argparse.ArgumentParser(prog="botflow", description="AI middleware platform")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # botflow run
    run_parser = subparsers.add_parser("run", help="Start the main service (foreground)")
    run_parser.add_argument("--workspace", default=None, help="Workspace path (default: ~/.botflow/)")
    run_parser.add_argument("--host", default="0.0.0.0", help="HTTP server host")
    run_parser.add_argument("--port", type=int, default=8080, help="HTTP server port")

    # botflow set
    set_parser = subparsers.add_parser("set", help="Set API keys")
    set_parser.add_argument("key_type", choices=["llm-key", "mcp-key"], help="Key type")
    set_parser.add_argument("key_value", help="The key value")

    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "set":
        _cmd_set(args)
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_run(args: argparse.Namespace) -> None:
    """Start the botflow service."""
    workspace = get_workspace_path(args.workspace)
    init_workspace(workspace)
    config = load_config(workspace)

    from botflow.core import start_service
    start_service(
        workspace=workspace,
        host=args.host,
        port=args.port,
        config=config,
    )


async def _cmd_set_async(args: argparse.Namespace) -> None:
    """Set API keys in the database (async)."""
    from botflow.storage.db import Database

    workspace = get_workspace_path(None)
    init_workspace(workspace)

    db = Database(workspace / "data" / "botflow.db")
    try:
        await db.initialize()

        key_name = "llm_key" if args.key_type == "llm-key" else "mcp_key"
        await db.set_config(key_name, args.key_value)

        print(f"[botflow] {args.key_type} saved to database.")
    finally:
        await db.close()


def _cmd_set(args: argparse.Namespace) -> None:
    """Set API keys in the database."""
    asyncio.run(_cmd_set_async(args))
