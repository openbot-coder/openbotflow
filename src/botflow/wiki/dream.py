"""Dream — background maintenance task for MemWiki.

Runs every 24 hours:
1. Orphan page detection (pages with no inbound [[links]])
2. Stale page detection (90+ days since last update)
3. Broken link detection ([[Link]] pointing to non-existent files)
4. Refresh index.md
5. Append run summary to log.md
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

from botflow.common.logger import get_logger

log = get_logger("wiki.dream")

DREAM_INTERVAL = 24 * 60 * 60  # 24 hours in seconds
STALE_THRESHOLD_DAYS = 90


async def start_dream_task(wiki_dir: Path) -> asyncio.Task:
    """Start the dream background task.

    Args:
        wiki_dir: Path to the MemWiki knowledge base.

    Returns:
        asyncio.Task that can be cancelled on shutdown.
    """
    async def _loop():
        while True:
            try:
                await asyncio.sleep(DREAM_INTERVAL)
                await run_dream(wiki_dir)
            except asyncio.CancelledError:
                log.info("Dream task cancelled.")
                break
            except Exception as e:
                log.error("Dream task failed: {}", e)

    task = asyncio.create_task(_loop())
    log.info("Dream task started (interval: {}h).", DREAM_INTERVAL // 3600)
    return task


async def run_dream(wiki_dir: Path) -> None:
    """Execute a single dream cycle.

    Scans the wiki for issues and generates a report.
    """
    log.info("Dream cycle starting...")

    now = datetime.now()
    all_files = list(wiki_dir.rglob("*.md"))
    # Exclude index.md and log.md from analysis
    wiki_files = [f for f in all_files if f.name not in ("index.md", "log.md")]

    # Collect all [[links]] and backlinks
    link_pattern = re.compile(r"\[\[([^\]]+)\]\]")
    all_links: dict[str, set[str]] = {}  # file -> set of links it contains
    all_targets: dict[str, set[str]] = {}  # target -> set of files linking to it

    for f in wiki_files:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        links = set(link_pattern.findall(content))
        rel = str(f.relative_to(wiki_dir))
        all_links[rel] = links
        for link in links:
            all_targets.setdefault(link, set()).add(rel)

    issues: list[str] = []

    # 1. Orphan pages (no inbound links)
    for f in wiki_files:
        rel = str(f.relative_to(wiki_dir))
        name = f.stem
        if name not in all_targets and rel != "index.md":
            issues.append(f"[orphan] {rel} — no inbound [[links]]")

    # 2. Stale pages
    for f in wiki_files:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if (now - mtime) > timedelta(days=STALE_THRESHOLD_DAYS):
                rel = str(f.relative_to(wiki_dir))
                days = (now - mtime).days
                issues.append(f"[stale] {rel} — last updated {days} days ago")
        except Exception:
            continue

    # 3. Broken links
    valid_names = {f.stem for f in wiki_files}
    valid_names.add("index.md")
    valid_names.add("log.md")
    for f, links in all_links.items():
        for link in links:
            # Normalize link for matching
            link_stem = Path(link).stem if "/" in link else link
            if link_stem not in valid_names:
                issues.append(f"[broken] {f} → [[{link}]] — target not found")

    # 4. Refresh index.md
    _refresh_index(wiki_dir, wiki_files)

    # 5. Append to log.md
    summary = f"Dream ran: {len(wiki_files)} files scanned, {len(issues)} issues found."
    if issues:
        summary += "\nIssues:\n" + "\n".join(f"  - {i}" for i in issues)
    _append_log(wiki_dir, now, summary)

    log.info("Dream cycle complete: {}", summary)


def _refresh_index(wiki_dir: Path, wiki_files: list[Path]) -> None:
    """Regenerate index.md from current files."""
    sections: dict[str, list[str]] = {
        "Sources": [],
        "Concepts": [],
        "Entities": [],
        "Syntheses": [],
    }

    for f in sorted(wiki_files):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # Extract title from frontmatter
        title = f.stem
        for line in content.split("\n"):
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip('"')
                break
        # Extract first non-empty line as description
        desc = ""
        in_body = False
        for line in content.split("\n"):
            if in_body and line.strip():
                desc = line.strip()[:80]
                break
            if line.strip() == "---" and not in_body:
                in_body = True

        rel = str(f.relative_to(wiki_dir))
        entry = f"- [{title}]({rel}) — {desc}" if desc else f"- [{title}]({rel})"
        parent = f.parent.name
        if parent == "sources":
            sections["Sources"].append(entry)
        elif parent == "concepts":
            sections["Concepts"].append(entry)
        elif parent == "entities":
            sections["Entities"].append(entry)
        elif parent == "syntheses":
            sections["Syntheses"].append(entry)

    lines = ["# MemWiki Index\n"]
    for section, entries in sections.items():
        lines.append(f"## {section}\n")
        lines.extend(entries)
        lines.append("")

    index_path = wiki_dir / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")


def _append_log(wiki_dir: Path, now: datetime, summary: str) -> None:
    """Append a dream summary entry to log.md."""
    log_path = wiki_dir / "log.md"
    entry = f"\n## [{now.strftime('%Y-%m-%d')}] dream | Dream Maintenance\n{summary}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
