from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.trilobite.prompts import COMPACTION_PROMPT

if TYPE_CHECKING:
    from src.trilobite.agent import Agent


def should_compact(agent: Agent) -> bool:
    """Decide whether the context is large enough to warrant compaction.

    The only measure used is ``_token_count``: the real prompt size reported by
    the last API response. Estimated token counts are not consulted -- local
    estimates drift from the provider's own accounting (most dramatically for
    images, whose cost depends on the provider's resizing, not on the payload
    size), which used to trigger compaction spuriously right after reading an
    image. The cost of that fidelity is one turn of lag: whatever is appended
    after the last response is measured by the next response.
    """
    if not agent._token_count:
        return False
    threshold = int(agent.max_context_tokens * agent.compaction_trigger_ratio)
    return agent._token_count >= threshold


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
