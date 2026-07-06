"""MemoryAgent — LangChain create_react_agent wrapper for MemWiki."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

try:
    from langchain.agents import create_react_agent
except ImportError:
    from langgraph.prebuilt import create_react_agent

from botflow.common.logger import get_logger
from botflow.router import CooldownManager, GroupRouter
from botflow.wiki.skills import get_skill_prompt
from botflow.wiki.tools_impl import set_wiki_dir, wiki_tools
from botflow.wiki.types import BotflowLLM

log = get_logger("wiki.agent")


class MemoryAgent:
    """Wraps LangGraph's create_react_agent with botflow's LLM provider system.

    Usage:
        agent = MemoryAgent(wiki_dir, model_group="fast", router=router)
        result = await agent.run("remember", title="RAG", content="Retrieval Augmented Generation...")
    """

    def __init__(
        self,
        wiki_dir: Path,
        model_group: str = "fast",
        router: GroupRouter | None = None,
        db: Any = None,
        cooldown_manager: CooldownManager | None = None,
        max_iterations: int = 10,
    ) -> None:
        self.wiki_dir = wiki_dir
        self.model_group = model_group
        self.max_iterations = max_iterations

        # Set the wiki directory for all tools
        set_wiki_dir(wiki_dir)

        # Build router if not provided
        if router is None:
            from botflow.core import _get_db
            db = db or _get_db()
            cooldown = cooldown_manager or CooldownManager()
            # Find the fast model group ID from DB
            import asyncio

            async def _find_group_id() -> int:
                groups = await db.get_model_groups()
                for g in groups:
                    if g.name == model_group:
                        return g.id
                # Fallback: use first group
                return groups[0].id if groups else 1

            try:
                loop = asyncio.get_running_loop()
                group_id = loop.run_until_complete(_find_group_id())
            except RuntimeError:
                # No running loop, create one
                group_id = asyncio.run(_find_group_id())

            router = GroupRouter(group_id=group_id, db=db, cooldown_manager=cooldown)

        self.router = router
        self.llm = BotflowLLM(model_group=model_group, router=router)

    async def run(self, skill_name: str, user_args: str) -> str:
        """Run a wiki skill with the given user arguments.

        Args:
            skill_name: Skill name (remember/recall/query/learn/research).
            user_args: User-provided arguments as a string.

        Returns:
            Agent response text.
        """
        system_prompt = get_skill_prompt(skill_name, str(self.wiki_dir))

        agent = create_react_agent(
            model=self.llm,
            tools=wiki_tools,
            prompt=system_prompt,
        )

        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_args)]},
            config={"recursion_limit": self.max_iterations},
        )

        # Extract the final AI message
        messages = result.get("messages", [])
        if messages:
            last = messages[-1]
            return last.content if hasattr(last, "content") else str(last)
        return "No response from agent."
