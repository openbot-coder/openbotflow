"""Daily conversation summary + compressed raw-session retention.

Runs once per day (driven by an asyncio background task in core.py):

  1. Aggregate the day's call logs into stats (usage, errors, top models).
  2. Ask an LLM to produce an "LLM Wiki" markdown summary of the day's
     conversations (themes, recurring questions, failure patterns).
  3. Compress the raw session records with gzip and retain them for a
     rolling window (raw_session_retention_days, default 7).
  4. Purge detailed call-log fields older than call_log_detail_days (default 1),
     keeping only the stats columns.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

from loguru import logger

from botflow.config import get_config
from botflow.router import GroupRouter
from botflow.storage.db import Database

# Maximum number of call logs fed to the summary LLM prompt (avoid huge payloads).
_MAX_SAMPLE_FOR_SUMMARY = 200


def _yesterday_day(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def build_stats(logs: list) -> dict:
    """Aggregate usage/error stats from a day's call logs."""
    total = len(logs)
    errors = [l for l in logs if l.status == "error"]
    by_model: dict[str, int] = {}
    by_key: dict[int, int] = {}
    prompt_tokens = completion_tokens = total_tokens = 0
    cost = 0.0
    for l in logs:
        if l.model_id:
            by_model[str(l.model_id)] = by_model.get(str(l.model_id), 0) + 1
        if l.api_key_id is not None:
            by_key[str(l.api_key_id)] = by_key.get(str(l.api_key_id), 0) + 1
        prompt_tokens += l.prompt_tokens or 0
        completion_tokens += l.completion_tokens or 0
        total_tokens += l.total_tokens or 0
        cost += l.cost or 0.0
    error_types: dict[str, int] = {}
    for e in errors:
        et = e.error_type or "unknown"
        error_types[et] = error_types.get(et, 0) + 1
    return {
        "total_calls": total,
        "error_calls": len(errors),
        "error_rate": round(len(errors) / total, 4) if total else 0.0,
        "by_model": by_model,
        "by_api_key": by_key,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
        },
        "cost": round(cost, 6),
        "error_types": error_types,
    }


def _build_summary_prompt(logs: list, stats: dict) -> str:
    """Build a compact prompt from a sample of the day's conversations."""
    samples = []
    for l in logs[:_MAX_SAMPLE_FOR_SUMMARY]:
        try:
            req = json.loads(l.request_body) if l.request_body else {}
        except Exception:
            req = {}
        messages = req.get("messages") or []
        last_user = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    last_user = content
                break
        if last_user:
            samples.append(f"- {last_user[:300]}")
    sample_block = "\n".join(samples) if samples else "(no user messages captured)"
    return (
        "You are maintaining an 'LLM Wiki' — a daily knowledge log of how this "
        "proxy's users converse with the models. Below are today's usage stats "
        "and a sample of user prompts.\n\n"
        f"## Usage stats\n```json\n{json.dumps(stats, ensure_ascii=False)}\n```\n\n"
        "## Sample user prompts\n" + sample_block + "\n\n"
        "Produce a concise Markdown wiki entry covering: recurring themes, "
        "frequently asked questions, notable failure/error patterns, and any "
        "observations useful for operators. Use headings and bullet lists. "
        "Write in the same language as the user prompts."
    )


async def run_daily_summary(db: Database, day: str | None = None) -> None:
    """Generate the wiki summary + compress raw sessions for `day` (default yesterday)."""
    day = day or _yesterday_day()
    config = get_config()
    logs = await db.get_call_logs_for_day(day)
    if not logs:
        logger.info("No call logs for {}; skipping daily summary.", day)
        return

    stats = build_stats(logs)
    stats_json = json.dumps(stats, ensure_ascii=False)

    # Compress + retain raw sessions (rolling window).
    raw = json.dumps(
        [l.model_dump() for l in logs], ensure_ascii=False, default=str
    ).encode("utf-8")
    await db.save_raw_session(day, gzip.compress(raw))

    # Generate wiki summary via LLM (best effort; failures keep stats only).
    summary_md = ""
    try:
        summary_md = await _generate_wiki(db, _build_summary_prompt(logs, stats), config)
    except Exception as e:
        logger.warning("Daily summary LLM generation failed for {}: {}", day, e)

    await db.upsert_daily_summary(day, summary_md, stats_json)
    logger.info(
        "Daily summary for {} done: {} calls, {} errors, wiki {} chars.",
        day, stats["total_calls"], stats["error_calls"], len(summary_md),
    )


async def _generate_wiki(db: Database, prompt: str, config) -> str:
    """Call the configured summary group/model to produce the wiki markdown."""
    group_name = config.summary_group or "default"
    groups = await db.list_groups(enabled_only=True)
    group_id = None
    for g in groups:
        if g.name == group_name:
            group_id = g.id
            break
    if group_id is None and groups:
        group_id = groups[0].id
    if group_id is None:
        return ""
    router = GroupRouter(group_id=group_id, db=db)
    result = await router.route(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        stream=False,
    )
    return result.get("content", "") if isinstance(result, dict) else str(result)


async def purge_old_detail(db: Database) -> int:
    """Purge large call-log fields older than call_log_detail_days.

    Keeps the stats columns; only clears request_body/response_body/traceback.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=get_config().call_log_detail_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rowcount = await db.execute_write(
        """UPDATE call_logs
           SET request_body = NULL, response_body = NULL, traceback = NULL
           WHERE created_at < ? AND request_body IS NOT NULL""",
        (cutoff,),
    )
    return rowcount


async def purge_old_raw_sessions(db: Database) -> int:
    """Delete compressed raw sessions older than raw_session_retention_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=get_config().raw_session_retention_days)).strftime(
        "%Y-%m-%d"
    )
    deleted = await db.delete_old_raw_sessions(cutoff)
    await db.delete_old_daily_summaries(cutoff)
    return deleted


async def purge_old_call_logs(db: Database, retention_days: Optional[int] = None) -> int:
    """Delete whole call_log rows older than call_logs_retention_days (default 180)."""
    days = retention_days if retention_days is not None else get_config().call_logs_retention_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    deleted = await db.delete_old_call_logs(cutoff)
    if deleted > 0:
        logger.info("Cleaned up {} old call_log records", deleted)
    return deleted
