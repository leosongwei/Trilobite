from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.trilobite.prompts import COMPACTION_PROMPT
from src.trilobite.tokens import estimate_tokens_for_messages, estimate_tokens_for_tools

if TYPE_CHECKING:
    from src.trilobite.agent import Agent


def should_compact(agent: Agent) -> bool:
    """Estimate whether the context is large enough to warrant compaction.

    ``_token_count`` is the real usage reported by the last API call, which
    only reflects messages from the last compact marker onward. ``pending`` is
    the messages added since that call. Together they approximate what the
    next request would cost, so pre-marker messages never skew the estimate.
    """
    tools = agent._permission.filter_definitions()
    pending = agent.history.raw[agent._token_covered:]
    estimated = (
        agent._token_count
        + estimate_tokens_for_messages(pending)
        + estimate_tokens_for_tools(tools)
    )
    threshold = int(agent.max_context_tokens * agent.compaction_trigger_ratio)
    return estimated >= threshold


def build_compact_prompt(agent: Agent) -> str:
    """Build the user-facing instruction that asks the model for a handoff summary."""
    prompt = COMPACTION_PROMPT

    todo_path = agent.session_dir / "todos.json"
    if todo_path.exists():
        try:
            todos = json.loads(todo_path.read_text())
            if todos:
                lines = ["\nCurrent todo list:"]
                for t in todos:
                    status = t.get("status", "pending")
                    lines.append(f"  [{status}] {t['title']}")
                prompt += "\n".join(lines)
        except Exception:
            pass

    return prompt
