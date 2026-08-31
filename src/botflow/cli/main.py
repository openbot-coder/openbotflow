"""CLI entry point for botflow.

Subcommands:
    run          Start the HTTP LLM Proxy service (foreground)
    stop         Stop the service
    restart      Restart the service
    status       Show service status
    logs         View service logs
    set          Set a config value
    get          Get a config value
    config       List all config
    cleanup      Clean up old call_logs
    provider     Manage providers
    model        Manage models
    group        Manage groups
    version      Show version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from botflow import __version__
from botflow.cli.service import (
    clear_pid,
    get_status,
    read_pid,
    restart_service,
    stop_service,
    tail_logs,
    write_pid,
)


def _get_workspace(args) -> Path:
    """Resolve workspace path from args or default."""
    from botflow.workspace import get_workspace_path
    return get_workspace_path(getattr(args, "workspace", None))


def _init_workspace_db(args):
    """Common CLI pattern: resolve workspace, ensure dirs, return (workspace, Database)."""
    from botflow.workspace import init_workspace
    from botflow.storage.db import Database

    workspace = _get_workspace(args)
    init_workspace(workspace)
    return workspace, Database(workspace / "data" / "botflow.db")


# ---------------------------------------------------------------------------
# Service commands
# ---------------------------------------------------------------------------


def cmd_run(args):
    # UNCOVERED: 前台阻塞式服务启动（asyncio.run(uvicorn.serve)）——只能在真实服务进程触发，
    # 单元测试中调用会阻塞测试进程，故不可覆盖。
    """Start the botflow HTTP LLM Proxy service."""
    from botflow.common.logger import setup_logging
    from botflow.config import load_config
    from botflow.workspace import init_workspace

    workspace = _get_workspace(args)
    init_workspace(workspace)

    config_path = getattr(args, "config", None)
    if config_path:
        from pathlib import Path as P
        config = load_config(P(config_path).parent)
    else:
        config = load_config(workspace)

    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    setup_logging(workspace / "logs", config.log_level)

    # Write PID
    import os
    write_pid(workspace, os.getpid())

    import uvicorn
    from botflow.core import app, create_app

    async def _run():
        await create_app(workspace, config)
        server_config = uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level.lower(),
            loop="asyncio",
            forwarded_allow_ips="*",
        )
        server = uvicorn.Server(server_config)
        await server.serve()

    try:
        asyncio.run(_run())
    finally:
        clear_pid(workspace)


def cmd_stop(args):
    """Stop the botflow service."""
    workspace = _get_workspace(args)
    result = stop_service(workspace)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)


def cmd_restart(args):
    """Restart the botflow service."""
    workspace = _get_workspace(args)
    result = restart_service(
        workspace,
        host=args.host or "0.0.0.0",
        port=args.port or 8080,
        config_path=getattr(args, "config", None),
    )
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)


def cmd_status(args):
    """Show botflow service status."""
    workspace = _get_workspace(args)
    port = args.port or 8080
    status = get_status(workspace, port)

    pid = status.get("pid")
    running = status.get("running", False)
    health = status.get("health")

    print(f"PID:      {pid or 'N/A'}")
    print(f"Running:  {'yes' if running else 'no'}")
    if health:
        print(f"Health:   {json.dumps(health)}")
    else:
        print("Health:   (not reachable)")


def cmd_logs(args):
    """View botflow service logs."""
    workspace = _get_workspace(args)
    output = tail_logs(workspace, lines=args.lines or 50)
    print(output, end="")


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


def cmd_set(args):
    """Set a config key-value pair."""
    workspace, db = _init_workspace_db(args)

    async def _set():
        async with db:
            await db.set_config(args.key, args.value)
            # Auto-register llm_key in the api_keys table for immediate auth use.
            if args.key == "llm_key" and args.value:
                existing = await db.list_api_keys()
                if not existing:
                    await db.create_api_key(args.value, label="legacy:llm_key")
            print(f"Config set: {args.key} = {args.value}")

    asyncio.run(_set())


def cmd_get(args):
    """Get a config value."""
    workspace, db = _init_workspace_db(args)

    async def _get():
        async with db:
            val = await db.get_config(args.key)
            if val:
                print(f"{args.key} = {val}")
            else:
                print(f"Config '{args.key}' not set.")
                sys.exit(1)

    asyncio.run(_get())


def cmd_config(args):
    """List all config values."""
    workspace, db = _init_workspace_db(args)

    async def _config():
        async with db:
            rows = await db.execute_read(
                "SELECT key, value FROM config ORDER BY key"
            )
            if not rows:
                print("No config values set.")
                return
            for row in rows:
                print(f"{row['key']} = {row['value']}")

    asyncio.run(_config())


# ---------------------------------------------------------------------------
# Cleanup command
# ---------------------------------------------------------------------------


def cmd_cleanup(args):
    """Clean up old call_logs."""
    workspace, db = _init_workspace_db(args)

    async def _cleanup():
        from botflow.storage.daily_summary import purge_old_call_logs
        from botflow.config import load_config

        async with db:
            config = load_config(workspace)
            days = args.days or config.call_logs_retention_days
            deleted = await purge_old_call_logs(db) if args.days is None else await db.delete_old_call_logs(
                (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            )
            print(f"Cleaned up {deleted} records older than {days} days.")

    asyncio.run(_cleanup())


# ---------------------------------------------------------------------------
# API key commands (multi-tenant client keys)
# ---------------------------------------------------------------------------


def cmd_apikey(args):
    """Manage client API keys."""
    workspace, db = _init_workspace_db(args)
    subcmd = args.apikey_action

    async def _apikey():
        async with db:
            if subcmd == "list":
                keys = await db.list_api_keys()
                if not keys:
                    print("No API keys registered.")
                    return
                for k in keys:
                    status = "enabled" if k.is_enabled else "disabled"
                    print(f"  [{k.id}] {k.key_hash[:8]}… (label={k.label or '-'}) [{status}]")
            elif subcmd == "add":
                key = await db.create_api_key(args.key, label=args.label or "")
                masked = args.key[:4] + "…" + args.key[-4:] if len(args.key) > 8 else "****"
                print(f"API key created: id={key.id} (raw key shown once: {masked})")
            elif subcmd == "disable":
                ok = await db.set_api_key_enabled(args.id, False)
                print(f"API key {args.id} disabled." if ok else f"API key {args.id} not found.")
            elif subcmd == "enable":
                ok = await db.set_api_key_enabled(args.id, True)
                print(f"API key {args.id} enabled." if ok else f"API key {args.id} not found.")
            elif subcmd == "update":
                label = args.label if args.label is not None else None
                enabled = {"true": True, "false": False}.get(args.enabled) if args.enabled is not None else None
                ok = await db.update_api_key(args.id, label=label, is_enabled=enabled)
                if ok:
                    parts = []
                    if label is not None:
                        parts.append(f"label={label}")
                    if enabled is not None:
                        parts.append(f"enabled={enabled}")
                    print(f"API key {args.id} updated: {', '.join(parts)}.")
                else:
                    print(f"API key {args.id} not found.")
            elif subcmd == "delete":
                ok = await db.delete_api_key(args.id)
                print(f"API key {args.id} deleted." if ok else f"API key {args.id} not found.")

    asyncio.run(_apikey())


# ---------------------------------------------------------------------------
# Daily summary command
# ---------------------------------------------------------------------------


def cmd_summary(args):
    """Run the daily conversation summary + raw-session compression."""
    workspace, db = _init_workspace_db(args)

    async def _summary():
        from botflow.config import load_config
        from botflow.storage.daily_summary import run_daily_summary

        config = load_config(workspace)
        async with db:
            day = args.day
            await run_daily_summary(db, day=day)
            summary = await db.get_daily_summary(day) if day else None
            if summary:
                print(summary.summary_md or "(no wiki text; stats only)")

    asyncio.run(_summary())


# ---------------------------------------------------------------------------
# Provider commands
# ---------------------------------------------------------------------------


def cmd_provider(args):
    """Manage LLM providers."""
    workspace, db = _init_workspace_db(args)
    subcmd = args.provider_action

    async def _provider():
        from botflow.storage.models import Provider

        async with db:
            if subcmd == "list":
                providers = await db.list_providers(enabled_only=args.enabled)
                if not providers:
                    print("No providers found.")
                    return
                for p in providers:
                    status = "enabled" if p.is_enabled else "disabled"
                    print(f"  [{p.id}] {p.name} ({p.provider_type}) [{status}]")

            elif subcmd == "get":
                p = await db.get_provider(args.id)
                if not p:
                    print(f"Provider {args.id} not found.")
                    sys.exit(1)
                print(json.dumps(p.model_dump(mode="json"), indent=2, default=str))

            elif subcmd == "add":
                pid = await db.create_provider(Provider(
                    name=args.name,
                    provider_type=args.type,
                    api_key=args.api_key or "",
                    base_url=args.base_url or "",
                ))
                print(f"Provider created: id={pid}")

            elif subcmd == "update":
                updates = {}
                if args.name:
                    updates["name"] = args.name
                if args.api_key:
                    updates["api_key"] = args.api_key
                if args.base_url:
                    updates["base_url"] = args.base_url
                if args.enabled is not None:
                    updates["is_enabled"] = args.enabled
                if not updates:
                    print("No updates specified.")
                    sys.exit(1)
                await db.update_provider(args.id, updates)
                print(f"Provider {args.id} updated.")

            elif subcmd == "delete":
                await db.delete_provider(args.id)
                print(f"Provider {args.id} deleted.")

    asyncio.run(_provider())


# ---------------------------------------------------------------------------
# Model commands
# ---------------------------------------------------------------------------


def cmd_model(args):
    """Manage LLM models."""
    workspace, db = _init_workspace_db(args)
    subcmd = args.model_action

    async def _model():
        from botflow.storage.models import Model

        async with db:
            if subcmd == "list":
                models = await db.list_models(enabled_only=args.enabled)
                if not models:
                    print("No models found.")
                    return
                for m in models:
                    status = "enabled" if m.is_enabled else "disabled"
                    fmt = f" api_format={m.api_format}" if m.api_format else ""
                    print(f"  [{m.id}] {m.name} (provider={m.provider_id}){fmt} [{status}]")

            elif subcmd == "get":
                m = await db.get_model(args.id)
                if not m:
                    print(f"Model {args.id} not found.")
                    sys.exit(1)
                print(json.dumps(m.model_dump(mode="json"), indent=2, default=str))

            elif subcmd == "add":
                extra_config = {}
                proxy = getattr(args, "proxy", None)
                if proxy:
                    extra_config["proxy"] = proxy
                mid = await db.create_model(Model(
                    name=args.name,
                    provider_id=args.provider_id,
                    display_name=args.display_name or "",
                    api_format=getattr(args, "api_format", "") or "",
                    max_retries=args.max_retries or 3,
                    cooldown_seconds=args.cooldown or 60,
                    extra_config=extra_config,
                ))
                print(f"Model created: id={mid}")
                if proxy:
                    print(f"  proxy={proxy}")

            elif subcmd == "update":
                updates = {}
                if args.name:
                    updates["name"] = args.name
                if args.display_name:
                    updates["display_name"] = args.display_name
                if getattr(args, "api_format", None) is not None:
                    updates["api_format"] = args.api_format
                if args.max_retries is not None:
                    updates["max_retries"] = args.max_retries
                if args.cooldown is not None:
                    updates["cooldown_seconds"] = args.cooldown
                if args.enabled is not None:
                    updates["is_enabled"] = args.enabled
                if getattr(args, "proxy", None) is not None:
                    m = await db.get_model(args.id)
                    config = dict(m.extra_config) if m else {}
                    config["proxy"] = args.proxy
                    updates["extra_config"] = config
                if not updates:
                    print("No updates specified.")
                    sys.exit(1)
                await db.update_model(args.id, updates)
                print(f"Model {args.id} updated.")

            elif subcmd == "delete":
                await db.delete_model(args.id)
                print(f"Model {args.id} deleted.")

            elif subcmd == "sync":
                provider_id = args.provider_id
                if provider_id:
                    providers = [provider_id]
                else:
                    provs = await db.list_providers(enabled_only=True)
                    providers = [p.id for p in provs]

                if not providers:
                    print("No enabled providers found.")
                    sys.exit(1)

                from botflow.core import sync_models_from_provider

                total_added = 0
                total_skipped = 0
                for pid in providers:
                    provider = await db.get_provider(pid)
                    pname = provider.name if provider else f"#{pid}"
                    print(f"Syncing models from provider [{pid}] {pname} ...")
                    try:
                        result = await sync_models_from_provider(pid, db=db)
                        added = result.get("added", 0)
                        skipped = result.get("skipped", 0)
                        errors = result.get("errors", [])
                        total_added += added
                        total_skipped += skipped
                        parts = []
                        if added:
                            parts.append(f"added={added}")
                        if skipped:
                            parts.append(f"skipped={skipped}")
                        if errors:
                            parts.append(f"errors={errors}")
                        print(f"  Result: {', '.join(parts) if parts else 'no changes'}")
                    except Exception as e:
                        print(f"  Error: {e}")

                print(f"\nSync complete: total added={total_added} skipped={total_skipped}")

    asyncio.run(_model())


# ---------------------------------------------------------------------------
# Group commands
# ---------------------------------------------------------------------------


def cmd_group(args):
    """Manage model groups."""
    workspace, db = _init_workspace_db(args)
    subcmd = args.group_action

    async def _group():
        from botflow.storage.models import ModelGroup

        async with db:
            if subcmd == "list":
                groups = await db.list_groups(enabled_only=args.enabled)
                if not groups:
                    print("No groups found.")
                    return
                for g in groups:
                    status = "enabled" if g.is_enabled else "disabled"
                    fb = f" fallback→{g.fallback_group_id}" if g.fallback_group_id else ""
                    print(f"  [{g.id}] {g.name} [{status}]{fb}")

            elif subcmd == "get":
                g = await db.get_group(args.id)
                if not g:
                    print(f"Group {args.id} not found.")
                    sys.exit(1)
                print(json.dumps(g.model_dump(mode="json"), indent=2, default=str))
                models = await db.get_group_models(args.id, enabled_only=False)
                if models:
                    print("\nModels:")
                    for gm in models:
                        print(f"  [{gm.model_id}] {gm.model_name} (weight={gm.weight}, provider={gm.provider_name})")

            elif subcmd == "add":
                gid = await db.create_group(ModelGroup(
                    name=args.name,
                    description=args.description or "",
                ))
                print(f"Group created: id={gid}")

            elif subcmd == "update":
                updates = {}
                if args.name:
                    updates["name"] = args.name
                if args.description:
                    updates["description"] = args.description
                if args.enabled is not None:
                    updates["is_enabled"] = args.enabled
                if args.fallback is not None:
                    updates["fallback_group_id"] = args.fallback
                if not updates:
                    print("No updates specified.")
                    sys.exit(1)
                await db.update_group(args.id, updates)
                print(f"Group {args.id} updated.")

            elif subcmd == "delete":
                await db.delete_group(args.id)
                print(f"Group {args.id} deleted.")

            elif subcmd == "add-model":
                await db.add_model_to_group(args.id, args.model_id, args.weight or 1.0)
                print(f"Model {args.model_id} added to group {args.id} (weight={args.weight or 1.0}).")

            elif subcmd == "remove-model":
                await db.remove_model_from_group(args.id, args.model_id)
                print(f"Model {args.model_id} removed from group {args.id}.")

            elif subcmd == "set-weight":
                await db.update_model_weight(args.id, args.model_id, args.weight)
                print(f"Model {args.model_id} weight in group {args.id} set to {args.weight}.")

    asyncio.run(_group())


# ---------------------------------------------------------------------------
# Stats commands
# ---------------------------------------------------------------------------


def cmd_stats(args):
    """Show statistics."""
    workspace, db = _init_workspace_db(args)
    subcmd = args.stats_action

    async def _stats():
        async with db:
            if subcmd == "cost":
                days = args.days or 30
                summary = await db.get_cost_summary(days)
                if not summary:
                    print("No cost data found.")
                    return
                print(f"Cost summary (last {days} days):")
                for row in summary:
                    print(f"  {row['day']}: {row['total_calls']} calls, {row['total_tokens']} tokens, ${row['total_cost']:.4f}")

            elif subcmd == "model":
                stats = await db.get_model_stats(args.id)
                if not stats:
                    print(f"No stats for model {args.id}.")
                    return
                print(json.dumps(stats.model_dump(mode="json"), indent=2, default=str))

            elif subcmd == "group":
                stats = await db.get_group_stats(args.id)
                if not stats:
                    print(f"No stats for group {args.id}.")
                    return
                print(json.dumps(stats.model_dump(mode="json"), indent=2, default=str))

            elif subcmd == "recent":
                limit = args.limit or 20
                logs = await db.query_call_logs(limit=limit)
                if not logs:
                    print("No call logs found.")
                    return
                print(f"Recent {len(logs)} calls:")
                for log_entry in logs:
                    model = log_entry.model_id or "?"
                    dur = f"{log_entry.duration_ms}ms" if log_entry.duration_ms else "?"
                    tok = log_entry.total_tokens or 0
                    print(f"  [{log_entry.status}] model={model} {dur} {tok}tokens {log_entry.created_at}")

    asyncio.run(_stats())


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def cmd_version(args):
    print(f"botflow v{__version__}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="botflow",
        description="Botflow — AI Model Routing Gateway",
    )
    parser.add_argument("--workspace", help="Workspace directory (default: ~/.botflow)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # --- run ---
    p_run = sub.add_parser("run", help="Start the HTTP/MCP service")
    p_run.add_argument("--host", default="0.0.0.0", help="Bind host")
    p_run.add_argument("--port", type=int, default=8080, help="Bind port")
    p_run.add_argument("--config", help="Path to config .env file")
    p_run.set_defaults(func=cmd_run)

    # --- stop ---
    p_stop = sub.add_parser("stop", help="Stop the service")
    p_stop.set_defaults(func=cmd_stop)

    # --- restart ---
    p_restart = sub.add_parser("restart", help="Restart the service")
    p_restart.add_argument("--host", default=None, help="Bind host")
    p_restart.add_argument("--port", type=int, default=None, help="Bind port")
    p_restart.add_argument("--config", help="Path to config .env file")
    p_restart.set_defaults(func=cmd_restart)

    # --- status ---
    p_status = sub.add_parser("status", help="Show service status")
    p_status.add_argument("--port", type=int, default=8080, help="Port to check health")
    p_status.set_defaults(func=cmd_status)

    # --- logs ---
    p_logs = sub.add_parser("logs", help="View service logs")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    p_logs.set_defaults(func=cmd_logs)

    # --- set ---
    p_set = sub.add_parser("set", help="Set a config value")
    p_set.add_argument("key", help="Config key")
    p_set.add_argument("value", help="Config value")
    p_set.set_defaults(func=cmd_set)

    # --- get ---
    p_get = sub.add_parser("get", help="Get a config value")
    p_get.add_argument("key", help="Config key")
    p_get.set_defaults(func=cmd_get)

    # --- config ---
    p_config = sub.add_parser("config", help="List all config values")
    p_config.set_defaults(func=cmd_config)

    # --- cleanup ---
    p_cleanup = sub.add_parser("cleanup", help="Clean up old call_logs")
    p_cleanup.add_argument("--days", type=int, default=180, help="Retention in days (default: 180)")
    p_cleanup.set_defaults(func=cmd_cleanup)

    # --- apikey ---
    p_apikey = sub.add_parser("apikey", help="Manage client API keys (multi-tenant)")
    ak_sub = p_apikey.add_subparsers(dest="apikey_action", help="API key actions")
    ak_list = ak_sub.add_parser("list", help="List API keys (hashes only)")
    ak_list.set_defaults(func=cmd_apikey)
    ak_add = ak_sub.add_parser("add", help="Add a client API key")
    ak_add.add_argument("key", help="The raw API key string")
    ak_add.add_argument("--label", default="", help="Human label")
    ak_add.set_defaults(func=cmd_apikey)
    ak_dis = ak_sub.add_parser("disable", help="Disable an API key")
    ak_dis.add_argument("id", type=int, help="API key id")
    ak_dis.set_defaults(func=cmd_apikey)
    ak_en = ak_sub.add_parser("enable", help="Enable an API key")
    ak_en.add_argument("id", type=int, help="API key id")
    ak_en.set_defaults(func=cmd_apikey)
    ak_up = ak_sub.add_parser("update", help="Update an API key")
    ak_up.add_argument("id", type=int, help="API key id")
    ak_up.add_argument("--label", default=None, help="New label")
    ak_up.add_argument("--enabled", default=None, choices=["true", "false"], help="Enable or disable the key")
    ak_up.set_defaults(func=cmd_apikey)
    ak_del = ak_sub.add_parser("delete", help="Delete an API key")
    ak_del.add_argument("id", type=int, help="API key id")
    ak_del.set_defaults(func=cmd_apikey)

    # --- summary ---
    p_summary = sub.add_parser("summary", help="Run daily conversation summary + raw-session compression")
    p_summary.add_argument("--day", default=None, help="Target day YYYY-MM-DD (default: yesterday)")
    p_summary.set_defaults(func=cmd_summary)

    # --- provider ---
    p_prov = sub.add_parser("provider", help="Manage LLM providers")
    prov_sub = p_prov.add_subparsers(dest="provider_action", help="Provider actions")

    prov_list = prov_sub.add_parser("list", help="List providers")
    prov_list.add_argument("--enabled", action="store_true", help="Show only enabled")
    prov_list.set_defaults(func=cmd_provider)

    prov_get = prov_sub.add_parser("get", help="Get provider details")
    prov_get.add_argument("id", type=int, help="Provider ID")
    prov_get.set_defaults(func=cmd_provider)

    prov_add = prov_sub.add_parser("add", help="Add a provider")
    prov_add.add_argument("name", help="Provider name")
    prov_add.add_argument("--type", required=True, help="Provider type (openai, anthropic, etc)")
    prov_add.add_argument("--api-key", help="API key")
    prov_add.add_argument("--base-url", help="Base URL")
    prov_add.set_defaults(func=cmd_provider)

    prov_update = prov_sub.add_parser("update", help="Update a provider")
    prov_update.add_argument("id", type=int, help="Provider ID")
    prov_update.add_argument("--name", help="New name")
    prov_update.add_argument("--api-key", help="New API key")
    prov_update.add_argument("--base-url", help="New base URL")
    prov_update.add_argument("--enabled", type=_bool_arg, help="Enable/disable (true/false)")
    prov_update.set_defaults(func=cmd_provider)

    prov_del = prov_sub.add_parser("delete", help="Delete a provider")
    prov_del.add_argument("id", type=int, help="Provider ID")
    prov_del.set_defaults(func=cmd_provider)

    # --- model ---
    p_model = sub.add_parser("model", help="Manage LLM models")
    model_sub = p_model.add_subparsers(dest="model_action", help="Model actions")

    model_list = model_sub.add_parser("list", help="List models")
    model_list.add_argument("--enabled", action="store_true", help="Show only enabled")
    model_list.set_defaults(func=cmd_model)

    model_get = model_sub.add_parser("get", help="Get model details")
    model_get.add_argument("id", type=int, help="Model ID")
    model_get.set_defaults(func=cmd_model)

    model_add = model_sub.add_parser("add", help="Add a model")
    model_add.add_argument("name", help="Model name (as passed to provider)")
    model_add.add_argument("--provider-id", type=int, required=True, help="Provider ID")
    model_add.add_argument("--display-name", help="Display name")
    model_add.add_argument("--api-format", help="Per-model SDK override (openai/deepseek/anthropic/google/azure/ollama/vllm; empty=use provider type)")
    model_add.add_argument("--max-retries", type=int, help="Max retries (default: 3)")
    model_add.add_argument("--cooldown", type=int, help="Cooldown seconds (default: 60)")
    model_add.add_argument("--proxy", help="HTTP proxy URL for this model (e.g. http://127.0.0.1:7890)")
    model_add.set_defaults(func=cmd_model)

    model_update = model_sub.add_parser("update", help="Update a model")
    model_update.add_argument("id", type=int, help="Model ID")
    model_update.add_argument("--name", help="New model name")
    model_update.add_argument("--display-name", help="New display name")
    model_update.add_argument("--api-format", help="Per-model SDK override (empty to clear)")
    model_update.add_argument("--max-retries", type=int, help="Max retries")
    model_update.add_argument("--cooldown", type=int, help="Cooldown seconds")
    model_update.add_argument("--enabled", type=_bool_arg, help="Enable/disable")
    model_update.add_argument("--proxy", help="HTTP proxy URL (empty to clear)")
    model_update.set_defaults(func=cmd_model)

    model_del = model_sub.add_parser("delete", help="Delete a model")
    model_del.add_argument("id", type=int, help="Model ID")
    model_del.set_defaults(func=cmd_model)

    model_sync = model_sub.add_parser("sync", help="Sync models from upstream providers")
    model_sync.add_argument("--provider-id", type=int, help="Sync specific provider only (default: all enabled)")
    model_sync.set_defaults(func=cmd_model)

    # --- group ---
    p_group = sub.add_parser("group", help="Manage model groups")
    group_sub = p_group.add_subparsers(dest="group_action", help="Group actions")

    grp_list = group_sub.add_parser("list", help="List groups")
    grp_list.add_argument("--enabled", action="store_true", help="Show only enabled")
    grp_list.set_defaults(func=cmd_group)

    grp_get = group_sub.add_parser("get", help="Get group details")
    grp_get.add_argument("id", type=int, help="Group ID")
    grp_get.set_defaults(func=cmd_group)

    grp_add = group_sub.add_parser("add", help="Add a group")
    grp_add.add_argument("name", help="Group name")
    grp_add.add_argument("--description", help="Description")
    grp_add.set_defaults(func=cmd_group)

    grp_update = group_sub.add_parser("update", help="Update a group")
    grp_update.add_argument("id", type=int, help="Group ID")
    grp_update.add_argument("--name", help="New name")
    grp_update.add_argument("--description", help="New description")
    grp_update.add_argument("--enabled", type=_bool_arg, help="Enable/disable")
    grp_update.add_argument("--fallback", type=int, help="Fallback group ID")
    grp_update.set_defaults(func=cmd_group)

    grp_del = group_sub.add_parser("delete", help="Delete a group")
    grp_del.add_argument("id", type=int, help="Group ID")
    grp_del.set_defaults(func=cmd_group)

    grp_add_model = group_sub.add_parser("add-model", help="Add model to group")
    grp_add_model.add_argument("id", type=int, help="Group ID")
    grp_add_model.add_argument("model_id", type=int, help="Model ID")
    grp_add_model.add_argument("--weight", type=float, help="Weight (default: 1.0)")
    grp_add_model.set_defaults(func=cmd_group)

    grp_rm_model = group_sub.add_parser("remove-model", help="Remove model from group")
    grp_rm_model.add_argument("id", type=int, help="Group ID")
    grp_rm_model.add_argument("model_id", type=int, help="Model ID")
    grp_rm_model.set_defaults(func=cmd_group)

    grp_set_w = group_sub.add_parser("set-weight", help="Set model weight in group")
    grp_set_w.add_argument("id", type=int, help="Group ID")
    grp_set_w.add_argument("model_id", type=int, help="Model ID")
    grp_set_w.add_argument("weight", type=float, help="New weight")
    grp_set_w.set_defaults(func=cmd_group)

    # --- stats ---
    p_stats = sub.add_parser("stats", help="Show statistics")
    stats_sub = p_stats.add_subparsers(dest="stats_action", help="Stats actions")

    stats_cost = stats_sub.add_parser("cost", help="Cost summary")
    stats_cost.add_argument("--days", type=int, default=30, help="Lookback days")
    stats_cost.set_defaults(func=cmd_stats)

    stats_model = stats_sub.add_parser("model", help="Model stats")
    stats_model.add_argument("id", type=int, help="Model ID")
    stats_model.set_defaults(func=cmd_stats)

    stats_group = stats_sub.add_parser("group", help="Group stats")
    stats_group.add_argument("id", type=int, help="Group ID")
    stats_group.set_defaults(func=cmd_stats)

    stats_recent = stats_sub.add_parser("recent", help="Recent calls")
    stats_recent.add_argument("-n", "--limit", type=int, default=20, help="Max entries")
    stats_recent.set_defaults(func=cmd_stats)

    # --- version ---
    p_version = sub.add_parser("version", help="Show version")
    p_version.set_defaults(func=cmd_version)

    return parser


def _bool_arg(v: str) -> bool:
    """Parse a boolean CLI argument."""
    if v.lower() in ("true", "1", "yes"):
        return True
    if v.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {v!r}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
